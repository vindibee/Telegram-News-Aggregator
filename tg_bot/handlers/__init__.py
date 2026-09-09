"""Хендлеры бота, собранные в один корневой роутер."""

from aiogram import Router

from tg_bot.handlers.billing import router as billing_router
from tg_bot.handlers.news import router as news_router

router = Router(name="root")

# Порядок важен: роутер новостей заканчивается перехватчиком неизвестных
# callback-запросов, поэтому включается последним — иначе он поглотил бы
# нажатия платёжных кнопок.
router.include_router(billing_router)
router.include_router(news_router)

__all__ = ["router"]
