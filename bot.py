import os
import asyncio
import logging
import sqlite3
import requests
import threading
import time
import random
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
ADMIN_ID        = 6466766416
PORT            = int(os.environ.get("PORT", "8080"))
RENDER_URL      = os.environ.get("RENDER_URL", "")
DB_PATH         = os.environ.get("DB_PATH", "bot.db")
WB_AFFILIATE_ID = os.environ.get("WB_AFFILIATE_ID", "")
PAYMENT_TOKEN   = os.environ.get("PAYMENT_TOKEN", "390540012:LIVE:97580")

HOT_QUERIES = ["наушники","кроссовки","смартфон","куртка","ноутбук","часы","рюкзак","платье","планшет"]
ENTER_QUERY  = "q"
ENTER_PRICE  = "p"
BROADCAST    = "bc"

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
            user_id INTEGER, query TEXT, max_price INTEGER, active INTEGER DEFAULT 1,
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

def add_user(uid, uname):     db("INSERT OR IGNORE INTO users(id,username) VALUES(?,?)", (uid, uname))
def add_watch(uid, q, mp):    db("INSERT INTO watches(user_id,query,max_price) VALUES(?,?,?)", (uid, q, mp))
def get_watches(uid):         return db("SELECT id,query,max_price FROM watches WHERE user_id=? AND active=1", (uid,))
def del_watch(uid, wid):      db("UPDATE watches SET active=0 WHERE id=? AND user_id=?", (wid, uid))
def all_watches():            return db("SELECT id,user_id,query,max_price FROM watches WHERE active=1")
def was_sent(wid, iid):       return bool(db("SELECT 1 FROM notifications WHERE watch_id=? AND item_id=?", (wid, iid)))
def mark_sent(uid, wid, iid): db("INSERT INTO notifications(user_id,watch_id,item_id) VALUES(?,?,?)", (uid, wid, iid))
def all_users():              return [r["id"] for r in db("SELECT id FROM users WHERE blocked=0")]
def watches_count(uid):       return (db("SELECT COUNT(*) c FROM watches WHERE user_id=? AND active=1", (uid,)) or [{"c":0}])[0]["c"]
def log_payment(uid, plan, amount): db("INSERT INTO payments(user_id,plan,amount) VALUES(?,?,?)", (uid, plan, amount))

def get_sub(uid):
    if uid == ADMIN_ID:
        return {"plan": "admin", "limit": 999999, "active": True}
    rows = db("SELECT plan, expires_at FROM subscriptions WHERE user_id=?", (uid,))
    if not rows:
        return {"plan": "free", "limit": 3, "active": True}
    row = rows[0]
    plan, expires = row["plan"], row["expires_at"]
    if plan == "free":
        return {"plan": "free", "limit": 3, "active": True}
    if expires and datetime.fromisoformat(expires) > datetime.now():
        return {"plan": plan, "limit": PLANS[plan]["limit"], "active": True, "expires": expires[:10]}
    return {"plan": "free", "limit": 3, "active": False, "expired": True}

def set_sub(uid, plan, days):
    expires = (datetime.now() + timedelta(days=days)).isoformat()
    db("INSERT OR REPLACE INTO subscriptions(user_id,plan,expires_at) VALUES(?,?,?)", (uid, plan, expires))

def user_limit(uid):
    return get_sub(uid)["limit"]

def get_stats():
    total_users  = db("SELECT COUNT(*) c FROM users")[0]["c"]
    new_today    = db("SELECT COUNT(*) c FROM users WHERE date(joined_at)=date('now')"  )[0]["c"]
    new_week     = db("SELECT COUNT(*) c FROM users WHERE joined_at>=datetime('now','-7 days')"  )[0]["c"]
    total_watches= db("SELECT COUNT(*) c FROM watches WHERE active=1")[0]["c"]
    notifications= db("SELECT COUNT(*) c FROM notifications")[0]["c"]
    paying       = db("SELECT COUNT(*) c FROM subscriptions WHERE plan!='free' AND expires_at>datetime('now')"  )[0]["c"]
    premium      = db("SELECT COUNT(*) c FROM subscriptions WHERE plan='premium' AND expires_at>datetime('now')"  )[0]["c"]
    pro          = db("SELECT COUNT(*) c FROM subscriptions WHERE plan='pro' AND expires_at>datetime('now')"  )[0]["c"]
    month_income = db("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE paid_at>=datetime('now','-30 days')"  )[0]["s"]
    total_income = db("SELECT COALESCE(SUM(amount),0) s FROM payments")[0]["s"]
    return {"total_users":total_users,"new_today":new_today,"new_week":new_week,
            "total_watches":total_watches,"notifications":notifications,
            "paying":paying,"premium":premium,"pro":pro,
            "month_income":month_income,"total_income":total_income}

# ─── Фильтр релевантности ─────────────────────────────────────────────────────

def is_relevant(item_name: str, query: str) -> bool:
    """Проверяем что товар соответствует запросу пользователя."""
    name = item_name.lower()
    words = [w for w in query.lower().split() if len(w) > 2]
    if not words:
        return True
    matches = sum(1 for w in words if w in name)
    # Достаточно совпадения хотя бы половины слов
    return matches >= max(1, len(words) // 2)

# ─── WB парсер ────────────────────────────────────────────────────────────────

H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36",
     "Accept": "*/*", "Origin": "https://www.wildberries.ru", "Referer": "https://www.wildberries.ru/"}

def wb_url(aid):
    u = f"https://www.wildberries.ru/catalog/{aid}/detail.aspx"
    return f"{u}?utm_source=affiliate&utm_campaign={WB_AFFILIATE_ID}" if WB_AFFILIATE_ID else u

def price(p, key="total"):
    try:
        s = p.get("sizes", [])
        v = (s[0].get("price", {}).get(key, 0) if s else 0) or p.get("salePriceU" if key=="total" else "priceU", 0)
        return v // 100 if v else None
    except: return None

def to_item(p, pr):
    return {"id": str(p.get("id",0)), "name": p.get("name",""), "price": pr,
            "old": price(p,"basic"), "rating": p.get("reviewRating",0),
            "fb": p.get("feedbacks",0), "url": wb_url(p.get("id",0))}

def wb_req(query, extra=None):
    params = {"query": query, "resultset": "catalog", "limit": 30, "appType": 1,
              "curr": "rub", "lang": "ru", "dest": -1257786, **(extra or {})}
    for v in ("v9", "v7", "v5"):
        try:
            r = requests.get(f"https://search.wb.ru/exactmatch/ru/common/{v}/search",
                             params=params, headers=H, timeout=10)
            if r.status_code == 200:
                return r.json().get("data", {}).get("products", [])
        except: pass
    return []

def wb_search(query, max_price):
    items = []
    for p in wb_req(query, {"sort": "priceup", "priceU": max_price * 100}):
        pr = price(p)
        name = p.get("name", "")
        # ✅ Фильтр релевантности — только подходящие товары
        if pr and pr <= max_price and is_relevant(name, query):
            items.append(to_item(p, pr))
    return items

def wb_hot():
    items = []
    for p in wb_req(random.choice(HOT_QUERIES), {"sort": "popular", "discount": 25}):
        pr, old = price(p), price(p, "basic")
        if pr and old and old > pr and (1 - pr/old) >= 0.25:
            i = to_item(p, pr); i["old"] = old; items.append(i)
    items.sort(key=lambda x: -(x.get("old", 0) - x["price"]))
    return items[:8]

def fmt(item):
    disc = ""
    if item.get("old") and item["old"] > item["price"]:
        disc = f" (−{int((1 - item['price']/item['old'])*100)}%)"
    return (f"📦 *{item['name'][:60]}*\n"
            f"💰 *{item['price']:,} ₽*{disc}\n"
            f"⭐ {item.get('rating','—')}  📝 {item.get('fb',0)} отзывов\n"
            f"🔗 [Открыть на WB]({item['url']})\n")

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
        [InlineKeyboardButton("📊 Статистика",        callback_data="admin_stats")],
        [InlineKeyboardButton("📢 Рассылка",          callback_data="admin_broadcast")],
        [InlineKeyboardButton("👥 Список пользователей", callback_data="admin_users")],
        [InlineKeyboardButton("💰 Доходы",            callback_data="admin_income")],
        [InlineKeyboardButton("⚙️ Настройки бота",   callback_data="admin_settings")],
        [InlineKeyboardButton("◀️ Главное меню",      callback_data="menu")],
    ])

def back_admin():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад в панель", callback_data="admin")]])

def back_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu")]])

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
    if uid == ADMIN_ID:
        plan_line = "👑 Администратор — безлимитный доступ"
    elif sub["plan"] == "free":
        plan_line = "🆓 Бесплатно: до 3 отслеживаний"
    else:
        plan_line = f"💎 {sub['plan'].title()}: активна до {sub.get('expires','')}"
    await u.message.reply_text(
        f"👋 Привет! Слежу за ценами на Wildberries.\n\n{plan_line}\n\nВыбери действие:",
        reply_markup=main_kb(uid))

async def btn(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    d = q.data; uid = q.from_user.id

    # ── Главное меню ──────────────────────────────────────────────────────────
    if d == "menu":
        sub = get_sub(uid)
        if uid == ADMIN_ID:
            plan_line = "👑 Администратор — безлимит"
        elif sub["plan"] == "free":
            plan_line = "🆓 Бесплатно: 3 отслеживания"
        else:
            plan_line = f"💎 {sub['plan'].title()} до {sub.get('expires','')}"
        await q.edit_message_text(f"🏠 *Главное меню*\n{plan_line}",
                                  parse_mode="Markdown", reply_markup=main_kb(uid))

    # ── Панель администратора ─────────────────────────────────────────────────
    elif d == "admin":
        if uid != ADMIN_ID: return
        await q.edit_message_text(
            "👑 *Панель администратора*\n\nВыбери раздел:",
            parse_mode="Markdown", reply_markup=admin_kb())

    elif d == "admin_stats":
        if uid != ADMIN_ID: return
        s = get_stats()
        conv = s["paying"] / max(s["total_users"],1) * 100
        await q.edit_message_text(
            f"📊 *Статистика бота*\n\n"
            f"👥 *Пользователи:*\n"
            f"  Всего: {s['total_users']}\n"
            f"  Сегодня: +{s['new_today']}\n"
            f"  За 7 дней: +{s['new_week']}\n\n"
            f"💎 *Подписки:*\n"
            f"  Платящих: {s['paying']} ({conv:.1f}%)\n"
            f"  Премиум: {s['premium']}\n"
            f"  Про: {s['pro']}\n\n"
            f"📦 *Активных отслеживаний:* {s['total_watches']}\n"
            f"🔔 *Уведомлений отправлено:* {s['notifications']}\n\n"
            f"💰 *Доход:*\n"
            f"  За месяц: {s['month_income']//100:,} ₽\n"
            f"  Всего: {s['total_income']//100:,} ₽",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats"),
                InlineKeyboardButton("◀️ Назад",    callback_data="admin")]]))

    elif d == "admin_income":
        if uid != ADMIN_ID: return
        s = get_stats()
        payments = db("SELECT p.plan, p.amount, p.paid_at, u.username FROM payments p "
                      "LEFT JOIN users u ON u.id=p.user_id ORDER BY p.paid_at DESC LIMIT 10")
        text = f"💰 *Доходы*\n\nЗа месяц: *{s['month_income']//100:,} ₽*\nВсего: *{s['total_income']//100:,} ₽*\n\n"
        if payments:
            text += "📋 *Последние платежи:*\n"
            for p in payments:
                uname = f"@{p['username']}" if p['username'] else "—"
                date  = p['paid_at'][:10]
                text += f"  {date} {uname} — {p['plan']} {p['amount']//100} ₽\n"
        else:
            text += "_Платежей пока нет_"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_admin())

    elif d == "admin_users":
        if uid != ADMIN_ID: return
        users = db("SELECT id, username, joined_at FROM users ORDER BY joined_at DESC LIMIT 15")
        text = f"👥 *Последние пользователи:*\n\n"
        for usr in users:
            uname = f"@{usr['username']}" if usr['username'] else f"id:{usr['id']}"
            date  = usr['joined_at'][:10]
            sub   = get_sub(usr['id'])
            badge = "💎" if sub['plan'] != 'free' else ""
            text += f"  {date} {uname} {badge}\n"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_admin())

    elif d == "admin_broadcast":
        if uid != ADMIN_ID: return
        c.user_data["s"] = BROADCAST
        users_count = len(all_users())
        await q.edit_message_text(
            f"📢 *Рассылка*\n\n"
            f"Получателей: {users_count} пользователей\n\n"
            f"Напиши текст сообщения — оно будет отправлено всем:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="admin")]]))

    elif d == "admin_settings":
        if uid != ADMIN_ID: return
        await q.edit_message_text(
            "⚙️ *Настройки бота*\n\n"
            f"🤖 Бот: @DeshevleRuBot\n"
            f"💾 БД: {DB_PATH}\n"
            f"🌐 URL: {RENDER_URL or 'не задан'}\n"
            f"🔗 Партнёрка WB: {'✅ подключена' if WB_AFFILIATE_ID else '❌ не задана'}\n"
            f"💳 Оплата ЮКасса: {'✅ подключена' if PAYMENT_TOKEN else '❌ не задана'}\n\n"
            f"⏱ Проверка цен: каждые 30 мин\n"
            f"🏓 Пинг keep-alive: каждую минуту",
            parse_mode="Markdown", reply_markup=back_admin())

    # ── Подписка ──────────────────────────────────────────────────────────────
    elif d == "sub":
        sub = get_sub(uid)
        if uid == ADMIN_ID:
            await q.edit_message_text("👑 Ты администратор — безлимитный доступ навсегда!",
                                      reply_markup=back_btn()); return
        if sub["plan"] != "free" and sub.get("active"):
            await q.edit_message_text(
                f"💎 *Подписка {sub['plan'].title()} активна*\n\n"
                f"📅 До: {sub.get('expires','')}\n"
                f"📦 Лимит: {sub['limit']} отслеживаний",
                parse_mode="Markdown", reply_markup=back_btn())
        else:
            expired = "\n⚠️ _Подписка истекла — оформи снова!_\n" if sub.get("expired") else ""
            await q.edit_message_text(
                f"💎 *Выбери тариф:*{expired}\n\n"
                "🆓 *Бесплатно* — 3 отслеживания\n"
                "💎 *Премиум* — 199 ₽/мес — 20 отслеживаний\n"
                "🚀 *Про* — 499 ₽/мес — безлимит\n\n"
                "Оплата: карта, СБП, ЮMoney",
                parse_mode="Markdown", reply_markup=sub_kb())

    elif d in ("buy_premium", "buy_pro"):
        plan_key = d.replace("buy_", "")
        plan = PLANS[plan_key]
        await c.bot.send_invoice(
            chat_id=uid,
            title=f"Подписка {plan['name']} — 30 дней",
            description=f"До {plan['limit']} отслеживаний товаров на Wildberries",
            payload=plan_key,
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(plan["name"], plan["price"])],
            start_parameter=f"sub_{plan_key}",
        )

    # ── Добавить товар ────────────────────────────────────────────────────────
    elif d == "add":
        limit = user_limit(uid)
        count = watches_count(uid)
        if count >= limit:
            if uid == ADMIN_ID:
                await q.edit_message_text("⚠️ Технический лимит.", reply_markup=back_btn()); return
            await q.edit_message_text(
                f"⚠️ *Достигнут лимит {limit} отслеживания*\n\n"
                "Оформи подписку чтобы добавить больше:\n\n"
                "💎 *Премиум* — 199 ₽/мес — до 20 товаров\n"
                "🚀 *Про* — 499 ₽/мес — безлимит",
                parse_mode="Markdown", reply_markup=sub_kb()); return
        c.user_data["s"] = ENTER_QUERY
        await q.edit_message_text(
            "🔍 Напиши название товара:\n\n"
            "_Например: наушники Marshall, кроссовки Nike Air_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="menu")]]))

    # ── Список отслеживаний ───────────────────────────────────────────────────
    elif d == "list":
        ws  = get_watches(uid)
        sub = get_sub(uid)
        lim = "∞" if uid == ADMIN_ID else str(sub["limit"])
        if not ws:
            await q.edit_message_text("📋 Нет отслеживаний.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Добавить", callback_data="add")]])); return
        txt = f"📋 *Отслеживания ({len(ws)}/{lim}):*\n\n"
        btns = []
        for w in ws:
            txt += f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
            btns.append([InlineKeyboardButton(f"❌ {w['query'][:25]}", callback_data=f"d_{w['id']}")])
        btns.append([InlineKeyboardButton("➕ Добавить", callback_data="add"),
                     InlineKeyboardButton("◀️ Назад",    callback_data="menu")])
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("d_"):
        del_watch(uid, int(d[2:]))
        ws  = get_watches(uid)
        sub = get_sub(uid)
        lim = "∞" if uid == ADMIN_ID else str(sub["limit"])
        txt = "✅ Удалено!\n\n"
        btns = []
        if ws:
            txt += f"📋 *Отслеживания ({len(ws)}/{lim}):*\n\n"
            for w in ws:
                txt += f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
                btns.append([InlineKeyboardButton(f"❌ {w['query'][:25]}", callback_data=f"d_{w['id']}")])
        btns.append([InlineKeyboardButton("➕ Добавить", callback_data="add"),
                     InlineKeyboardButton("◀️ Назад",    callback_data="menu")])
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    # ── Горящие скидки ────────────────────────────────────────────────────────
    elif d == "hot":
        await q.edit_message_text("⏳ Ищу горящие скидки...")
        hot = wb_hot()
        if not hot:
            await q.edit_message_text("😔 Сейчас нет. Попробуй позже!", reply_markup=back_btn()); return
        txt = "🔥 *Горящие скидки:*\n\n"
        for item in hot[:5]: txt += fmt(item) + "\n"
        await q.edit_message_text(txt, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить", callback_data="hot"),
                InlineKeyboardButton("◀️ Назад",    callback_data="menu")]]))

# ─── Оплата ───────────────────────────────────────────────────────────────────

async def precheckout(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.pre_checkout_query.answer(ok=True)

async def payment_success(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    plan_key = u.message.successful_payment.invoice_payload
    plan = PLANS.get(plan_key)
    if not plan: return
    set_sub(uid, plan_key, plan["days"])
    log_payment(uid, plan_key, plan["price"])
    expires = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")
    await u.message.reply_text(
        f"🎉 *Оплата прошла!*\n\n"
        f"✅ Подписка *{plan['name']}* активирована\n"
        f"📦 Лимит: {plan['limit']} отслеживаний\n"
        f"📅 Активна до: {expires}",
        parse_mode="Markdown", reply_markup=main_kb(uid))
    try:
        uname = u.effective_user.username or str(uid)
        await c.bot.send_message(ADMIN_ID,
            f"💰 *Новая оплата!*\n\n"
            f"👤 @{uname} (id: {uid})\n"
            f"📦 Тариф: {plan['name']}\n"
            f"💵 Сумма: {plan['price']//100} ₽",
            parse_mode="Markdown")
    except: pass

# ─── Сообщения ────────────────────────────────────────────────────────────────

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid  = u.effective_user.id
    text = u.message.text.strip()
    s    = c.user_data.get("s")

    # Рассылка от админа
    if s == BROADCAST and uid == ADMIN_ID:
        c.user_data["s"] = None
        users = all_users()
        m = await u.message.reply_text(f"⏳ Отправляю {len(users)} пользователям...")
        sent = 0
        for user_id in users:
            try:
                await c.bot.send_message(user_id, text, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.05)
            except: pass
        await m.edit_text(
            f"✅ Рассылка завершена!\n\n📨 Отправлено: {sent}/{len(users)}",
            reply_markup=back_admin())
        return

    if s == ENTER_QUERY:
        c.user_data["q"] = text; c.user_data["s"] = ENTER_PRICE
        await u.message.reply_text(
            f"💰 Товар: *{text}*\n\nУкажи максимальную цену:\n_Например: 5000_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="menu")]]))

    elif s == ENTER_PRICE:
        try:
            mp = int(text.replace(" ","").replace(",",""))
            if mp <= 0: raise ValueError
        except:
            await u.message.reply_text("⚠️ Введи число, например: 5000"); return
        query = c.user_data.get("q",""); c.user_data["s"] = None
        add_watch(uid, query, mp)
        m = await u.message.reply_text("⏳ Ищу товары на WB...")
        res = wb_search(query, mp)
        if res:
            reply = f"✅ Добавлено!\n\n🎯 Нашёл {len(res)} товаров до {mp:,} ₽:\n\n"
            for item in res[:3]: reply += fmt(item) + "\n"
        else:
            reply = (f"✅ Добавлено!\n\n📦 *{query}* — до *{mp:,} ₽*\n\n"
                     f"Сейчас подходящих нет, но слежу! Как только найду — напишу.")
        await m.edit_text(reply, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Список", callback_data="list"),
                InlineKeyboardButton("➕ Ещё",    callback_data="add")]]))
    else:
        await u.message.reply_text("Используй кнопки 👇", reply_markup=main_kb(uid))

# ─── Планировщик ──────────────────────────────────────────────────────────────

async def scheduler(app):
    await asyncio.sleep(60)
    while True:
        try:
            ws = all_watches()
            logger.info(f"Checking {len(ws)} watches")
            for w in ws:
                # ✅ wb_search уже фильтрует по релевантности
                for item in wb_search(w["query"], w["max_price"])[:3]:
                    if was_sent(w["id"], item["id"]): continue
                    txt = (f"🔔 *Нашёл по твоей цене!*\n\n"
                           f"🔍 _{w['query']}_\n") + fmt(item)
                    try:
                        await app.bot.send_message(w["user_id"], txt,
                            parse_mode="Markdown", disable_web_page_preview=True)
                        mark_sent(w["user_id"], w["id"], item["id"])
                    except Exception as e: logger.warning(f"send: {e}")
                await asyncio.sleep(2)
        except Exception as e: logger.error(f"scheduler: {e}")
        await asyncio.sleep(1800)

# ─── Keep-alive ───────────────────────────────────────────────────────────────

async def keep_alive():
    await asyncio.sleep(30)
    while True:
        try:
            if RENDER_URL:
                r = requests.get(RENDER_URL, timeout=10)
                logger.info(f"ping: {r.status_code}")
        except Exception as e: logger.warning(f"ping: {e}")
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
