import asyncio
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from core.config import BOT_TOKEN
from core.logger import logger
from db.database import init_models
from tg_bot.handlers import router
from tg_bot.middlewares import DatabaseMiddleware


async def main():
    logger.info("Инициализация Базы Данных...")
    await init_models()

    # DefaultBotProperties устанавливает HTML разметку по умолчанию для всех сообщений
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # Подключаем Middleware для БД
    dp.update.middleware(DatabaseMiddleware())

    # Подключаем роутеры с хендлерами
    dp.include_router(router)

    # Инициализируем aiohttp сессию здесь, чтобы она жила пока работает бот
    http_session = aiohttp.ClientSession(
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0"
        }
    )

    logger.info("Бот успешно запущен и готов к работе!")
    try:
        # Передаем http_session в хендлеры через kwargs диспетчера
        await dp.start_polling(bot, http_session=http_session)
    finally:
        logger.info("Остановка бота, закрытие сессий...")
        await http_session.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (Ctrl+C)")