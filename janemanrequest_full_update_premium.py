"""
JANEMAN Telegram admin bot — join-request welcome messages with custom emoji.

Environment:
  BOT_TOKEN  = fresh Telegram bot token from @BotFather
  ADMIN_ID   = your numeric Telegram user ID
  DB_PATH    = optional SQLite path (default: janeman_pro.db)
Install: pip install -U python-telegram-bot
Run:     python janemanrequest_updated.py

Join-request DMs use ChatJoinRequest.user_chat_id. Telegram allows contacting
that private chat for a limited period (5 minutes) after a join request, provided
the bot is an admin with can_invite_users. Send saved messages BEFORE approving
the request. copy_message is used to preserve Telegram-supported message entities,
including custom emoji, when Telegram permits copying that message.
"""
import asyncio
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ChatJoinRequestHandler, ContextTypes, MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("janeman")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
# Render persistent disk: set DB_PATH=/var/data/janeman_pro.db and mount a disk at /var/data.
# If no disk is mounted, use local file (may be lost on redeploy/replacement).
_default_db = "/var/data/janeman_pro.db" if os.path.isdir("/var/data") else "janeman_pro.db"
DB_PATH = os.getenv("DB_PATH", _default_db).strip() or _default_db
DB_LOCK = threading.RLock()
CACHED_MESSAGES = []


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Render Web Service health endpoint; also supports /health checks.
        body = b"JANEMAN service is running\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("Health endpoint: " + fmt, *args)


def run_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = None
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        log.info("Health endpoint listening on port %s", port)
        server.serve_forever()
    except OSError:
        log.exception("Health endpoint could not start")
    finally:
        if server is not None:
            server.server_close()


def db_connect():
    # Create the parent folder if DB_PATH points to a custom directory.
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    global CACHED_MESSAGES
    with DB_LOCK, db_connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        conn.execute("""CREATE TABLE IF NOT EXISTS messages_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            msg_id TEXT NOT NULL
        )""")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_accept','OFF')")
        conn.execute("INSERT OR IGNORE INTO stats(key,count) VALUES('total_requests',0)")
        conn.execute("INSERT OR IGNORE INTO stats(key,count) VALUES('accepted',0)")
        conn.execute("""CREATE TABLE IF NOT EXISTS flow_sessions (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            full_name TEXT DEFAULT '',
            loss_amount TEXT DEFAULT '',
            device TEXT DEFAULT '',
            selected_option TEXT DEFAULT '',
            step TEXT DEFAULT 'idle',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('option_1','Option 1')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('option_2','Option 2')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('option_3','Option 3')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('option_4','Option 4')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('prompt_amount','कृपया लॉस का अमाउंट लिखें। उदाहरण: 1000, 20K, 1 lakh, एक हजार')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('prompt_device','आपका फोन कौन-सा है? नीचे चुनें:')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('prompt_options','नीचे दिए गए चार विकल्पों में से एक चुनें:')")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('prompt_done','✅ आपकी रिक्वेस्ट हमें मिल गई है। कृपया प्रतीक्षा करें; हमारी टीम आपसे संपर्क करेगी।')")
        CACHED_MESSAGES = conn.execute(
            "SELECT chat_id,msg_id FROM messages_list ORDER BY id ASC"
        ).fetchall()


def get_setting(key, default="OFF"):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )


def add_saved_message(chat_id, msg_id):
    global CACHED_MESSAGES
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO messages_list(chat_id,msg_id) VALUES(?,?)",
            (str(chat_id), str(msg_id))
        )
        CACHED_MESSAGES = conn.execute(
            "SELECT chat_id,msg_id FROM messages_list ORDER BY id ASC"
        ).fetchall()


def clear_saved_messages():
    global CACHED_MESSAGES
    with DB_LOCK, db_connect() as conn:
        conn.execute("DELETE FROM messages_list")
        CACHED_MESSAGES = []


def add_user(user_id):
    with DB_LOCK, db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))


def get_all_users():
    with DB_LOCK, db_connect() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users ORDER BY user_id")]


def get_stats():
    with DB_LOCK, db_connect() as conn:
        return dict(conn.execute("SELECT key,count FROM stats").fetchall())


def bump_stat(key, amount=1):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO stats(key,count) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET count=count+excluded.count",
            (key, amount)
        )


def is_admin(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def main_menu():
    s = get_stats()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 Join requests: {s.get('total_requests', 0)}", callback_data="noop")],
        [InlineKeyboardButton(f"✅ Auto-approved: {s.get('accepted', 0)}", callback_data="noop")],
        [InlineKeyboardButton(f"👥 Users who started bot: {len(get_all_users())}", callback_data="noop")],
        [InlineKeyboardButton("⚙️ Welcome settings", callback_data="welcome")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton("🔄 Refresh panel", callback_data="home")],
    ])


def welcome_menu():
    status = get_setting("auto_accept")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Auto-approve: {'🟢 ON' if status == 'ON' else '🔴 OFF'}", callback_data="toggle_auto")],
        [InlineKeyboardButton(f"➕ Add message/media ({len(CACHED_MESSAGES)} saved)", callback_data="add_welcome")],
        [InlineKeyboardButton("🗑️ Clear saved messages", callback_data="clear_welcome")],
        [InlineKeyboardButton("👁️ Test sequence to admin", callback_data="test")],
        [InlineKeyboardButton("⚙️ Customize all user messages", callback_data="flow_settings")],
        [InlineKeyboardButton("⬅️ Main menu", callback_data="home")],
    ])


def flow_settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Message after Start Now", callback_data="cfg_start_message")],
        [InlineKeyboardButton("💸 Loss amount question", callback_data="cfg_prompt_amount")],
        [InlineKeyboardButton("📱 Phone question", callback_data="cfg_prompt_device")],
        [InlineKeyboardButton("🔘 Four option button names", callback_data="cfg_options")],
        [InlineKeyboardButton("📋 Options instruction message", callback_data="cfg_prompt_options")],
        [InlineKeyboardButton("✅ Final confirmation message", callback_data="cfg_prompt_done")],
        [InlineKeyboardButton("⬅️ Welcome settings", callback_data="welcome")],
    ])


def option_settings_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Option {i}: {get_setting(f'option_{i}', f'Option {i}')}", callback_data=f"cfg_option_{i}")]
        for i in range(1, 5)
    ] + [[InlineKeyboardButton("⬅️ Message settings", callback_data="flow_settings")]])


async def edit_panel(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            log.warning("Panel edit failed: %s", exc)
            await query.message.reply_text(text, reply_markup=markup)
    except TelegramError:
        log.exception("Panel edit failed")
        await query.message.reply_text(text, reply_markup=markup)


async def copy_sequence(bot, destination_chat_id, add_start_button=False):
    sent = failed = 0
    saved = list(CACHED_MESSAGES)
    for index, (source_chat_id, source_message_id) in enumerate(saved):
        try:
            kwargs = {}
            if add_start_button and index == len(saved) - 1:
                kwargs["reply_markup"] = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Start Now", callback_data="flow_start")]
                ])
            await bot.copy_message(
                chat_id=int(destination_chat_id),
                from_chat_id=int(source_chat_id),
                message_id=int(source_message_id),
                **kwargs,
            )
            sent += 1
        except TelegramError as exc:
            failed += 1
            log.warning("Copy message %s from %s failed to %s: %s",
                        source_message_id, source_chat_id, destination_chat_id, exc)
    return sent, failed


def save_flow(user, step, loss_amount=None, device=None, selected_option=None):
    with DB_LOCK, db_connect() as conn:
        conn.execute("""INSERT INTO flow_sessions
            (user_id, username, full_name, loss_amount, device, selected_option, step, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, full_name=excluded.full_name,
                loss_amount=COALESCE(excluded.loss_amount, flow_sessions.loss_amount),
                device=COALESCE(excluded.device, flow_sessions.device),
                selected_option=COALESCE(excluded.selected_option, flow_sessions.selected_option),
                step=excluded.step, updated_at=CURRENT_TIMESTAMP""",
            (user.id, user.username or "", user.full_name or "",
             loss_amount, device, selected_option, step))


def get_flow(user_id):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("""SELECT user_id, username, full_name, loss_amount,
            device, selected_option, step FROM flow_sessions WHERE user_id=?""",
            (int(user_id),)).fetchone()
    if not row:
        return None
    keys = ("user_id", "username", "full_name", "loss_amount", "device", "selected_option", "step")
    return dict(zip(keys, row))


def get_options_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(get_setting(f"option_{i}", f"Option {i}"), callback_data=f"flow_option_{i}")]
        for i in range(1, 5)
    ])


def looks_like_amount(value):
    value = (value or "").strip().lower()
    if any(ch.isdigit() for ch in value) or any(ch in "०१२३४५६७८९" for ch in value):
        return True
    amount_words = (
        "k", "thousand", "million", "lakh", "lac", "crore", "cr", "rupee", "rupees",
        "rs", "inr", "हजार", "लाख", "करोड़", "करोड", "रुपये", "रुपया", "पैसे"
    )
    return any(word in value for word in amount_words)


async def notify_admin_of_user(context, user, heading, message=None):
    flow = get_flow(user.id) or {}
    username = f"@{user.username}" if user.username else "(no username)"
    details = (
        f"📩 {heading}\n"
        f"👤 Name: {user.full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 User ID: {user.id}\n"
        f"💸 Loss: {flow.get('loss_amount') or 'Not provided'}\n"
        f"📱 Device: {flow.get('device') or 'Not selected'}\n"
        f"🧩 Option: {flow.get('selected_option') or 'Not selected'}"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Reply to this user", callback_data=f"admin_reply_{user.id}")]])
    await context.bot.send_message(chat_id=ADMIN_ID, text=details, reply_markup=markup)
    if message is not None:
        try:
            await context.bot.copy_message(chat_id=ADMIN_ID, from_chat_id=message.chat_id,
                                           message_id=message.message_id)
        except TelegramError as exc:
            log.warning("Could not copy user message %s: %s", user.id, exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    add_user(user.id)
    if not is_admin(update):
        await msg.reply_text("Welcome! Bot active hai.")
        return
    context.user_data.pop("state", None)
    await msg.reply_text(
        "👑 JANEMAN ADMIN PANEL\n\n"
        "Welcome Settings → Add message/media mein custom-emoji messages set karein.\n"
        "Commands: /help /stats /id /done /cancel",
        reply_markup=main_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "/start — open admin panel\n/help — help\n/stats — statistics\n"
            "/id — show numeric ID\n/done — finish welcome sequence\n/cancel — cancel action\n"
            "/setoption1 TEXT, /setoption2 TEXT, /setoption3 TEXT — set choice buttons\n"
            "/options — view the four choices\n\n"
            "Saved messages are copied to the join request's user_chat_id before "
            "auto-approval, so the user does not need to start the bot first."
        )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_message:
        await update.effective_message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.effective_message:
        return
    s = get_stats()
    await update.effective_message.reply_text(
        f"📊 Statistics\nJoin requests: {s.get('total_requests', 0)}\n"
        f"Auto-approved: {s.get('accepted', 0)}\nUsers who started bot: {len(get_all_users())}\n"
        f"Saved welcome items: {len(CACHED_MESSAGES)}\nAuto-approve: {get_setting('auto_accept')}"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if is_admin(update):
        context.user_data.pop("state", None)
        context.user_data.pop("reply_to_user", None)
        await update.effective_message.reply_text("Cancelled. Use /start to reopen panel.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.effective_message:
        return
    if context.user_data.get("state") == "waiting_welcome":
        context.user_data.pop("state", None)
        await update.effective_message.reply_text(
            f"✅ Saved sequence finished. Total: {len(CACHED_MESSAGES)}",
            reply_markup=welcome_menu()
        )
    else:
        await update.effective_message.reply_text("Nothing to finish. Open /start → Welcome settings.")


async def set_option_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.effective_message:
        return
    command = (update.effective_message.text or "").split()[0].split("@")[0].lower()
    option_number = command.replace("/setoption", "")
    if option_number not in ("1", "2", "3", "4"):
        return
    value = " ".join(context.args).strip()
    if not value:
        await update.effective_message.reply_text(
            f"Use: /setoption{option_number} Your button text\nExample: /setoption{option_number} Android Tool"
        )
        return
    set_setting(f"option_{option_number}", value[:64])
    await update.effective_message.reply_text(f"✅ Option {option_number} updated: {value[:64]}")


async def options_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Current choices:\n"
        + "\n".join(f"{i}. {get_setting(f'option_{i}', f'Option {i}')}" for i in range(1, 5))
        + "\n\nChange with /setoption1 through /setoption4 followed by button text."
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    action = q.data or ""
    if action == "flow_start":
        await q.answer()
        add_user(q.from_user.id)
        save_flow(q.from_user, "waiting_amount")
        start_chat = get_setting("start_message_chat", "")
        start_msg = get_setting("start_message_id", "")
        if start_chat and start_msg:
            try:
                await context.bot.copy_message(chat_id=q.message.chat_id,
                    from_chat_id=int(start_chat), message_id=int(start_msg))
            except (TelegramError, ValueError) as exc:
                log.warning("Configured start message could not be copied: %s", exc)
        pchat, pmsg = get_setting("prompt_amount_chat", ""), get_setting("prompt_amount_id", "")
        if pchat and pmsg:
            try:
                await context.bot.copy_message(chat_id=q.message.chat_id, from_chat_id=int(pchat), message_id=int(pmsg))
            except (TelegramError, ValueError) as exc:
                log.warning("Amount prompt copy failed: %s", exc)
                await q.message.reply_text(get_setting("prompt_amount"))
        else:
            await q.message.reply_text(get_setting("prompt_amount"))
        return
    if action in ("flow_android", "flow_iphone"):
        await q.answer()
        device = "Android" if action == "flow_android" else "iPhone"
        save_flow(q.from_user, "waiting_option", device=device)
        pchat, pmsg = get_setting("prompt_options_chat", ""), get_setting("prompt_options_id", "")
        if pchat and pmsg:
            try:
                await context.bot.copy_message(chat_id=q.message.chat_id, from_chat_id=int(pchat), message_id=int(pmsg))
            except (TelegramError, ValueError) as exc:
                log.warning("Options prompt copy failed: %s", exc)
                await q.message.reply_text(get_setting("prompt_options"))
        else:
            await q.message.reply_text(get_setting("prompt_options"))
        await q.message.reply_text("👇", reply_markup=get_options_keyboard())
        return
    if action.startswith("flow_option_"):
        await q.answer()
        number = action.rsplit("_", 1)[-1]
        if number not in ("1", "2", "3", "4"):
            return
        label = get_setting(f"option_{number}", f"Option {number}")
        save_flow(q.from_user, "completed", selected_option=label)
        pchat, pmsg = get_setting("prompt_done_chat", ""), get_setting("prompt_done_id", "")
        if pchat and pmsg:
            try:
                await context.bot.copy_message(chat_id=q.message.chat_id, from_chat_id=int(pchat), message_id=int(pmsg))
            except (TelegramError, ValueError) as exc:
                log.warning("Final message copy failed: %s", exc)
                await q.message.reply_text(get_setting("prompt_done"))
        else:
            await q.message.reply_text(get_setting("prompt_done"))
        await notify_admin_of_user(context, q.from_user, "New support request")
        return
    if action.startswith("admin_reply_"):
        if q.from_user.id != ADMIN_ID:
            await q.answer("Access denied", show_alert=True)
            return
        target_id = action.rsplit("_", 1)[-1]
        if not target_id.isdigit():
            await q.answer("Invalid user", show_alert=True)
            return
        context.user_data["reply_to_user"] = int(target_id)
        await q.answer()
        await q.message.reply_text(f"✍️ अब इस user (ID: {target_id}) को भेजने के लिए text/media भेजें। Cancel: /cancel")
        return
    if q.from_user.id != ADMIN_ID:
        await q.answer("Access denied", show_alert=True)
        return
    await q.answer()

    if action == "noop":
        return
    if action == "home":
        context.user_data.pop("state", None)
        await edit_panel(q, "👑 JANEMAN ADMIN PANEL\nChoose a feature:", main_menu())
    elif action == "welcome":
        await edit_panel(q,
            f"⚙️ Welcome settings\nAuto-approve: {get_setting('auto_accept')}\n"
            f"Saved messages: {len(CACHED_MESSAGES)}\n"
            "Saved messages are copied as Telegram messages, preserving supported custom emoji.",
            welcome_menu())
    elif action == "toggle_auto":
        value = "OFF" if get_setting("auto_accept") == "ON" else "ON"
        set_setting("auto_accept", value)
        await edit_panel(q, f"⚙️ Welcome settings\nAuto-approve: {value}\nSaved: {len(CACHED_MESSAGES)}",
                         welcome_menu())
    elif action == "add_welcome":
        context.user_data.pop("reply_to_user", None)
        context.user_data["state"] = "waiting_welcome"
        await edit_panel(q,
            "📝 Ab apna text/custom emoji, photo, video, voice, sticker ya media bhejo.\n"
            "Har message sequence mein save hoga. Complete hone par /done; cancel ke liye /cancel.")
    elif action == "set_start_message":
        context.user_data["state"] = "waiting_start_message"
        await edit_panel(q,
            "📝 Start Now दबाने के बाद भेजा जाने वाला अपना Telegram text/custom emoji/media भेजें।\n"
            "यह एक message save होगा; cancel के लिए /cancel.")
    elif action == "flow_settings":
        context.user_data.pop("reply_to_user", None)
        context.user_data.pop("state", None)
        await edit_panel(q, "⚙️ Customize every user-facing message:", flow_settings_menu())
    elif action == "cfg_start_message":
        context.user_data.pop("reply_to_user", None)
        context.user_data["state"] = "waiting_start_message"
        await edit_panel(q, "Start Now के बाद भेजा जाने वाला text/custom emoji/media message भेजें।")
    elif action in ("cfg_prompt_amount", "cfg_prompt_device", "cfg_prompt_options", "cfg_prompt_done"):
        context.user_data.pop("reply_to_user", None)
        states = {
            "cfg_prompt_amount": ("waiting_prompt_amount", "लॉस अमाउंट पूछने वाला message भेजें।"),
            "cfg_prompt_device": ("waiting_prompt_device", "फोन पूछने वाला message भेजें।"),
            "cfg_prompt_options": ("waiting_prompt_options", "चार ऑप्शन से पहले दिखने वाला message भेजें।"),
            "cfg_prompt_done": ("waiting_prompt_done", "आखिरी confirmation message भेजें।"),
        }
        state, prompt = states[action]
        context.user_data["state"] = state
        await edit_panel(q, prompt + " Text, custom emoji, photo या media भेजें।")
    elif action == "cfg_options":
        context.user_data.pop("reply_to_user", None)
        context.user_data.pop("state", None)
        await edit_panel(q, "चारों बटन अपने हिसाब से सेट करें:", option_settings_menu())
    elif action.startswith("cfg_option_"):
        number = action.rsplit("_", 1)[-1]
        if number not in ("1", "2", "3", "4"):
            return
        context.user_data.pop("reply_to_user", None)
        context.user_data["state"] = f"waiting_option_{number}"
        await edit_panel(q, f"Option {number} के लिए नया button text भेजें।")
    elif action == "clear_welcome":
        clear_saved_messages()
        await edit_panel(q, "🗑️ Welcome sequence cleared.", welcome_menu())
    elif action == "test":
        sent, failed = await copy_sequence(context.bot, ADMIN_ID)
        await q.message.reply_text(
            f"Test result — sent: {sent}, failed: {failed}." if sent or failed
            else "Sequence empty hai. Add message/media first.")
    elif action == "broadcast":
        context.user_data.pop("reply_to_user", None)
        context.user_data["state"] = "waiting_broadcast"
        await edit_panel(q, "📣 Ab broadcast ke liye ek message/media bhejo.\nCancel: /cancel")


async def content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    # Admin setup states take priority over reply mode: never forward setup content to a member.
    if is_admin(update):
        state = context.user_data.get("state")
        config_states = {
            "waiting_start_message": ("start_message_chat", "start_message_id", "Start message"),
            "waiting_prompt_amount": ("prompt_amount_chat", "prompt_amount_id", "Loss amount question"),
            "waiting_prompt_device": ("prompt_device_chat", "prompt_device_id", "Phone question"),
            "waiting_prompt_options": ("prompt_options_chat", "prompt_options_id", "Options instruction"),
            "waiting_prompt_done": ("prompt_done_chat", "prompt_done_id", "Final confirmation"),
        }
        if state in config_states:
            chat_key, msg_key, label = config_states[state]
            set_setting(chat_key, str(msg.chat_id))
            set_setting(msg_key, str(msg.message_id))
            context.user_data.pop("state", None)
            context.user_data.pop("reply_to_user", None)
            await msg.reply_text(f"✅ {label} saved. It will be copied to users.")
            return
        if state and state.startswith("waiting_option_"):
            number = state.rsplit("_", 1)[-1]
            value = (msg.text or msg.caption or "").strip()
            if not value:
                await msg.reply_text("कृपया button text भेजें।")
                return
            set_setting(f"option_{number}", value[:64])
            context.user_data.pop("state", None)
            context.user_data.pop("reply_to_user", None)
            await msg.reply_text(f"✅ Option {number} updated.", reply_markup=option_settings_menu())
            return

        # Admin can reply with any Telegram-supported text/media after pressing Reply.
        target_id = context.user_data.get("reply_to_user")
        if target_id:
            try:
                await context.bot.copy_message(chat_id=int(target_id), from_chat_id=msg.chat_id,
                                               message_id=msg.message_id)
                await msg.reply_text(f"✅ Message sent to user {target_id}.")
            except TelegramError as exc:
                await msg.reply_text(f"❌ Could not send message: {exc}")
            return

        state = context.user_data.get("state")
        if state == "waiting_welcome":
            add_saved_message(msg.chat_id, msg.message_id)
            await msg.reply_text(f"✅ Saved item #{len(CACHED_MESSAGES)}. Send another item or /done.")
        elif state == "waiting_broadcast":
            context.user_data.pop("state", None)
            users = get_all_users()
            await msg.reply_text(f"📣 Broadcast started for {len(users)} users.")
            sent = failed = 0
            for user_id in users:
                try:
                    await context.bot.copy_message(
                        chat_id=user_id, from_chat_id=msg.chat_id, message_id=msg.message_id
                    )
                    sent += 1
                except TelegramError as exc:
                    failed += 1
                    log.info("Broadcast failed to %s: %s", user_id, exc)
            await msg.reply_text(f"🏁 Broadcast complete\n✅ Sent: {sent}\n❌ Failed: {failed}")
        return

    # User-side intake and ongoing two-way support chat.
    flow = get_flow(user.id)
    if not flow:
        return
    step = flow.get("step")
    if step == "waiting_amount":
        amount_text = (msg.text or msg.caption or "").strip()
        if not looks_like_amount(amount_text):
            await msg.reply_text("कृपया लॉस का अमाउंट लिखें। उदाहरण: 1000, 20K, 1 lakh, एक हजार")
            return
        save_flow(user, "waiting_device", loss_amount=amount_text[:100])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Android", callback_data="flow_android"),
            InlineKeyboardButton("iPhone", callback_data="flow_iphone"),
        ]])
        pchat, pmsg = get_setting("prompt_device_chat", ""), get_setting("prompt_device_id", "")
        if pchat and pmsg:
            try:
                await context.bot.copy_message(chat_id=msg.chat_id, from_chat_id=int(pchat), message_id=int(pmsg))
            except (TelegramError, ValueError) as exc:
                log.warning("Device prompt copy failed: %s", exc)
                await msg.reply_text(get_setting("prompt_device"))
            await msg.reply_text("👇", reply_markup=keyboard)
        else:
            await msg.reply_text(get_setting("prompt_device"), reply_markup=keyboard)
        return

    if step in ("waiting_device", "waiting_option"):
        await msg.reply_text("कृपया ऊपर दिए गए बटन में से अपना विकल्प चुनें।")
        return

    # After intake, relay every user message/media to admin with identity and reply control.
    if step == "completed":
        username = f"@{user.username}" if user.username else "(no username)"
        header = (
            f"📨 User message\n👤 {user.full_name}\n🔗 {username}\n🆔 ID: {user.id}\n"
            f"💸 Loss: {flow.get('loss_amount') or '—'} | 📱 {flow.get('device') or '—'} | "
            f"🧩 {flow.get('selected_option') or '—'}"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Reply to this user", callback_data=f"admin_reply_{user.id}")]])
        await context.bot.send_message(chat_id=ADMIN_ID, text=header, reply_markup=markup)
        try:
            await context.bot.copy_message(chat_id=ADMIN_ID, from_chat_id=msg.chat_id,
                                           message_id=msg.message_id)
        except TelegramError as exc:
            log.warning("Could not relay user content from %s: %s", user.id, exc)


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    if not request:
        return
    bump_stat("total_requests")
    # CRITICAL: use user_chat_id, not from_user.id, to contact join requester.
    # Telegram grants this chat ID a limited 5-minute window after the request.
    destination = request.user_chat_id
    if CACHED_MESSAGES:
        sent, failed = await copy_sequence(context.bot, destination, add_start_button=True)
        log.info("Welcome sequence for request %s: sent=%s failed=%s",
                 request.from_user.id, sent, failed)
    else:
        await context.bot.send_message(
            chat_id=destination,
            text="आगे बढ़ने के लिए नीचे Start Now दबाएँ।",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start Now", callback_data="flow_start")]])
        )

    # Send welcome items before approving, so the temporary contact window remains valid.
    if get_setting("auto_accept") == "ON":
        try:
            await context.bot.approve_chat_join_request(
                chat_id=request.chat.id, user_id=request.from_user.id
            )
            bump_stat("accepted")
        except TelegramError:
            log.exception("Could not approve join request from %s", request.from_user.id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram error: %s", context.error, exc_info=context.error)


def build_application():
    """Create a fresh Telegram application for each outer retry."""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("setoption1", set_option_command))
    app.add_handler(CommandHandler("setoption2", set_option_command))
    app.add_handler(CommandHandler("setoption3", set_option_command))
    app.add_handler(CommandHandler("options", options_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, content_handler))
    app.add_error_handler(error_handler)
    return app


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing. Set BOT_TOKEN in Render Environment.")
    if ADMIN_ID <= 0:
        raise SystemExit("ADMIN_ID missing/invalid. Set your numeric Telegram user ID.")

    init_db()
    # Start the HTTP health endpoint once for Render Web Service.
    threading.Thread(target=run_health_server, name="health-server", daemon=True).start()

    # Outer recovery loop: if polling exits due to a transient exception, wait and
    # create a fresh Application. Telegram/network retries are also handled by PTB.
    while True:
        app = None
        try:
            # Python 3.13/3.14 may not create a default event loop in MainThread.
            # python-telegram-bot's run_polling() expects a current loop.
            # Create a fresh loop for every retry because run_polling may close it.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            app = build_application()
            log.info("JANEMAN bot starting / polling")
            app.run_polling(
                poll_interval=1.0,
                timeout=30,
                drop_pending_updates=False,
                allowed_updates=Update.ALL_TYPES,
            )
            # If run_polling returns normally, don't silently terminate the worker.
            log.warning("Telegram polling stopped normally; restarting in 10 seconds")
        except KeyboardInterrupt:
            log.info("Shutdown requested; exiting")
            break
        except Exception:
            log.exception("Telegram bot stopped unexpectedly; restarting in 10 seconds")
        finally:
            # run_polling normally shuts down the application itself. This block is
            # intentionally defensive and does not mask the original exception.
            pass
        time.sleep(10)


if __name__ == "__main__":
    main()
