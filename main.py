import os
import json
import logging
import asyncio
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from flask import Flask, request
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import gspread
from groq import Groq
from oauth2client.service_account import ServiceAccountCredentials

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN            = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL      = os.environ["WEBHOOK_URL"]
OWNER_ID         = int(os.environ["OWNER_USER_ID"])
PRODUCT_SHEET_ID = os.environ["PRODUCT_FILE_ID"]
SECURE_SHEET_ID  = os.environ["SECURE_FILE_ID"]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheet(sheet_id, worksheet_index=0):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    creds  = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id).get_worksheet(worksheet_index)

def get_whitelist():
    try:
        raw = os.environ.get("WHITELIST", "")
        return [int(i.strip()) for i in raw.split(",") if i.strip().isdigit()]
    except Exception as e:
        logger.error(f"Whitelist error: {e}")
        return []

def get_products():
    try:
        return get_sheet(PRODUCT_SHEET_ID, worksheet_index=0).get_all_records()
    except Exception as e:
        logger.error(f"Products error: {e}")
        return []

def get_secure_data():
    try:
        return get_sheet(SECURE_SHEET_ID, worksheet_index=0).get_all_records()
    except Exception as e:
        logger.error(f"Secure data error: {e}")
        return []

# ── Access helpers ────────────────────────────────────────────────────────────
def is_owner(user_id):
    return user_id == OWNER_ID

def is_whitelisted(user_id):
    return is_owner(user_id) or user_id in get_whitelist()

# ── Formatters ────────────────────────────────────────────────────────────────
def format_currency(value):
    try:
        num = float(value)
        formatted = f"{round(num):,}".replace(",", ".")
        return f"Rp {formatted}"
    except:
        return str(value)

def format_product_full(r):
    return (
        f"📦 *{r.get('product_name', '-')}*\n"
        f"   Variation : {r.get('variation', '-')}\n"
        f"   Platform  : {r.get('platform', '-')}\n"
        f"   COGS      : {format_currency(r.get('cogs', 0))}\n"
        f"   Price     : {format_currency(r.get('sell_price', 0))}\n"
        f"   Updated   : {r.get('last_updated', '-')}\n"
    )

def split_into_chunks(text_blocks, max_chars=3500):
    chunks = []
    current = ""
    for block in text_blocks:
        if len(current) + len(block) > max_chars:
            chunks.append(current.strip())
            current = block
        else:
            current += "\n" + block
    if current.strip():
        chunks.append(current.strip())
    return chunks

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    if not is_whitelisted(uid):
        await update.message.reply_text("⛔ Sorry, you are not authorised to use this bot.")
        return
    msg = (
        f"👋 Hello {name}!\n\n"
        "Available commands:\n"
        "  /products — browse products by platform\n"
        "  /secure   — view secure data keys\n\n"
        "🔍 Search tips:\n"
        "  Type a product name to search\n"
        "  Add /s for Shopee only\n"
        "  Add /t for Tokopedia only\n"
        "  e.g. `fufang /s` or `fufang 12 botol /t`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        await update.message.reply_text("⛔ You are not authorised.")
        return
    keyboard = [
        [
            InlineKeyboardButton("🛍 Tokopedia", callback_data="platform_tokopedia"),
            InlineKeyboardButton("🟠 Shopee",    callback_data="platform_shopee"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a platform:", reply_markup=reply_markup)

async def callback_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_whitelisted(uid):
        await query.edit_message_text("⛔ You are not authorised.")
        return

    platform = "tokopedia" if query.data == "platform_tokopedia" else "shopee"
    label    = "Tokopedia" if platform == "tokopedia" else "Shopee"

    await query.edit_message_text(f"⏳ Fetching {label} products...")

    records = get_products()
    filtered = [r for r in records if platform in str(r.get("platform", "")).lower()]

    if not filtered:
        await query.edit_message_text(f"No products found for {label}.")
        return

    lines = []
    for r in filtered:
        lines.append(f"• {r.get('product_name', '-')} — {r.get('variation', '-')}")

    header = f"📋 *{label} Products:*\n\n"
    body   = "\n".join(lines)
    full   = header + body

    if len(full) > 3500:
        chunks = split_into_chunks([header] + [l + "\n" for l in lines])
        await query.edit_message_text(chunks[0], parse_mode="Markdown")
        for chunk in chunks[1:]:
            await query.message.reply_text(chunk, parse_mode="Markdown")
    else:
        await query.edit_message_text(full, parse_mode="Markdown")

async def cmd_secure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("⛔ This command is for the owner only.")
        return
    records = get_secure_data()
    if not records:
        await update.message.reply_text("No secure data found.")
        return
    lines = ["🔐 *Secure Data Keys:*\n"]
    for r in records:
        lines.append(f"• *{r.get('key', '-')}* — {r.get('type', '-')}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        await update.message.reply_text("⛔ You are not authorised.")
        return

    text = update.message.text.strip()

    # ── Secure data lookup ────────────────────────────────────────────────────
    if is_owner(uid):
        records = get_secure_data()
        keys    = [r.get("key", "").upper() for r in records]
        parts   = text.upper().split()

        if parts and parts[0] in keys:
            matched = [r for r in records if r.get("key", "").upper() == parts[0]]
            if len(parts) >= 2:
                matched = [r for r in matched if r.get("type", "").upper() == parts[1]]
            if matched:
                lines = []
                for r in matched:
                    lines.append(
                        f"🔑 *{r.get('key', '-')}*\n"
                        f"   Type  : {r.get('type', '-')}\n"
                        f"   Value : `{r.get('value', '-')}`\n"
                        f"   Note  : {r.get('note', '-')}\n"
                    )
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
                return
            else:
                await update.message.reply_text("❌ No secure data found for that key/type.")
                return

    # ── Product search ────────────────────────────────────────────────────────
    platform_filter = None
    if text.lower().endswith(" /s"):
        platform_filter = "shopee"
        text = text[:-3].strip()
    elif text.lower().endswith(" /t"):
        platform_filter = "tokopedia"
        text = text[:-3].strip()

    search_parts = text.lower().split()
    records      = get_products()

    from rapidfuzz import fuzz
    matched = []
    for r in records:
        name      = str(r.get("product_name", "")).lower()
        variation = str(r.get("variation", "")).lower()
        combined  = name + " " + variation

        if all(word in combined for word in search_parts):
            matched.append(r)
            continue

        # Fuzzy fallback
        score = fuzz.partial_ratio(text.lower(), combined)
        if score >= 70:
            matched.append(r)

    if platform_filter:
        matched = [r for r in matched if platform_filter in str(r.get("platform", "")).lower()]

    if not matched:
        await update.message.reply_text(f"❌ No products found for '*{update.message.text}*'", parse_mode="Markdown")
        return

    blocks = [format_product_full(r) for r in matched]
    chunks = split_into_chunks(blocks)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="Markdown")
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_whitelisted(uid):
        await update.message.reply_text("⛔ You are not authorised.")
        return

    await update.message.reply_text("🎙️ Processing your voice message...")

    # Download voice file from Telegram
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_path = "voice_message.ogg"
    await file.download_to_drive(file_path)

    # Transcribe using Groq Whisper
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    with open(file_path, "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=(file_path, f.read()),
            model="whisper-large-v3",
            language="id"
        )
    text = transcription.text.strip()
    await update.message.reply_text(f"🔍 Searching for: *{text}*", parse_mode="Markdown")

# Search products using transcribed text with fuzzy matching
    from rapidfuzz import fuzz
    search_parts = text.lower().split()
    records = get_products()
    matched = []
    for r in records:
        name = str(r.get("product_name", "")).lower()
        variation = str(r.get("variation", "")).lower()
        combined = name + " " + variation

        # Try exact match first
        if all(word in combined for word in search_parts):
            matched.append(r)
            continue

        # Try fuzzy match if exact fails
        score = fuzz.partial_ratio(text.lower(), combined)
        if score >= 70:
            matched.append(r)

    if not matched:
        await update.message.reply_text(f"❌ No products found for '*{text}*'", parse_mode="Markdown")
        return

    blocks = [format_product_full(r) for r in matched]
    chunks = split_into_chunks(blocks)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="Markdown")
# ── Build PTB app ─────────────────────────────────────────────────────────────
ptb_app = Application.builder().token(TOKEN).build()
ptb_app.add_handler(CommandHandler("start",    cmd_start))
ptb_app.add_handler(CommandHandler("products", cmd_products))
ptb_app.add_handler(CommandHandler("secure",   cmd_secure))
ptb_app.add_handler(CallbackQueryHandler(callback_platform, pattern="^platform_"))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
ptb_app.add_handler(MessageHandler(filters.VOICE, handle_voice))

# ── Flask app ─────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_update(data))
    finally:
        loop.close()
    return "ok", 200

async def process_update(data):
    async with ptb_app:
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)

@flask_app.route("/")
def index():
    return "Bot is running.", 200

# ── Local testing with polling ────────────────────────────────────────────────
import sys
if __name__ == "__main__" and "--polling" in sys.argv:
    import asyncio
    async def run_polling():
        await ptb_app.bot.delete_webhook()
        async with ptb_app:
            await ptb_app.start()
            await ptb_app.updater.start_polling()
            print("Bot running in polling mode... Press Ctrl+C to stop")
            await asyncio.sleep(float("inf"))
    asyncio.run(run_polling())
    sys.exit()

def register_webhook():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(Bot(TOKEN).set_webhook(url=WEBHOOK_URL))
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    finally:
        loop.close()

register_webhook()

app = flask_app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)
