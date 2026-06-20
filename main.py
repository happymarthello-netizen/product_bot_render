import os
import json
import logging
import asyncio
import time
from flask import Flask, request
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import gspread
from groq import Groq
from oauth2client.service_account import ServiceAccountCredentials
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from product_knowledge import PRODUCT_KNOWLEDGE

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN            = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL      = os.environ["WEBHOOK_URL"]
OWNER_ID         = int(os.environ["OWNER_USER_ID"])
PRODUCT_SHEET_ID = os.environ["PRODUCT_FILE_ID"]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheet(sheet_id, worksheet_name=None, worksheet_index=0):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    creds  = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(sheet_id)
    if worksheet_name:
        return sheet.worksheet(worksheet_name)
    return sheet.get_worksheet(worksheet_index)

def get_direct_prices():
    for attempt in range(2):
        try:
            records = get_sheet(PRODUCT_SHEET_ID, worksheet_name="Direct").get_all_records()
            if records:
                return records
        except Exception as e:
            logger.error(f"Direct prices error (attempt {attempt+1}): {e}")
            time.sleep(1)
    return []

def save_order(order_data, telegram_user_id, telegram_username):
    try:
        from datetime import datetime
        sheet = get_sheet(os.environ["ORDERS_SHEET_ID"], worksheet_index=0)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            timestamp,
            order_data.get("customer_name", ""),
            order_data.get("address", ""),
            order_data.get("phone", ""),
            order_data.get("product", ""),
            order_data.get("variation", ""),
            "",
            order_data.get("quantity", ""),
            order_data.get("delivery_method", ""),
            str(telegram_user_id),
            telegram_username or "",
            "pending"
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        import traceback
        logger.error(f"Save order error: {e}")
        logger.error(traceback.format_exc())
        return False

# ── Formatters ────────────────────────────────────────────────────────────────
def format_currency(value):
    try:
        num = float(value)
        formatted = f"{round(num):,}".replace(",", ".")
        return f"Rp {formatted}"
    except:
        return str(value)

def format_product_customer(r):
    return (
        f"📦 *{r.get('product_name', '-')}*\n"
        f"   Variation  : {r.get('variation', '-')}\n"
        f"   Harga      : {format_currency(r.get('sell_price_direct', 0))}\n"
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

# ── AI Natural Language ───────────────────────────────────────────────────────
def is_health_question(text):
    health_keywords = [
        "sakit", "gejala", "obat", "penyakit", "keluhan", "ada",
        "untuk", "cocok", "recommend", "suggest", "medicine", "sick",
        "stroke", "jantung", "darah", "lever", "hati", "kolesterol",
        "diabetes", "kanker", "luka", "infeksi", "kemoterapi",
        "saya", "apa", "apakah", "bagaimana", "tolong", "bantu",
        "kurang", "lelah", "lesu", "pusing", "nyeri", "radang",
        "hepatitis", "kelelahan", "demam", "trombosit", "stamina"
    ]
    text_lower = text.lower()
    word_count = len(text_lower.split())
    return any(keyword in text_lower for keyword in health_keywords) or word_count > 2

async def send_voice_reply(update, text):
    from gtts import gTTS
    tts = gTTS(text=text, lang='id')
    voice_path = "reply.mp3"
    tts.save(voice_path)
    with open(voice_path, "rb") as f:
        await update.message.reply_voice(voice=f)
    os.remove(voice_path)

def is_order_intent(text):
    order_keywords = [
        "mau pesan", "mau beli", "saya pesan", "saya beli", "order",
        "pesan", "beli", "mau order", "ambil", "checkout"
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in order_keywords)

async def extract_order_details(text):
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    
    extraction_prompt = """
Anda adalah asisten yang mengekstrak informasi pesanan dari teks pelanggan.
Ekstrak informasi berikut dari teks, dan balas HANYA dalam format JSON (tanpa markdown, tanpa penjelasan):

{
  "customer_name": "nama pelanggan atau kosong jika tidak ada",
  "address": "alamat lengkap atau kosong jika tidak ada",
  "phone": "nomor HP atau kosong jika tidak ada",
  "product": "nama produk yang dipesan atau kosong jika tidak ada",
  "variation": "variasi produk jika disebutkan atau kosong",
  "quantity": "jumlah yang dipesan (angka) atau kosong jika tidak ada",
  "delivery_method": "JNE/JNT/SiCepat/Gojek/Grab jika disebutkan, atau kosong jika tidak ada"
}

Jika informasi tidak disebutkan dalam teks, biarkan kosong (string kosong "").
"""
    
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=300,
        messages=[
            {"role": "system", "content": extraction_prompt},
            {"role": "user", "content": text}
        ]
    )
    
    import json as json_lib
    try:
        result = response.choices[0].message.content.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        print(f"DEBUG: AI extraction raw result: {result}")
        return json_lib.loads(result)
    except Exception as e:
        logger.error(f"Order extraction error: {e}")
        return {}
    
async def handle_ai_question(update, text, voice_reply=False):
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=150,
        messages=[
            {"role": "system", "content": PRODUCT_KNOWLEDGE},
            {"role": "user", "content": text}
        ]
    )
    reply = response.choices[0].message.content
    if voice_reply:
        await send_voice_reply(update, reply)
    else:
        await update.message.reply_text(reply)

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    msg = (
        f"👋 Hello {name}!\n\n"
        "Available commands:\n"
        "  /products — browse all products\n\n"
        "🔍 Search tips:\n"
        "  Type a product name to search\n"
        "  Or send a 🎙️ voice message!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching all products...")
    records = get_direct_prices()
    if not records:
        await update.message.reply_text("No products found.")
        return

    lines = [f"• {r.get('product_name', '-')} — {r.get('variation', '-')}" for r in records]
    header = "📋 *All Products:*\n\n"
    full = header + "\n".join(lines)

    if len(full) > 3500:
        chunks = split_into_chunks([header] + [l + "\n" for l in lines])
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    else:
        await update.message.reply_text(full, parse_mode="Markdown")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if is_order_intent(text):
        order_data = await extract_order_details(text)
        
        missing = []
        if not order_data.get("customer_name"):
            missing.append("nama")
        if not order_data.get("address"):
            missing.append("alamat")
        if not order_data.get("phone"):
            missing.append("no HP")
        if not order_data.get("delivery_method"):
            missing.append("metode pengiriman (JNE/JNT/SiCepat atau Gojek/Grab untuk Jakarta)")

        if missing:
            await update.message.reply_text(
                f"Baik! Boleh lengkapi informasi berikut: {', '.join(missing)} 😊"
            )
            return

        telegram_user_id = update.effective_user.id
        telegram_username = update.effective_user.username
        success = save_order(order_data, telegram_user_id, telegram_username)

        if success:
            await update.message.reply_text(
                f"✅ Pesanan Anda sudah saya catat!\n\n"
                f"Nama: {order_data.get('customer_name')}\n"
                f"Alamat: {order_data.get('address')}\n"
                f"No HP: {order_data.get('phone')}\n"
                f"Produk: {order_data.get('product')} ({order_data.get('variation')})\n"
                f"Jumlah: {order_data.get('quantity')}\n"
                f"Pengiriman: {order_data.get('delivery_method')}\n\n"
                f"Mohon cek kembali data di atas. Jika ada yang salah, "
                f"silakan kirim ulang dengan data yang benar.\n\n"
                f"Tim kami akan menghubungi Anda untuk konfirmasi total harga. Terima kasih! 🙏"
            )
        else:
            await update.message.reply_text("Maaf, terjadi kesalahan. Silakan coba lagi.")
        return

    if is_health_question(text):
        await handle_ai_question(update, text)
        return

    from rapidfuzz import fuzz
    search_parts = text.lower().split()
    records = get_direct_prices()

    matched = []
    for r in records:
        name = str(r.get("product_name", "")).lower()
        variation = str(r.get("variation", "")).lower()
        combined = name + " " + variation
        if all(word in combined for word in search_parts):
            matched.append(r)
            continue
        score = fuzz.partial_ratio(text.lower(), combined)
        if score >= 70:
            matched.append(r)

    if not matched:
        await update.message.reply_text(f"❌ No products found for '*{update.message.text}*'", parse_mode="Markdown")
        return

    blocks = [format_product_customer(r) for r in matched]
    chunks = split_into_chunks(blocks)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="Markdown")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎙️ Processing your voice message...")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_path = "voice_message.ogg"
    await file.download_to_drive(file_path)

    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    with open(file_path, "rb") as f:
        transcription = groq_client.audio.transcriptions.create(
            file=(file_path, f.read()),
            model="whisper-large-v3",
            language="id"
        )
    text = transcription.text.strip()
    await update.message.reply_text(f"🔍 Searching for: *{text}*", parse_mode="Markdown")

    if is_order_intent(text):
        order_data = await extract_order_details(text)
        
        missing = []
        if not order_data.get("customer_name"):
            missing.append("nama")
        if not order_data.get("address"):
            missing.append("alamat")
        if not order_data.get("phone"):
            missing.append("no HP")
        if not order_data.get("delivery_method"):
            missing.append("metode pengiriman (JNE/JNT/SiCepat atau Gojek/Grab untuk Jakarta)")

        if missing:
            await update.message.reply_text(
                f"Baik! Boleh lengkapi informasi berikut: {', '.join(missing)} 😊"
            )
            return

        telegram_user_id = update.effective_user.id
        telegram_username = update.effective_user.username
        success = save_order(order_data, telegram_user_id, telegram_username)

        if success:
            await update.message.reply_text(
                f"✅ Pesanan Anda sudah saya catat!\n\n"
                f"Nama: {order_data.get('customer_name')}\n"
                f"Alamat: {order_data.get('address')}\n"
                f"No HP: {order_data.get('phone')}\n"
                f"Produk: {order_data.get('product')} ({order_data.get('variation')})\n"
                f"Jumlah: {order_data.get('quantity')}\n"
                f"Pengiriman: {order_data.get('delivery_method')}\n\n"
                f"Mohon cek kembali data di atas. Jika ada yang salah, "
                f"silakan kirim ulang dengan data yang benar.\n\n"
                f"Tim kami akan menghubungi Anda untuk konfirmasi total harga. Terima kasih! 🙏"
            )
        else:
            await update.message.reply_text("Maaf, terjadi kesalahan. Silakan coba lagi.")
        return

    if is_health_question(text):
        await handle_ai_question(update, text, voice_reply=True)
        return

    from rapidfuzz import fuzz
    search_parts = text.lower().split()
    records = get_direct_prices()

    matched = []
    for r in records:
        name = str(r.get("product_name", "")).lower()
        variation = str(r.get("variation", "")).lower()
        combined = name + " " + variation
        if all(word in combined for word in search_parts):
            matched.append(r)
            continue
        score = fuzz.partial_ratio(text.lower(), combined)
        if score >= 70:
            matched.append(r)

    if not matched:
        await update.message.reply_text(f"❌ No products found for '*{text}*'", parse_mode="Markdown")
        return

    blocks = [format_product_customer(r) for r in matched]
    chunks = split_into_chunks(blocks)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="Markdown")

# ── Build PTB app ─────────────────────────────────────────────────────────────
ptb_app = Application.builder().token(TOKEN).build()
ptb_app.add_handler(CommandHandler("start",    cmd_start))
ptb_app.add_handler(CommandHandler("products", cmd_products))
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
    import sys
    if "--polling" in sys.argv:
        async def run_polling():
            await ptb_app.bot.delete_webhook()
            async with ptb_app:
                await ptb_app.start()
                await ptb_app.updater.start_polling()
                print("Bot running in polling mode... Press Ctrl+C to stop")
                await asyncio.sleep(float("inf"))
        asyncio.run(run_polling())
        sys.exit()

    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)