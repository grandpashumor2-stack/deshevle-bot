import os
import asyncio
import logging
import sqlite3
import requests
import threading
import time
import random
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
PORT = int(os.environ.get("PORT", "8080"))
RENDER_URL = os.environ.get("RENDER_URL", "")
DB_PATH = os.environ.get("DB_PATH", "bot.db")
WB_AFFILIATE_ID = os.environ.get("WB_AFFILIATE_ID", "")

ENTER_QUERY = "ENTER_QUERY"
ENTER_PRICE = "ENTER_PRICE"

HOT_QUERIES = ["наушники", "кроссовки", "смартфон", "куртка", "ноутбук", "часы", "рюкзак"]

# ─── База данных ──────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, username TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS watches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, query TEXT, max_price INTEGER, active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, watch_id INTEGER, item_id TEXT)""")
    conn.commit()
    conn.close()

def db_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def add_user(uid, username):
    db_query("INSERT OR IGNORE INTO users (id, username) VALUES (?,?)", (uid, username))

def add_watch(uid, query, max_price):
    db_query("INSERT INTO watches (user_id, query, max_price) VALUES (?,?,?)", (uid, query, max_price))

def get_watches(uid):
    return db_query("SELECT id, query, max_price FROM watches WHERE user_id=? AND active=1", (uid,))

def get_watches_count(uid):
    r = db_query("SELECT COUNT(*) as c FROM watches WHERE user_id=? AND active=1", (uid,))
    return r[0]["c"] if r else 0

def delete_watch(uid, wid):
    db_query("UPDATE watches SET active=0 WHERE id=? AND user_id=?", (wid, uid))

def get_all_active_watches():
    return db_query("SELECT id, user_id, query, max_price FROM watches WHERE active=1")

def was_notified(watch_id, item_id):
    return bool(db_query("SELECT 1 FROM notifications WHERE watch_id=? AND item_id=?", (watch_id, item_id)))

def mark_notified(uid, watch_id, item_id):
    db_query("INSERT INTO notifications (user_id, watch_id, item_id) VALUES (?,?,?)", (uid, watch_id, item_id))

def get_stats():
    u = db_query("SELECT COUNT(*) as c FROM users")[0]["c"]
    w = db_query("SELECT COUNT(*) as c FROM watches WHERE active=1")[0]["c"]
    n = db_query("SELECT COUNT(*) as c FROM notifications")[0]["c"]
    return {"users": u, "watches": w, "notifications": n}

def get_all_users():
    return [r["id"] for r in db_query("SELECT id FROM users")]

# ─── Парсер WB ────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru", "Referer": "https://www.wildberries.ru/",
}

def make_url(article_id):
    base = f"https://www.wildberries.ru/catalog/{article_id}/detail.aspx"
    if WB_AFFILIATE_ID:
        return f"{base}?utm_source=affiliate&utm_medium=cpa&utm_campaign={WB_AFFILIATE_ID}"
    return base

def extract_price(p):
    try:
        sizes = p.get("sizes", [])
        if sizes:
            v = sizes[0].get("price", {}).get("total", 0)
            if v: return v // 100
        v = p.get("salePriceU", 0)
        if v: return v // 100
    except: pass
    return None

def extract_old_price(p):
    try:
        sizes = p.get("sizes", [])
        if sizes:
            v = sizes[0].get("price", {}).get("basic", 0)
            if v: return v // 100
        v = p.get("priceU", 0)
        if v: return v // 100
    except: pass
    return None

def parse_product(p, price):
    article = p.get("id", 0)
    return {"id": str(article), "name": p.get("name", "Товар"), "price": price,
            "old_price": extract_old_price(p), "rating": p.get("reviewRating", 0),
            "feedbacks": p.get("feedbacks", 0), "url": make_url(article)}

def wb_search(query, max_price):
    for v in ("v9", "v7", "v5"):
        try:
            url = f"https://search.wb.ru/exactmatch/ru/common/{v}/search"
            params = {"query": query, "resultset": "catalog", "limit": 20, "sort": "priceup",
                      "page": 1, "appType": 1, "curr": "rub", "lang": "ru",
                      "dest": -1257786, "priceU": max_price * 100}
            r = requests.get(url, params=params, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                products = r.json().get("data", {}).get("products", [])
                result = []
                for p in products:
                    price = extract_price(p)
                    if price and price <= max_price:
                        result.append(parse_product(p, price))
                return result
        except Exception as e:
            logger.warning(f"WB search error: {e}")
    return []

def wb_hot_deals():
    query = random.choice(HOT_QUERIES)
    for v in ("v9", "v7", "v5"):
        try:
            url = f"https://search.wb.ru/exactmatch/ru/common/{v}/search"
            params = {"query": query, "resultset": "catalog", "limit": 30, "sort": "popular",
                      "page": 1, "appType": 1, "curr": "rub", "lang": "ru",
                      "dest": -1257786, "discount": 25}
            r = requests.get(url, params=params, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                products = r.json().get("data", {}).get("products", [])
                result = []
                for p in products:
                    price = extract_price(p)
                    old = extract_old_price(p)
                    if price and old and old > price and (1 - price/old) >= 0.25:
                        item = parse_product(p, price)
                        item["old_price"] = old
                        result.append(item)
                result.sort(key=lambda x: -(x.get("old_price", 0) - x["price"]))
                return result[:8]
        except Exception as e:
            logger.warning(f"WB hot deals error: {e}")
    return []

def format_item(item):
    discount = ""
    if item.get("old_price") and item["old_price"] > item["price"]:
        pct = int((1 - item["price"] / item["old_price"]) * 100)
        discount = f" (−{pct}%)"
    return (
        f"📦 *{item['name'][:60]}*\n"
        f"💰 *{item['price']:,} ₽*{discount}\n"
        f"⭐ {item.get('rating','—')}  📝 {item.get('feedbacks',0)} отзывов\n"
        f"🔗 [Открыть на WB]({item['url']})\n"
    )

# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить товар", callback_data="add_watch"),
         InlineKeyboardButton("📋 Мои отслеживания", callback_data="my_watches")],
        [InlineKeyboardButton("🔥 Горящие скидки", callback_data="hot_deals"),
         InlineKeyboardButton("ℹ️ Как работает", callback_data="how_it_works")],
    ])

# ─── Хендлеры ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_user(uid, update.effective_user.username or "")
    await update.message.reply_text(
        "👋 Привет! Я слежу за ценами на Wildberries и сообщаю когда товар подешевел.\n\n"
        "🆓 Бесплатно: до 3 отслеживаний\n"
        "💎 Премиум: до 20 отслеживаний (скоро)\n\n"
        "Выбери действие:", reply_markup=main_kb()
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    s = get_stats()
    await update.message.reply_text(
        f"📊 *Статистика:*\n\n👥 Пользователей: {s['users']}\n"
        f"👁 Отслеживаний: {s['watches']}\n🔔 Уведомлений: {s['notifications']}",
        parse_mode="Markdown"
    )

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    text = update.message.text.replace("/broadcast", "").strip()
    if not text:
        await update.message.reply_text("Использование: /broadcast Текст")
        return
    users = get_all_users()
    sent = 0
    for uid in users:
        try:
            await ctx.bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"✅ Отправлено {sent}/{len(users)}")

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "back_to_main":
        await q.edit_message_text("🏠 *Главное меню*\n\nВыбери действие:",
                                  parse_mode="Markdown", reply_markup=main_kb())

    elif data == "how_it_works":
        await q.edit_message_text(
            "📖 *Как пользоваться ботом:*\n\n"
            "1️⃣ Нажми «Добавить товар»\n"
            "2️⃣ Введи название товара\n"
            "3️⃣ Укажи максимальную цену\n"
            "4️⃣ Бот проверяет WB каждые 30 минут\n"
            "5️⃣ Как только найдёт дешевле — напишет!\n\n"
            "🔗 Ссылки партнёрские — покупаешь по той же цене, бот получает %",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]])
        )

    elif data == "add_watch":
        if get_watches_count(uid) >= 3:
            await q.edit_message_text(
                "⚠️ У тебя уже 3 отслеживания.\nУдали одно чтобы добавить новое.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Мои отслеживания", callback_data="my_watches")]])
            )
            return
        ctx.user_data["state"] = ENTER_QUERY
        await q.edit_message_text(
            "🔍 Напиши название товара:\n\n_Например: кроссовки Nike, наушники Sony_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back_to_main")]])
        )

    elif data == "my_watches":
        watches = get_watches(uid)
        if not watches:
            await q.edit_message_text("📋 Нет отслеживаний. Добавь первый товар!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Добавить товар", callback_data="add_watch")]]))
            return
        text = f"📋 *Твои отслеживания ({len(watches)}/3):*\n\n"
        buttons = []
        for w in watches:
            text += f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
            buttons.append([InlineKeyboardButton(f"❌ {w['query'][:25]}", callback_data=f"del_{w['id']}")])
        buttons.append([InlineKeyboardButton("➕ Добавить", callback_data="add_watch"),
                        InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")])
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("del_"):
        wid = int(data.split("_")[1])
        delete_watch(uid, wid)
        watches = get_watches(uid)
        if not watches:
            await q.edit_message_text("✅ Удалено! Отслеживаний больше нет.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Добавить товар", callback_data="add_watch"),
                    InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]]))
            return
        text = f"✅ Удалено!\n\n📋 *Отслеживания ({len(watches)}/3):*\n\n"
        buttons = []
        for w in watches:
            text += f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
            buttons.append([InlineKeyboardButton(f"❌ {w['query'][:25]}", callback_data=f"del_{w['id']}")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")])
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "hot_deals":
        await q.edit_message_text("⏳ Ищу горящие скидки...")
        hot = wb_hot_deals()
        if not hot:
            await q.edit_message_text("😔 Горящих скидок сейчас нет. Попробуй позже!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]]))
            return
        text = "🔥 *Горящие скидки прямо сейчас:*\n\n"
        for item in hot[:5]:
            text += format_item(item) + "\n"
        await q.edit_message_text(text, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить", callback_data="hot_deals"),
                InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]]))

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    state = ctx.user_data.get("state")

    if state == ENTER_QUERY:
        ctx.user_data["query"] = text
        ctx.user_data["state"] = ENTER_PRICE
        await update.message.reply_text(
            f"💰 Товар: *{text}*\n\nУкажи максимальную цену в рублях.\n_Например: 5000_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back_to_main")]])
        )

    elif state == ENTER_PRICE:
        try:
            max_price = int(text.replace(" ", "").replace(",", ""))
            if max_price <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Введи корректную цену (только цифры, например: 5000)")
            return

        query = ctx.user_data.get("query", "")
        ctx.user_data["state"] = None
        add_watch(uid, query, max_price)

        msg = await update.message.reply_text("⏳ Ищу товары прямо сейчас...")
        results = wb_search(query, max_price)

        if results:
            reply = f"✅ Отслеживание добавлено!\n\n🎯 Нашёл {len(results)} товаров до {max_price:,} ₽:\n\n"
            for item in results[:3]:
                reply += format_item(item) + "\n"
        else:
            reply = (f"✅ Отслеживание добавлено!\n\n📦 Товар: *{query}*\n"
                     f"💰 Максимум: *{max_price:,} ₽*\n\nСейчас подходящих нет, но я слежу!")

        await msg.edit_text(reply, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Мои отслеживания", callback_data="my_watches"),
                InlineKeyboardButton("➕ Ещё", callback_data="add_watch")]]))
    else:
        await update.message.reply_text("Используй кнопки меню 👇", reply_markup=main_kb())

# ─── Планировщик ──────────────────────────────────────────────────────────────

def scheduler_loop(app):
    time.sleep(60)
    while True:
        try:
            watches = get_all_active_watches()
            logger.info(f"Checking {len(watches)} watches...")
            for w in watches:
                try:
                    results = wb_search(w["query"], w["max_price"])
                    for item in results[:3]:
                        if was_notified(w["id"], item["id"]): continue
                        text = f"🔔 *Нашёл товар по твоей цене!*\n\n🔍 Запрос: _{w['query']}_\n" + format_item(item)
                        try:
                            asyncio.run_coroutine_threadsafe(
                                app.bot.send_message(w["user_id"], text,
                                    parse_mode="Markdown", disable_web_page_preview=True),
                                app.loop
                            ).result(timeout=10)
                            mark_notified(w["user_id"], w["id"], item["id"])
                        except Exception as e:
                            logger.warning(f"Send error: {e}")
                    time.sleep(2)
                except Exception as e:
                    logger.error(f"Watch {w['id']} error: {e}")
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        time.sleep(30 * 60)

# ─── Keep-alive ───────────────────────────────────────────────────────────────

def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            if RENDER_URL:
                r = requests.get(RENDER_URL, timeout=10)
                logger.info(f"Keep-alive ping: {r.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive error: {e}")
        time.sleep(60)

# ─── Веб-сервер ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - DeshevleBot is alive!")
    def log_message(self, *args): pass

def run_web():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    threading.Thread(target=scheduler_loop, args=(app,), daemon=True).start()

    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
