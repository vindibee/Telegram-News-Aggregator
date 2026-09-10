"""Хендлеры бота, собранные в один корневой роутер."""

from aiogram import Router

from tg_bot.handlers.admin import router as admin_router
from tg_bot.handlers.billing import router as billing_router
from tg_bot.handlers.cabinet import router as cabinet_router
from tg_bot.handlers.language import router as language_router
from tg_bot.handlers.news import router as news_router
from tg_bot.handlers.onboarding import router as onboarding_router
from tg_bot.handlers.promo import router as promo_router
from tg_bot.handlers.search import router as search_router
from tg_bot.handlers.stats import router as stats_router
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
# Знакомство идёт первым: его экраны — вход в бота, и они не должны
# перекрываться ничем другим.
router.include_router(onboarding_router)
router.include_router(language_router)
# Кабинет тоже ловит свободный ввод, но только внутри своих
# состояний, поэтому стоит рядом с остальными FSM-сценариями.
router.include_router(cabinet_router)
# Панель администратора идёт до пользовательских сценариев: её
# состояния FSM ловят произвольный ввод, а фильтр прав всё
# равно пропускает дальше всех, кроме администраторов.
router.include_router(admin_router)
# Промокоды тоже ждут свободный ввод внутри своего состояния.
router.include_router(promo_router)
router.include_router(search_router)
router.include_router(stats_router)
router.include_router(trial_router)
router.include_router(billing_router)
router.include_router(news_router)

__all__ = ["router"]
