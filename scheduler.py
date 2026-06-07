import asyncio
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30 * 60  # 30 минут


async def start_scheduler(bot: Bot, db, parser):
    asyncio.create_task(_run_scheduler(bot, db, parser))
    logger.info("Scheduler started (interval: 30 min)")


async def _run_scheduler(bot: Bot, db, parser):
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            await _check_all_watches(bot, db, parser)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")


async def _check_all_watches(bot: Bot, db, parser):
    watches = await db.get_all_active_watches()
    if not watches:
        return

    logger.info(f"Checking {len(watches)} watches...")

    for watch in watches:
        try:
            results = await parser.search(watch["query"], watch["max_price"])
            for item in results[:3]:
                already = await db.was_notified(watch["id"], item["id"])
                if already:
                    continue

                text = (
                    f"🔔 <b>Нашёл товар по твоей цене!</b>\n\n"
                    f"🔍 Запрос: <i>{watch['query']}</i>\n"
                    f"📦 <b>{item['name'][:70]}</b>\n"
                    f"💰 <b>{item['price']:,} ₽</b>"
                )

                if item.get("old_price") and item["old_price"] > item["price"]:
                    pct = int((1 - item["price"] / item["old_price"]) * 100)
                    text += f" (скидка {pct}%!)"

                text += (
                    f"\n⭐ {item.get('rating', '—')}  "
                    f"📝 {item.get('feedbacks', 0)} отзывов\n"
                    f"🔗 <a href=\"{item['url']}\">Открыть на WB</a>"
                )

                try:
                    await bot.send_message(
                        watch["user_id"],
                        text,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                    await db.mark_notified(watch["user_id"], watch["id"], item["id"])
                    await asyncio.sleep(0.1)
                except TelegramForbiddenError:
                    logger.info(f"User {watch['user_id']} blocked the bot")
                    break
                except TelegramBadRequest as e:
                    logger.warning(f"Bad request for user {watch['user_id']}: {e}")

            await asyncio.sleep(1)  # пауза между запросами к WB

        except Exception as e:
            logger.error(f"Error checking watch {watch['id']}: {e}")
