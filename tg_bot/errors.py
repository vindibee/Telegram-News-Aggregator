"""Глобальная обработка исключений хендлеров."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import CallbackQuery, ErrorEvent, Message

from core.logger import get_logger
from db.repositories import RepositoryError
from services.parser import ParserError
from tg_bot.keyboards import kb_to_channels

logger = get_logger(__name__)

_FALLBACK_TEXT = "⚠️ Внутренняя ошибка. Попробуйте позже."


async def handle_known_errors(event: ErrorEvent) -> bool:
    """Ожидаемые доменные ошибки: показываем пользователю их текст."""
    logger.warning("Обработанная ошибка: %s", event.exception)
    await _notify(event, f"❌ {event.exception}")
    return True


async def handle_flood(event: ErrorEvent) -> bool:
    """Flood control Telegram: повторять здесь нечего, только сообщаем."""
    logger.warning("Flood control не преодолён: %s", event.exception)
    await _notify(event, "⏳ Слишком много запросов. Подождите немного и повторите.")
    return True


async def handle_forbidden(event: ErrorEvent) -> bool:
    """Пользователь заблокировал бота — уведомлять некого."""
    logger.info("Бот заблокирован пользователем: %s", event.exception)
    return True


async def handle_unexpected(event: ErrorEvent) -> bool:
    """Непредвиденная ошибка: полный traceback в лог, нейтральный текст в чат."""
    logger.exception("Необработанное исключение при обработке апдейта", exc_info=event.exception)
    await _notify(event, _FALLBACK_TEXT)
    return True


async def _notify(event: ErrorEvent, text: str) -> None:
    """Пытается сообщить пользователю об ошибке, не поднимая новых исключений."""
    update = event.update
    try:
        if isinstance(update.callback_query, CallbackQuery):
            await update.callback_query.answer(text[:200], show_alert=True)
            message = update.callback_query.message
            if isinstance(message, Message):
                await message.answer(text, reply_markup=kb_to_channels())
        elif isinstance(update.message, Message):
            await update.message.answer(text, reply_markup=kb_to_channels())
    except TelegramAPIError as exc:
        logger.warning("Не удалось доставить сообщение об ошибке: %s", exc)


def register_error_handlers(dispatcher: Dispatcher) -> None:
    """Регистрирует обработчики на диспетчере.

    Именно на диспетчере, а не на дочернем роутере: исключение всплывает вверх
    по цепочке роутеров, и обработчик на «соседнем» роутере никогда бы не
    сработал. Порядок важен — от частных типов к общему ``Exception``.
    """
    dispatcher.errors.register(handle_known_errors, ExceptionTypeFilter(ParserError, RepositoryError))
    dispatcher.errors.register(handle_flood, ExceptionTypeFilter(TelegramRetryAfter))
    dispatcher.errors.register(handle_forbidden, ExceptionTypeFilter(TelegramForbiddenError))
    dispatcher.errors.register(handle_unexpected, ExceptionTypeFilter(Exception))
