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

BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
ADMIN_ID         = int(os.environ.get("ADMIN_ID", "0"))
PORT             = int(os.environ.get("PORT", "8080"))
RENDER_URL       = os.environ.get("RENDER_URL", "")
DB_PATH          = os.environ.get("DB_PATH", "bot.db")
WB_AFFILIATE_ID  = os.environ.get("WB_AFFILIATE_ID", "")
PAYMENT_TOKEN    = os.environ.get("PAYMENT_TOKEN", "")  # токен ЮКасса из BotFather

HOT_QUERIES = ["наушники","кроссовки","смартфон","куртка","ноутбук","часы","рюкзак"]
ENTER_QUERY = "q"
ENTER_PRICE = "p"

# Тарифы (цена в копейках для Telegram Payments)
PLANS = {
    "premium": {"name": "Премиум",  "price": 19900,  "limit": 20,  "days": 30},
    "pro":     {"name": "Про",      "price": 49900,  "limit": 999, "days": 30},
}

# ─── БД ───────────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS watches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, query TEXT, max_price INTEGER, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, watch_id INTEGER, item_id TEXT);
        CREATE TABLE IF NOT EXISTS subscriptions(
            user_id INTEGER PRIMARY KEY,
            plan TEXT DEFAULT 'free',
            expires_at TEXT DEFAULT NULL);
    """)
    con.close()

def db(sql, p=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, p).fetchall()]
    con.commit(); con.close()
    return rows

def add_user(uid, uname):   db("INSERT OR IGNORE INTO users(id,username) VALUES(?,?)",(uid,uname))
def add_watch(uid,q,mp):    db("INSERT INTO watches(user_id,query,max_price) VALUES(?,?,?)",(uid,q,mp))
def get_watches(uid):       return db("SELECT id,query,max_price FROM watches WHERE user_id=? AND active=1",(uid,))
def del_watch(uid,wid):     db("UPDATE watches SET active=0 WHERE id=? AND user_id=?",(wid,uid))
def all_watches():          return db("SELECT id,user_id,query,max_price FROM watches WHERE active=1")
def was_sent(wid,iid):      return bool(db("SELECT 1 FROM notifications WHERE watch_id=? AND item_id=?",(wid,iid)))
def mark_sent(uid,wid,iid): db("INSERT INTO notifications(user_id,watch_id,item_id) VALUES(?,?,?)",(uid,wid,iid))
def all_users():            return [r["id"] for r in db("SELECT id FROM users")]
def stats():
    return {k: db(f"SELECT COUNT(*) c FROM {t}")[0]["c"]
            for k,t in [("users","users"),("watches","watches WHERE active=1"),("notifications","notifications")]}

def get_sub(uid):
    rows = db("SELECT plan, expires_at FROM subscriptions WHERE user_id=?", (uid,))
    if not rows:
        return {"plan": "free", "limit": 3, "active": True}
    row = rows[0]
    plan = row["plan"]
    expires = row["expires_at"]
    if plan == "free":
        return {"plan": "free", "limit": 3, "active": True}
    # Проверяем срок
    if expires and datetime.fromisoformat(expires) > datetime.now():
        return {"plan": plan, "limit": PLANS[plan]["limit"], "active": True, "expires": expires}
    # Подписка истекла
    return {"plan": "free", "limit": 3, "active": False, "expired": True}

def set_sub(uid, plan, days):
    expires = (datetime.now() + timedelta(days=days)).isoformat()
    db("INSERT OR REPLACE INTO subscriptions(user_id,plan,expires_at) VALUES(?,?,?)", (uid, plan, expires))

def watches_count(uid):
    return (db("SELECT COUNT(*) c FROM watches WHERE user_id=? AND active=1",(uid,)) or [{"c":0}])[0]["c"]

def user_limit(uid):
    return get_sub(uid)["limit"]

# ─── WB парсер ────────────────────────────────────────────────────────────────

H = {"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36",
     "Accept":"*/*","Origin":"https://www.wildberries.ru","Referer":"https://www.wildberries.ru/"}

def wb_url(aid):
    u = f"https://www.wildberries.ru/catalog/{aid}/detail.aspx"
    return f"{u}?utm_source=affiliate&utm_campaign={WB_AFFILIATE_ID}" if WB_AFFILIATE_ID else u

def price(p, key="total"):
    try:
        s=p.get("sizes",[])
        v=(s[0].get("price",{}).get(key,0) if s else 0) or p.get("salePriceU" if key=="total" else "priceU",0)
        return v//100 if v else None
    except: return None

def to_item(p, pr):
    return {"id":str(p.get("id",0)),"name":p.get("name",""),"price":pr,
            "old":price(p,"basic"),"rating":p.get("reviewRating",0),
            "fb":p.get("feedbacks",0),"url":wb_url(p.get("id",0))}

def wb_req(query, extra=None):
    params = {"query":query,"resultset":"catalog","limit":20,"appType":1,
              "curr":"rub","lang":"ru","dest":-1257786,**(extra or {})}
    for v in ("v9","v7","v5"):
        try:
            r = requests.get(f"https://search.wb.ru/exactmatch/ru/common/{v}/search",
                             params=params, headers=H, timeout=10)
            if r.status_code==200:
                return r.json().get("data",{}).get("products",[])
        except: pass
    return []

def wb_search(query, max_price):
    items=[]
    for p in wb_req(query, {"sort":"priceup","priceU":max_price*100}):
        pr=price(p)
        if pr and pr<=max_price: items.append(to_item(p,pr))
    return items

def wb_hot():
    items=[]
    for p in wb_req(random.choice(HOT_QUERIES), {"sort":"popular","discount":25}):
        pr,old=price(p),price(p,"basic")
        if pr and old and old>pr and (1-pr/old)>=0.25:
            i=to_item(p,pr); i["old"]=old; items.append(i)
    items.sort(key=lambda x: -(x.get("old",0)-x["price"]))
    return items[:8]

def fmt(item):
    disc=""
    if item.get("old") and item["old"]>item["price"]:
        disc=f" (−{int((1-item['price']/item['old'])*100)}%)"
    return (f"📦 *{item['name'][:60]}*\n"
            f"💰 *{item['price']:,} ₽*{disc}\n"
            f"⭐ {item.get('rating','—')}  📝 {item.get('fb',0)} отзывов\n"
            f"🔗 [Открыть на WB]({item['url']})\n")

# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить товар",    callback_data="add"),
         InlineKeyboardButton("📋 Мои отслеживания", callback_data="list")],
        [InlineKeyboardButton("🔥 Горящие скидки",   callback_data="hot"),
         InlineKeyboardButton("💎 Подписка",         callback_data="sub")],
    ])

def back_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu")]])

def sub_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Премиум — 199 ₽/мес (20 товаров)", callback_data="buy_premium")],
        [InlineKeyboardButton("🚀 Про — 499 ₽/мес (безлимит)",       callback_data="buy_pro")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu")],
    ])

# ─── Хендлеры ─────────────────────────────────────────────────────────────────

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    add_user(u.effective_user.id, u.effective_user.username or "")
    sub = get_sub(u.effective_user.id)
    plan_text = "🆓 Бесплатно: 3 отслеживания" if sub["plan"]=="free" else f"💎 {sub['plan'].title()}: активна"
    await u.message.reply_text(
        f"👋 Привет! Слежу за ценами на Wildberries.\n\n{plan_text}\n\nВыбери действие:",
        reply_markup=main_kb())

async def adm_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != ADMIN_ID: return
    s=stats()
    subs = db("SELECT plan, COUNT(*) c FROM subscriptions GROUP BY plan")
    sub_text = "\n".join([f"  {r['plan']}: {r['c']}" for r in subs]) or "  нет"
    await u.message.reply_text(
        f"📊 *Статистика:*\n\n👥 {s['users']} пользователей\n"
        f"👁 {s['watches']} отслеживаний\n🔔 {s['notifications']} уведомлений\n\n"
        f"💎 Подписки:\n{sub_text}", parse_mode="Markdown")

async def broadcast_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != ADMIN_ID: return
    text = u.message.text.replace("/broadcast","").strip()
    if not text: await u.message.reply_text("Использование: /broadcast Текст"); return
    sent=0
    for uid in all_users():
        try: await c.bot.send_message(uid,text); sent+=1; await asyncio.sleep(0.05)
        except: pass
    await u.message.reply_text(f"✅ Отправлено {sent}/{len(all_users())}")

async def btn(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q=u.callback_query; await q.answer()
    d=q.data; uid=q.from_user.id

    if d=="menu":
        sub = get_sub(uid)
        plan_text = "🆓 Бесплатно: 3 отслеживания" if sub["plan"]=="free" else f"💎 {sub['plan'].title()}: активна до {sub.get('expires','')[:10]}"
        await q.edit_message_text(f"🏠 *Главное меню*\n{plan_text}",
                                  parse_mode="Markdown", reply_markup=main_kb())

    elif d=="sub":
        sub = get_sub(uid)
        if sub["plan"] != "free":
            expires = sub.get("expires","")[:10]
            await q.edit_message_text(
                f"💎 *Твоя подписка: {sub['plan'].title()}*\n\n"
                f"✅ Активна до: {expires}\n"
                f"📦 Лимит отслеживаний: {sub['limit']}\n\n"
                f"Подписка продлится автоматически.",
                parse_mode="Markdown", reply_markup=back_btn())
        else:
            await q.edit_message_text(
                "💎 *Выбери тариф:*\n\n"
                "🆓 *Бесплатно* — 3 отслеживания\n"
                "💎 *Премиум* — 199 ₽/мес — 20 отслеживаний\n"
                "🚀 *Про* — 499 ₽/мес — безлимит\n\n"
                "Оплата через ЮКасса — банковская карта, СБП, ЮMoney",
                parse_mode="Markdown", reply_markup=sub_kb())

    elif d in ("buy_premium","buy_pro"):
        plan_key = d.replace("buy_","")
        plan = PLANS[plan_key]
        if not PAYMENT_TOKEN:
            await q.edit_message_text("⚠️ Оплата временно недоступна. Попробуй позже.",
                                      reply_markup=back_btn()); return
        await c.bot.send_invoice(
            chat_id=uid,
            title=f"Подписка {plan['name']}",
            description=f"{plan['limit']} отслеживаний на 30 дней",
            payload=plan_key,
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(plan["name"], plan["price"])],
            start_parameter=f"sub_{plan_key}",
            need_email=False,
            need_phone_number=False,
        )

    elif d=="add":
        limit = user_limit(uid)
        count = watches_count(uid)
        if count >= limit:
            sub = get_sub(uid)
            if sub["plan"] == "free":
                await q.edit_message_text(
                    f"⚠️ *Достигнут лимит {limit} отслеживания*\n\n"
                    "Чтобы добавить больше — оформи подписку:\n\n"
                    "💎 *Премиум* — 199 ₽/мес — до 20 товаров\n"
                    "🚀 *Про* — 499 ₽/мес — безлимит",
                    parse_mode="Markdown", reply_markup=sub_kb())
            else:
                await q.edit_message_text("⚠️ Достигнут лимит отслеживаний.",
                                          reply_markup=back_btn())
            return
        c.user_data["s"]=ENTER_QUERY
        await q.edit_message_text(
            "🔍 Напиши название товара:\n\n_Например: кроссовки Nike, наушники Sony_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена",callback_data="menu")]]))

    elif d=="list":
        ws=get_watches(uid)
        sub=get_sub(uid)
        plan_badge = "" if sub["plan"]=="free" else " 💎"
        if not ws:
            await q.edit_message_text("📋 Нет отслеживаний.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Добавить",callback_data="add")]])); return
        txt=f"📋 *Отслеживания ({len(ws)}/{sub['limit']}){plan_badge}:*\n\n"
        btns=[]
        for w in ws:
            txt+=f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
            btns.append([InlineKeyboardButton(f"❌ {w['query'][:25]}",callback_data=f"d_{w['id']}")])
        btns.append([InlineKeyboardButton("➕ Добавить",callback_data="add"),
                     InlineKeyboardButton("◀️ Назад",callback_data="menu")])
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    elif d.startswith("d_"):
        del_watch(uid, int(d[2:]))
        ws=get_watches(uid)
        sub=get_sub(uid)
        txt="✅ Удалено!\n\n"
        btns=[]
        if ws:
            txt+=f"📋 *Отслеживания ({len(ws)}/{sub['limit']}):*\n\n"
            for w in ws:
                txt+=f"🔍 *{w['query']}* — до {w['max_price']:,} ₽\n"
                btns.append([InlineKeyboardButton(f"❌ {w['query'][:25]}",callback_data=f"d_{w['id']}")])
        btns.append([InlineKeyboardButton("➕ Добавить",callback_data="add"),
                     InlineKeyboardButton("◀️ Назад",callback_data="menu")])
        await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    elif d=="hot":
        await q.edit_message_text("⏳ Ищу горящие скидки...")
        hot=wb_hot()
        if not hot:
            await q.edit_message_text("😔 Сейчас нет. Попробуй позже!", reply_markup=back_btn()); return
        txt="🔥 *Горящие скидки:*\n\n"
        for item in hot[:5]: txt+=fmt(item)+"\n"
        await q.edit_message_text(txt, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить",callback_data="hot"),
                InlineKeyboardButton("◀️ Назад",callback_data="menu")]]))

# ─── Оплата ───────────────────────────────────────────────────────────────────

async def precheckout(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Telegram спрашивает подтверждение перед оплатой."""
    await u.pre_checkout_query.answer(ok=True)

async def payment_success(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Платёж прошёл успешно."""
    uid = u.effective_user.id
    plan_key = u.message.successful_payment.invoice_payload
    plan = PLANS.get(plan_key)
    if not plan:
        return
    set_sub(uid, plan_key, plan["days"])
    expires = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")
    await u.message.reply_text(
        f"🎉 *Оплата прошла успешно!*\n\n"
        f"✅ Подписка *{plan['name']}* активирована\n"
        f"📦 Лимит: {plan['limit']} отслеживаний\n"
        f"📅 Активна до: {expires}\n\n"
        f"Теперь можешь добавлять больше товаров!",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    # Уведомляем админа
    if ADMIN_ID:
        try:
            await c.bot.send_message(ADMIN_ID,
                f"💰 Новая оплата!\nПользователь: {uid}\nТариф: {plan['name']}\nСумма: {plan['price']//100} ₽")
        except: pass

# ─── Сообщения ────────────────────────────────────────────────────────────────

async def msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid=u.effective_user.id; text=u.message.text.strip(); s=c.user_data.get("s")
    if s==ENTER_QUERY:
        c.user_data["q"]=text; c.user_data["s"]=ENTER_PRICE
        await u.message.reply_text(f"💰 Товар: *{text}*\n\nУкажи максимальную цену:\n_Например: 5000_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена",callback_data="menu")]]))
    elif s==ENTER_PRICE:
        try:
            mp=int(text.replace(" ","").replace(",",""))
            if mp<=0: raise ValueError
        except: await u.message.reply_text("⚠️ Введи число, например: 5000"); return
        query=c.user_data.get("q",""); c.user_data["s"]=None
        add_watch(uid,query,mp)
        m=await u.message.reply_text("⏳ Ищу товары...")
        res=wb_search(query,mp)
        if res:
            reply=f"✅ Добавлено!\n\n🎯 Нашёл {len(res)} товаров до {mp:,} ₽:\n\n"
            for item in res[:3]: reply+=fmt(item)+"\n"
        else:
            reply=f"✅ Добавлено!\n\n📦 *{query}* — до *{mp:,} ₽*\nСейчас нет подходящих, слежу!"
        await m.edit_text(reply, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Список",callback_data="list"),
                InlineKeyboardButton("➕ Ещё",callback_data="add")]]))
    else:
        await u.message.reply_text("Используй кнопки 👇", reply_markup=main_kb())

# ─── Планировщик ──────────────────────────────────────────────────────────────

async def scheduler(app):
    await asyncio.sleep(60)
    while True:
        try:
            ws=all_watches()
            logger.info(f"Checking {len(ws)} watches")
            for w in ws:
                for item in wb_search(w["query"],w["max_price"])[:3]:
                    if was_sent(w["id"],item["id"]): continue
                    txt=f"🔔 *Нашёл по твоей цене!*\n\n🔍 _{w['query']}_\n"+fmt(item)
                    try:
                        await app.bot.send_message(w["user_id"],txt,
                            parse_mode="Markdown",disable_web_page_preview=True)
                        mark_sent(w["user_id"],w["id"],item["id"])
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
                r=requests.get(RENDER_URL,timeout=10)
                logger.info(f"ping: {r.status_code}")
        except Exception as e: logger.warning(f"ping: {e}")
        await asyncio.sleep(60)

# ─── Веб-сервер ───────────────────────────────────────────────────────────────

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"OK - DeshevleBot alive!")
    def log_message(self,*a): pass

def run_web():
    HTTPServer(("0.0.0.0",PORT),WebHandler).serve_forever()

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    init_db()
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", adm_stats))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, payment_success))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg))

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        asyncio.create_task(scheduler(app))
        asyncio.create_task(keep_alive())
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
