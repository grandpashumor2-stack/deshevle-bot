import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import Database
from parser import WBParser
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db = Database()
parser = WBParser()


class AddWatch(StatesGroup):
    waiting_for_query = State()
    waiting_for_max_price = State()


# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await db.add_user(message.from_user.id, message.from_user.username or "")
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить товар", callback_data="add_watch")
    kb.button(text="📋 Мои отслеживания", callback_data="my_watches")
    kb.button(text="🔥 Горящие скидки", callback_data="hot_deals")
    kb.button(text="ℹ️ Как работает бот", callback_data="how_it_works")
    kb.adjust(2)
    await message.answer(
        "👋 Привет! Я слежу за ценами на Wildberries и сообщаю, "
        "когда товар подешевел.\n\n"
        "🆓 Бесплатно: до 3 отслеживаний\n"
        "💎 Премиум: до 20 отслеживаний (скоро)\n\n"
        "Выбери действие:",
        reply_markup=kb.as_markup()
    )


# ─── Как работает ─────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "how_it_works")
async def how_it_works(call: types.CallbackQuery):
    await call.message.edit_text(
        "📖 <b>Как пользоваться ботом:</b>\n\n"
        "1️⃣ Нажми «Добавить товар»\n"
        "2️⃣ Введи название товара (например: <i>кроссовки Nike</i>)\n"
        "3️⃣ Укажи максимальную цену в рублях\n"
        "4️⃣ Бот проверяет WB каждые 30 минут\n"
        "5️⃣ Как только появится товар дешевле — получишь уведомление!\n\n"
        "🔗 Ссылки в уведомлениях — партнёрские. "
        "Ты покупаешь по той же цене, а бот получает небольшой % — так мы остаёмся бесплатными.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")
        ]])
    )


# ─── Добавить отслеживание ────────────────────────────────────────────────────

@dp.callback_query(F.data == "add_watch")
async def add_watch_start(call: types.CallbackQuery, state: FSMContext):
    count = await db.get_watches_count(call.from_user.id)
    if count >= 3:
        await call.message.edit_text(
            "⚠️ У тебя уже 3 отслеживания (лимит бесплатного тарифа).\n"
            "Удали одно или дождись запуска Премиума.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Мои отслеживания", callback_data="my_watches")
            ]])
        )
        return
    await state.set_state(AddWatch.waiting_for_query)
    await call.message.edit_text(
        "🔍 Напиши название товара, который хочешь отслеживать.\n\n"
        "<i>Например: кроссовки Nike Air, смартфон Samsung, наушники Sony</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")
        ]])
    )


@dp.message(AddWatch.waiting_for_query)
async def add_watch_query(message: types.Message, state: FSMContext):
    await state.update_data(query=message.text.strip())
    await state.set_state(AddWatch.waiting_for_max_price)
    await message.answer(
        f"💰 Товар: <b>{message.text.strip()}</b>\n\n"
        "Теперь укажи <b>максимальную цену</b> в рублях.\n"
        "Бот пришлёт уведомление, когда найдёт дешевле.\n\n"
        "<i>Введи только число, например: 5000</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")
        ]])
    )


@dp.message(AddWatch.waiting_for_max_price)
async def add_watch_price(message: types.Message, state: FSMContext):
    try:
        max_price = int(message.text.strip().replace(" ", "").replace(",", ""))
        if max_price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи корректную цену (только цифры, например: 5000)")
        return

    data = await state.get_data()
    query = data["query"]
    await state.clear()

    await db.add_watch(message.from_user.id, query, max_price)

    # Сразу делаем первый поиск
    msg = await message.answer("⏳ Ищу товары прямо сейчас...")
    results = await parser.search(query, max_price)

    if results:
        text = f"✅ Отслеживание добавлено!\n\n🎯 Уже нашёл {len(results)} товаров до {max_price:,} ₽:\n\n"
        for item in results[:3]:
            text += format_item(item) + "\n"
    else:
        text = (
            f"✅ Отслеживание добавлено!\n\n"
            f"📦 Товар: <b>{query}</b>\n"
            f"💰 Максимальная цена: <b>{max_price:,} ₽</b>\n\n"
            f"Сейчас подходящих товаров нет, но я слежу! "
            f"Как только появится что-то дешевле — сразу напишу."
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Мои отслеживания", callback_data="my_watches")
    kb.button(text="➕ Добавить ещё", callback_data="add_watch")
    kb.adjust(2)

    await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup(),
                        disable_web_page_preview=True)


# ─── Мои отслеживания ─────────────────────────────────────────────────────────

@dp.callback_query(F.data == "my_watches")
async def my_watches(call: types.CallbackQuery):
    watches = await db.get_watches(call.from_user.id)
    if not watches:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Добавить товар", callback_data="add_watch")
        await call.message.edit_text(
            "📋 У тебя пока нет отслеживаний.\nДобавь первый товар!",
            reply_markup=kb.as_markup()
        )
        return

    text = f"📋 <b>Твои отслеживания ({len(watches)}/3):</b>\n\n"
    kb = InlineKeyboardBuilder()

    for w in watches:
        text += f"🔍 <b>{w['query']}</b> — до {w['max_price']:,} ₽\n"
        kb.button(text=f"❌ {w['query'][:20]}", callback_data=f"del_{w['id']}")

    kb.button(text="➕ Добавить", callback_data="add_watch")
    kb.button(text="◀️ Главная", callback_data="back_to_main")
    kb.adjust(1, 2)

    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("del_"))
async def delete_watch(call: types.CallbackQuery):
    watch_id = int(call.data.split("_")[1])
    await db.delete_watch(call.from_user.id, watch_id)
    await my_watches(call)


# ─── Горящие скидки ───────────────────────────────────────────────────────────

@dp.callback_query(F.data == "hot_deals")
async def hot_deals(call: types.CallbackQuery):
    msg = await call.message.edit_text("⏳ Ищу горящие скидки...")
    hot = await parser.get_hot_deals()

    if not hot:
        await msg.edit_text(
            "😔 Горящих скидок сейчас нет. Попробуй позже!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")
            ]])
        )
        return

    text = "🔥 <b>Горящие скидки прямо сейчас:</b>\n\n"
    for item in hot[:5]:
        text += format_item(item) + "\n"

    await msg.edit_text(
        text, parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="hot_deals"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")
        ]])
    )


# ─── Навигация ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(call: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить товар", callback_data="add_watch")
    kb.button(text="📋 Мои отслеживания", callback_data="my_watches")
    kb.button(text="🔥 Горящие скидки", callback_data="hot_deals")
    kb.button(text="ℹ️ Как работает бот", callback_data="how_it_works")
    kb.adjust(2)
    await call.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыбери действие:",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data == "cancel_add")
async def cancel_add(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await back_to_main(call)


# ─── Статистика для админа ────────────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    stats = await db.get_stats()
    await message.answer(
        f"📊 <b>Статистика бота:</b>\n\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"👁 Отслеживаний: {stats['watches']}\n"
        f"🔔 Уведомлений отправлено: {stats['notifications']}\n",
        parse_mode="HTML"
    )


# ─── Рассылка для всех (admin) ────────────────────────────────────────────────

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("Использование: /broadcast Текст сообщения")
        return
    users = await db.get_all_users()
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ Отправлено {sent}/{len(users)} пользователям")


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def format_item(item: dict) -> str:
    discount = ""
    if item.get("old_price") and item["old_price"] > item["price"]:
        pct = int((1 - item["price"] / item["old_price"]) * 100)
        discount = f" (−{pct}%)"
    return (
        f"📦 <b>{item['name'][:60]}</b>\n"
        f"💰 <b>{item['price']:,} ₽</b>{discount}"
        + (f" ~~{item['old_price']:,} ₽~~" if item.get("old_price") and item["old_price"] > item["price"] else "")
        + f"\n⭐ {item.get('rating', '—')}  📦 {item.get('feedbacks', 0)} отзывов\n"
        f"🔗 <a href=\"{item['url']}\">Открыть на WB</a>\n"
    )


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    await db.init()
    await start_scheduler(bot, db, parser)
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
