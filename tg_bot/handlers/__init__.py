"""Хендлеры бота, собранные в один корневой роутер."""

from aiogram import Router

from tg_bot.handlers.billing import router as billing_router
from tg_bot.handlers.language import router as language_router
from tg_bot.handlers.news import router as news_router
from tg_bot.handlers.trial import router as trial_router

router = Router(name="root")

# Порядок важен: роутер новостей заканчивается перехватчиком неизвестных
# callback-запросов, поэтому включается последним — иначе он поглотил бы
# нажатия платёжных кнопок.
#
# Роутер триала идёт первым: часть его обработчиков ловит любое сообщение,
# но только внутри состояния FSM, и поставить их после общих команд значило
# бы, что /help посреди диалога подтверждения телефона уведёт пользователя
# из сценария, оставив висеть клавиатуру запроса номера.
# Роутер языка стоит перед остальными: его состояние FSM ловит
# произвольный ввод, а смена языка должна работать из любого экрана.
router.include_router(language_router)
router.include_router(trial_router)
router.include_router(billing_router)
router.include_router(news_router)

__all__ = ["router"]
