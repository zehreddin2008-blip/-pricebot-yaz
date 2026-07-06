import os
import re
import json
import threading
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pricebot")

DATA_FILE = "tracked.json"
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHECK_INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MIN", "30"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# ---------- basit JSON depolama ----------
def load_data():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(items):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ---------- fiyat çekme ----------
PRICE_SELECTORS = [
    {"attrs": {"itemprop": "price"}},
    {"attrs": {"property": "og:price:amount"}},
    {"attrs": {"class": re.compile(r"(price|fiyat)", re.I)}},
]

def extract_price(html: str):
    soup = BeautifulSoup(html, "lxml")

    for sel in PRICE_SELECTORS:
        tag = soup.find(attrs=sel["attrs"])
        if tag:
            text = tag.get("content") or tag.get_text()
            price = parse_price_text(text)
            if price:
                return price

    # son çare: sayfadaki "1.234,56 TL" gibi ilk fiyat benzeri metni bul
    match = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?:TL|₺)", html)
    if match:
        return parse_price_text(match.group(1))
    return None

def parse_price_text(text: str):
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\d]", "", text)
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None

def get_price(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return extract_price(resp.text)

# ---------- Telegram komutları ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba! Fiyat takip botu hazır.\n\n"
        "/ekle <link> <hedef_fiyat> - ürünü takibe al\n"
        "/liste - takip ettiklerini göster\n"
        "/sil <id> - takibi kaldır"
    )

async def ekle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Kullanım: /ekle <link> <hedef_fiyat>")
        return
    url = context.args[0]
    try:
        target = float(context.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Hedef fiyat sayı olmalı, örn: 499.90")
        return

    await update.message.reply_text("Fiyat kontrol ediliyor, birazcık bekle...")
    try:
        price = get_price(url)
    except Exception as e:
        await update.message.reply_text(f"Bu siteden fiyat okunamadı: {e}")
        return

    items = load_data()
    new_id = (max([i["id"] for i in items], default=0)) + 1
    items.append({
        "id": new_id,
        "chat_id": update.effective_chat.id,
        "url": url,
        "target_price": target,
        "last_price": price,
        "added_at": datetime.utcnow().isoformat(),
    })
    save_data(items)

    if price is None:
        await update.message.reply_text(
            f"#{new_id} eklendi ama şu an fiyat okunamadı, takibe devam edeceğim."
        )
    else:
        await update.message.reply_text(
            f"#{new_id} eklendi.\nŞu anki fiyat: {price} TL\nHedef: {target} TL"
        )

async def liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = [i for i in load_data() if i["chat_id"] == update.effective_chat.id]
    if not items:
        await update.message.reply_text("Takip ettiğin ürün yok.")
        return
    lines = []
    for i in items:
        lines.append(
            f"#{i['id']} | hedef: {i['target_price']} TL | son fiyat: {i.get('last_price')} TL\n{i['url']}"
        )
    await update.message.reply_text("\n\n".join(lines))

async def sil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: /sil <id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçerli bir id gir.")
        return
    items = load_data()
    new_items = [i for i in items if not (i["id"] == target_id and i["chat_id"] == update.effective_chat.id)]
    if len(new_items) == len(items):
        await update.message.reply_text("Böyle bir kayıt bulamadım.")
    else:
        save_data(new_items)
        await update.message.reply_text(f"#{target_id} silindi.")

# ---------- periyodik fiyat kontrolü ----------
async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    items = load_data()
    changed = False
    for item in items:
        try:
            price = get_price(item["url"])
        except Exception as e:
            log.warning(f"Fiyat okunamadı ({item['url']}): {e}")
            continue
        if price is None:
            continue
        if price != item.get("last_price"):
            item["last_price"] = price
            changed = True
        if price <= item["target_price"]:
            await context.bot.send_message(
                chat_id=item["chat_id"],
                text=f"🎉 Fiyat düştü!\n{item['url']}\nGüncel fiyat: {price} TL (hedef: {item['target_price']} TL)"
            )
    if changed:
        save_data(items)

# ---------- Render için basit web sunucusu ----------
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return "Fiyat takip botu çalışıyor."

def run_bot():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ekle", ekle))
    application.add_handler(CommandHandler("liste", liste))
    application.add_handler(CommandHandler("sil", sil))

    application.job_queue.run_repeating(check_prices, interval=CHECK_INTERVAL_MIN * 60, first=30)

    application.run_polling(stop_signals=None)

def main():
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
