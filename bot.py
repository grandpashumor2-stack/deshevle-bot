import os
import asyncio
import logging
import sqlite3
import threading
import time
import random
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import parser as wb  # наш модуль парсинга WB

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
ADMIN_ID        = 6466766416
PORT            = int(os.environ.get("PORT", "8080"))
RENDER_URL      = os.environ.get("RENDER_URL", "")
DB_PATH         = os.environ.get("DB_PATH", "bot.db")
PAYMENT_TOKEN   = os.environ.get("PAYMENT_TOKEN", "390540012:LIVE:97580")

# Прокидываем affiliate ID в парсер
wb.WB_AFFILIATE_ID = os.environ.get("WB_AFFILIATE_ID", "")

ENTER_QUERY = "q"
ENTER_PRICE = "p"
BROADCAST   = "bc"

PLANS = {
    "premium": {"name": "Премиум", "price": 19900, "limit": 20,  "days": 30},
    "pro":     {"name": "Про",     "price": 49900, "limit": 999, "days": 30},
}

# ─── БД ───────────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            blocked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS watches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, query TEXT, max_price INTEGER,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, watch_id INTEGER, item_id TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS subscriptions(
            user_id INTEGER PRIMARY KEY,
            plan TEXT DEFAULT 'free',
            expires_at TEXT DEFAULT NULL);
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, plan TEXT, amount INTEGER,
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    con.close()

def db(sql, p=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, p).fetchall()]
    con.commit(); con.close()
    return rows

def add_user(uid, uname):           db("INSERT OR IGNORE INTO users(id,username) VALUES(?,?)", (uid,uname))
def add_watch(uid, q, mp):          db("INSERT INTO watches(user_id,query,max_price) VALUES(?,?,?)", (uid,q,mp))
def get_watches(uid):               return db("SELECT id,query,max_price FROM watches WHERE user_id=? AND active=1",(uid,))
def del_watch(uid, wid):            db("UPDATE watches SET active=0 WHERE id=? AND user_id=?",(wid,uid))
def all_watches():                  return db("SELECT id,user_id,query,max_price FROM watches WHERE active=1")
def was_sent(wid, iid):             return bool(db("SELECT 1 FROM notifications WHERE watch_id=? AND item_id=?",(wid,iid)))
def mark_sent(uid, wid, iid):       db("INSERT INTO notifications(user_id,watch_id,item_id) VALUES(?,?,?)",(uid,wid,iid))
def all_users():                    return [r["id"] for r in db("SELECT id FROM users WHERE blocked=0")]
def watches_count(uid):             return (db("SELECT COUNT(*) c FROM watches WHERE user_id=? AND active=1",(uid,)) or [{"c":0}])[0]["c"]
def log_payment(uid, plan, amount): db("INSERT INTO payments(user_id,plan,amount) VALUES(?,?,?)",(uid,plan,amount))

def get_sub(uid):
    if uid == ADMIN_ID:
        return {"plan":"admin","limit":999999,"active":True}
    rows = db("SELECT plan,expires_at FROM subscriptions WHERE user_id=?",(uid,))
    if not rows:
        return {"plan":"free","limit":3,"active":True}
    plan, expires = rows[0]["plan"], rows[0]["expires_at"]
    if plan == "free":
        return {"plan":"free","limit":3,"active":True}
    if expires and datetime.fromisoformat(expires) > datetime.now():
        return {"plan":plan,"limit":PLANS[plan]["limit"],"active":True,"expires":expires[:10]}
    return {"plan":"free","limit":3,"active":False,"expired":True}

def set_sub(uid, plan, days):
    expires = (datetime.now() + timedelta(days=days)).isoformat()
    db("INSERT OR REPLACE INTO subscriptions(user_id,plan,expires_at) VALUES(?,?,?)",(uid,plan,expires))

def user_limit(uid): return get_sub(uid)["limit"]

def get_stats():
    return {
        "users":        db("SELECT COUNT(*) c FROM users")[0]["c"],
        "new_today":    db("SELECT COUNT(*) c FROM users WHERE date(joined_at)=date('now')"  )[0]["c"],
        "new_week":     db("SELECT COUNT(*) c FROM users WHERE joined_at>=datetime('now','-7 days')"  )[0]["c"],
        "watches":      db("SELECT COUNT(*) c FROM watches WHERE active=1")[0]["c"],
        "notifs":       db("SELECT COUNT(*) c FROM notifications")[0]["c"],
        "paying":       db("SELECT COUNT(*) c FROM subscriptions WHERE plan!='free' AND expires_at>datetime('now')"  )[0]["c"],
        "premium":      db("SELECT COUNT(*) c FROM subscriptions WHERE plan='premium' AND expires_at>datetime('now')"  )[0]["c"],
        "pro":          db("SELECT COUNT(*) c FROM subscriptions WHERE plan='pro' AND expires_at>datetime('now')"  )[0]["c"],
        "income_month": db("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE paid_at>=datetime('now','-30 days')"  )[0]["s"],
        "income_total": db("SELECT COALESCE(SUM(amount),0) s FROM payments")[0]["s"],
    }

# ─── Форматирование ───────────────────────────────────────────────────────────

def fmt(item: dict) -> str:
    disc = ""
    if item.get("old") and item["old"] > item["price"]:
        pct  = int((1 - item["price"] / item["old"]) * 100)
        disc = f" (скидка {pct}%)"
    brand = f" [{item['brand']}]" if item.get("brand") else ""
    return (
        f"📦 *{item['name'][:65]}*{brand}\n"
        f"💰 *{item['price']:,} ₽*{disc}\n"
        f"⭐ {item.get('rating','—')}  📝 {item.get('fb',0)} отзывов\n"
        f"🔗 [Открыть на WB]({item['url']})\n"
    )

# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def main_kb(uid=None):
    rows = [
        [InlineKeyboardButton("➕ Добавить товар",    callback_data="add"),
         InlineKeyboardButton("📋 Мои отслеживания", callback_data="list")],
        [InlineKeyboardButton("🔥 Горящие скидки",   callback_data="hot"),
         InlineKeyboardButton("💎 Подписка",         callback_data="sub")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("👑 Панель администратора", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика",       callback_data="a_stats")],
        [InlineKeyboardButton("📢 Рассылка",         callback_data="a_broadcast")],
        [InlineKeyboardButton("👥 Пользователи",     callback_data="a_users")],
        [InlineKeyboardButton("💰 Доходы",           callback_data="a_income")],
        [InlineKeyboardButton("🔧 Тест WB API",      callback_data="a_wb_test")],
        [InlineKeyboardButton("◀️ Главное меню",     callback_data="menu")],
    ])

def back_admin(): return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Панель", callback_data="admin")]])
def back_btn():   return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад",  callback_data="menu")]])
def sub_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Премиум — 199 ₽/мес (20 товаров)", callback_data="buy_premium")],
        [InlineKeyboardButton("🚀 Про — 499 ₽/мес (безлимит)",       callback_data="buy_pro")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu")],
    ])

# ─── ХЕНДЛЕРЫ ─────────────────────────────────────────────────────────────────

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    add_user(uid, u.effective_user.username or "")
    sub = get_sub(uid)
    if uid == ADMIN_ID:          pl = "👑 Администратор — безлимит"
    elif sub["plan"] == "free":  pl = "🆓 Бесплатно: до 3 отслеживаний"
    else:                        pl = f"💎 {sub['plan'].title()}: до {sub.get('expires','')}"
    await u.message.reply_text(
        f"👋 Привет! Слежу за ценами на *Wildberries* и уведомляю когда товар дешевеет.\n\n"
        f"{pl}\n\nВыбери действие:",
        parse_mode="Markdown", reply_markup=main_kb(uid))

async def btn(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    d = q.data; uid = q.from_user.id

    # ── Главное меню ──────────────────────────────────────────────────────────
    if d == "menu":
        sub = get_sub(uid)
        if uid == ADMIN_ID:         pl = "👑 Администратор"
        elif sub["plan"] == "free": pl = "🆓 Бесплатно: 3 отслеживания"
        else:                       pl = f"💎 {sub['plan'].title()} до {sub.get('expires','')}"
        await q.edit_message_text(f"🏠 *Главное меню*\n{pl}",
                                  parse_mode="Markdown", reply_markup=main_kb(uid))

    # ── Добавить товар ────────────────────────────────────────────────────────
    elif d == "add":
        if watches_count(uid) >= user_limit(uid):
            if uid == ADMIN_ID:
                await q.edit_message_text("⚠️ Технический лимит.", reply_markup=back_btn()); return
            await q.edit_message_text(
                f"⚠️ *Достигнут лимит {user_limit(uid)} отслеживания*\n\n"
                "💎 Премиум — 199 ₽/мес — до 20 товаров\n"
                "🚀 Про — 499 ₽/мес — безлимит",
                parse_mode="Markdown", reply_markup=sub_kb()); return
        c.user_data["s"] = ENTER_QUERY
        await q.edit_message_text(
            "🔍 *Напиши название товара:*\n\n"
            "_Например: наушники Marshall Major, кроссовки Nike Air Max_\n\n"
            "💡 Чем точнее запрос — тем лучше результат",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="menu")]]))

    # ── Список отслеживаний ───────────────────────────────────────────────────
    elif d == "list":
        ws  = get_watches(uid)
        lim = "∞" if uid == ADMIN_ID else str(get_sub(uid)["limit"])
        if not ws:
            await q.edit_message_text("📋 Нет отслеживаний.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Добавить", callback_data="add")]])); return
        txt  = f"📋 *Отслеживания ({len(ws)}/{lim}):*\n\n"
        btns = []
        for w in ws:
            txt += f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
            btns.append([InlineKeyboardButton(f"❌ {w['query'][:28]}", callback_data=f"d_{w['id']}")])
        btns.append([InlineKeyboardButton("➕ Добавить", callback_data="add"),
                     InlineKeyboardButton("◀️ Назад",    callback_data="menu")])
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("d_"):
        del_watch(uid, int(d[2:]))
        ws  = get_watches(uid)
        lim = "∞" if uid == ADMIN_ID else str(get_sub(uid)["limit"])
        txt  = "✅ Удалено!\n\n"
        btns = []
        if ws:
            txt += f"📋 *Отслеживания ({len(ws)}/{lim}):*\n\n"
            for w in ws:
                txt += f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
                btns.append([InlineKeyboardButton(f"❌ {w['query'][:28]}", callback_data=f"d_{w['id']}")])
        btns.append([InlineKeyboardButton("➕ Добавить", callback_data="add"),
                     InlineKeyboardButton("◀️ Назад",    callback_data="menu")])
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    # ── Горящие скидки ────────────────────────────────────────────────────────
    elif d == "hot":
        await q.edit_message_text("⏳ Ищу горящие скидки на WB...")
        hot = wb.hot_deals()
        if not hot:
            await q.edit_message_text(
                "😔 Горящих скидок сейчас нет.\nПопробуй через несколько минут!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Обновить", callback_data="hot"),
                    InlineKeyboardButton("◀️ Назад",    callback_data="menu")]])); return
        txt = "🔥 *Горящие скидки на WB:*\n\n"
        for item in hot[:5]: txt += fmt(item) + "\n"
        await q.edit_message_text(txt, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить", callback_data="hot"),
                InlineKeyboardButton("◀️ Назад",    callback_data="menu")]]))

    # ── Подписка ──────────────────────────────────────────────────────────────
    elif d == "sub":
        sub = get_sub(uid)
        if uid == ADMIN_ID:
            await q.edit_message_text("👑 Ты администратор — безлимит навсегда!", reply_markup=back_btn()); return
        if sub["plan"] != "free" and sub.get("active"):
            await q.edit_message_text(
                f"💎 *{sub['plan'].title()} активна*\n\n📅 До: {sub.get('expires','')}\n📦 Лимит: {sub['limit']}",
                parse_mode="Markdown", reply_markup=back_btn())
        else:
            exp = "\n⚠️ _Подписка истекла — оформи снова!_\n" if sub.get("expired") else ""
            await q.edit_message_text(
                f"💎 *Тарифы:*{exp}\n\n"
                "🆓 Бесплатно — 3 отслеживания\n"
                "💎 Премиум — 199 ₽/мес — 20 отслеживаний\n"
                "🚀 Про — 499 ₽/мес — безлимит\n\n"
                "Оплата: карта, СБП, ЮMoney",
                parse_mode="Markdown", reply_markup=sub_kb())

    elif d in ("buy_premium", "buy_pro"):
        plan_key = d.replace("buy_", "")
        plan = PLANS[plan_key]
        await c.bot.send_invoice(
            chat_id=uid,
            title=f"Подписка {plan['name']} — 30 дней",
            description=f"До {plan['limit']} отслеживаний товаров на Wildberries",
            payload=plan_key, provider_token=PAYMENT_TOKEN, currency="RUB",
            prices=[LabeledPrice(plan["name"], plan["price"])],
            start_parameter=f"sub_{plan_key}")

    # ── Панель администратора ─────────────────────────────────────────────────
    elif d == "admin":
        if uid != ADMIN_ID: return
        await q.edit_message_text("👑 *Панель администратора*",
                                  parse_mode="Markdown", reply_markup=admin_kb())

    elif d == "a_stats":
        if uid != ADMIN_ID: return
        s = get_stats()
        conv = s["paying"] / max(s["users"], 1) * 100
        await q.edit_message_text(
            f"📊 *Статистика*\n\n"
            f"👥 Пользователей: *{s['users']}*\n"
            f"   Сегодня: +{s['new_today']}  |  Неделя: +{s['new_week']}\n\n"
            f"💎 Платящих: *{s['paying']}* ({conv:.1f}%)\n"
            f"   Премиум: {s['premium']}  |  Про: {s['pro']}\n\n"
            f"📦 Отслеживаний: *{s['watches']}*\n"
            f"🔔 Уведомлений: *{s['notifs']}*\n\n"
            f"💰 За месяц: *{s['income_month']//100:,} ₽*\n"
            f"💰 Всего: *{s['income_total']//100:,} ₽*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить", callback_data="a_stats"),
                InlineKeyboardButton("◀️ Назад",    callback_data="admin")]]))

    elif d == "a_income":
        if uid != ADMIN_ID: return
        s    = get_stats()
        pays = db("SELECT p.plan,p.amount,p.paid_at,u.username "
                  "FROM payments p LEFT JOIN users u ON u.id=p.user_id "
                  "ORDER BY p.paid_at DESC LIMIT 10")
        text = (f"💰 *Доходы*\n\n"
                f"Месяц: *{s['income_month']//100:,} ₽*\n"
                f"Всего: *{s['income_total']//100:,} ₽*\n\n"
                f"📋 *Последние платежи:*\n")
        if pays:
            for p in pays:
                un = f"@{p['username']}" if p["username"] else "—"
                text += f"  {p['paid_at'][:10]} {un} — {p['plan']} {p['amount']//100}₽\n"
        else:
            text += "_Платежей пока нет_"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_admin())

    elif d == "a_users":
        if uid != ADMIN_ID: return
        users = db("SELECT id,username,joined_at FROM users ORDER BY joined_at DESC LIMIT 15")
        text  = "👥 *Последние пользователи:*\n\n"
        for usr in users:
            un    = f"@{usr['username']}" if usr["username"] else f"id:{usr['id']}"
            badge = "💎" if get_sub(usr["id"])["plan"] != "free" else ""
            text += f"  {usr['joined_at'][:10]} {un} {badge}\n"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_admin())

    elif d == "a_broadcast":
        if uid != ADMIN_ID: return
        c.user_data["s"] = BROADCAST
        await q.edit_message_text(
            f"📢 *Рассылка*\n\nПолучателей: {len(all_users())}\n\nНапиши текст:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="admin")]]))

    elif d == "a_wb_test":
        if uid != ADMIN_ID: return
        await q.edit_message_text("⏳ Проверяю WB API...")
        t0       = time.time()
        results  = wb.search("наушники", 10000)
        elapsed  = round(time.time() - t0, 2)
        if results:
            example = results[0]
            text = (f"✅ *WB API работает!*\n\n"
                    f"⏱ Время: {elapsed}с\n"
                    f"📦 Найдено: {len(results)} товаров\n\n"
                    f"Пример:\n{fmt(example)}")
        else:
            text = (f"❌ *WB API не отвечает*\n\n"
                    f"⏱ Время: {elapsed}с\n\n"
                    f"Возможные причины:\n"
                    f"• WB временно заблокировал IP сервера\n"
                    f"• Временные проблемы на стороне WB\n\n"
                    f"_Обычно восстанавливается через 5-15 минут_")
        await q.edit_message_text(text, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Проверить снова", callback_data="a_wb_test"),
                InlineKeyboardButton("◀️ Назад",           callback_data="admin")]]))

# ─── Оплата ───────────────────────────────────────────────────────────────────

async def precheckout(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.pre_checkout_query.answer(ok=True)

async def payment_success(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid      = u.effective_user.id
    plan_key = u.message.successful_payment.invoice_payload
    plan     = PLANS.get(plan_key)
    if not plan: return
    set_sub(uid, plan_key, plan["days"])
    log_payment(uid, plan_key, plan["price"])
    expires = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")
    await u.message.reply_text(
        f"🎉 *Оплата прошла!*\n\n"
        f"✅ *{plan['name']}* активирована\n"
        f"📦 {plan['limit']} отслеживаний\n"
        f"📅 Активна до: {expires}",
        parse_mode="Markdown", reply_markup=main_kb(uid))
    try:
        un = u.effective_user.username or str(uid)
        await c.bot.send_message(ADMIN_ID,
            f"💰 *Новая оплата!*\n\n@{un} (id:{uid})\n{plan['name']} — {plan['price']//100}₽",
            parse_mode="Markdown")
    except: pass

# ─── Сообщения ────────────────────────────────────────────────────────────────

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid  = u.effective_user.id
    text = u.message.text.strip()
    s    = c.user_data.get("s")

    # Рассылка
    if s == BROADCAST and uid == ADMIN_ID:
        c.user_data["s"] = None
        users = all_users()
        m     = await u.message.reply_text(f"⏳ Отправляю {len(users)} пользователям...")
        sent  = 0
        for user_id in users:
            try:
                await c.bot.send_message(user_id, text, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.05)
            except: pass
        await m.edit_text(f"✅ Готово! {sent}/{len(users)}", reply_markup=back_admin())
        return

    # Шаг 1 — название товара
    if s == ENTER_QUERY:
        c.user_data["q"] = text
        c.user_data["s"] = ENTER_PRICE
        await u.message.reply_text(
            f"💰 Товар: *{text}*\n\n"
            f"Укажи максимальную цену в рублях:\n_Например: 5000_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="menu")]]))

    # Шаг 2 — цена → поиск
    elif s == ENTER_PRICE:
        try:
            mp = int(text.replace(" ","").replace(",","").replace(".",""))
            if mp <= 0: raise ValueError
        except:
            await u.message.reply_text("⚠️ Введи число, например: 5000"); return

        query = c.user_data.get("q", "")
        c.user_data["s"] = None
        add_watch(uid, query, mp)

        m = await u.message.reply_text(
            f"⏳ Ищу *{query}* до *{mp:,} ₽* на WB...",
            parse_mode="Markdown")

        # Поиск в отдельном потоке чтобы не блокировать бот
        results = await asyncio.get_event_loop().run_in_executor(
            None, wb.search, query, mp)

        if results:
            reply = (f"✅ *Отслеживание добавлено!*\n\n"
                     f"🎯 Нашёл *{len(results)} товаров* до {mp:,} ₽:\n\n")
            for item in results[:3]:
                reply += fmt(item) + "\n"
            reply += f"_Проверяю каждые 30 мин — сообщу о новых товарах_"
        else:
            reply = (f"✅ *Отслеживание добавлено!*\n\n"
                     f"📦 *{query}* — до *{mp:,} ₽*\n\n"
                     f"Сейчас на WB таких товаров нет в этом ценовом диапазоне.\n"
                     f"🔔 Как только появится — сразу напишу!")

        await m.edit_text(reply, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Мои отслеживания", callback_data="list"),
                InlineKeyboardButton("➕ Добавить ещё",     callback_data="add")]]))
    else:
        await u.message.reply_text("Используй кнопки меню 👇", reply_markup=main_kb(uid))

# ─── Планировщик ──────────────────────────────────────────────────────────────

async def scheduler(app):
    await asyncio.sleep(120)
    while True:
        ws = all_watches()
        logger.info(f"Scheduler: {len(ws)} отслеживаний")
        sent_total = 0
        for w in ws:
            try:
                results = await asyncio.get_event_loop().run_in_executor(
                    None, wb.search, w["query"], w["max_price"])
                for item in results[:5]:
                    if was_sent(w["id"], item["id"]): continue
                    disc = ""
                    if item.get("old") and item["old"] > item["price"]:
                        pct  = int((1 - item["price"]/item["old"])*100)
                        disc = f" со скидкой *{pct}%*"
                    txt = (f"🔔 *Нашёл товар по твоей цене{disc}!*\n\n"
                           f"🔍 Запрос: _{w['query']}_\n\n") + fmt(item)
                    try:
                        await app.bot.send_message(
                            w["user_id"], txt,
                            parse_mode="Markdown",
                            disable_web_page_preview=True)
                        mark_sent(w["user_id"], w["id"], item["id"])
                        sent_total += 1
                    except Exception as e:
                        logger.warning(f"Не смог отправить {w['user_id']}: {e}")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Ошибка watch {w['id']}: {e}")
        logger.info(f"Scheduler: отправлено {sent_total} уведомлений")
        await asyncio.sleep(1800)

# ─── Keep-alive ───────────────────────────────────────────────────────────────

async def keep_alive():
    await asyncio.sleep(30)
    while True:
        try:
            if RENDER_URL:
                r = requests.get(RENDER_URL, timeout=10)
                logger.info(f"Keep-alive: {r.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive error: {e}")
        await asyncio.sleep(60)

# ─── Веб-сервер ───────────────────────────────────────────────────────────────

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"OK - DeshevleBot alive!")
    def log_message(self, *a): pass

def run_web():
    HTTPServer(("0.0.0.0", PORT), WebHandler).serve_forever()

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    init_db()
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_success))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        asyncio.create_task(scheduler(app))
        asyncio.create_task(keep_alive())
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
