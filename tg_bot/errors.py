"""Глобальная обработка исключений хендлеров.

Сообщение об ошибке — тоже часть интерфейса, и показывать его на чужом
языке нельзя. Локализатор сюда попадает штатным путём: aiogram передаёт
обработчику ошибки тот же словарь контекста, что и хендлеру, поэтому
``i18n`` объявляется обычным аргументом.

Он может и отсутствовать — если исключение случилось раньше, чем
отработал :class:`~tg_bot.middlewares.i18n.I18nMiddleware`. Тогда ответ
уходит на языке по умолчанию: промолчать в ответ на ошибку хуже, чем
ответить не на том языке.
"""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import CallbackQuery, ErrorEvent, Message

from core.logger import get_logger
from db.repositories import RepositoryError
from services.i18n import TranslationManager, Translator
from services.parser import ParserError
from tg_bot.keyboards import kb_to_channels

logger = get_logger(__name__)


async def handle_known_errors(event: ErrorEvent, i18n: Translator | None = None) -> bool:
    """Ожидаемые сбои инфраструктуры: показываем нейтральное сообщение.

    Текст исключений парсера и репозиториев технический и адресован
    разработчику — переводить его незачем, а показывать пользователю
    вредно: он не должен читать про SQL-ограничения и HTTP-коды.
    """
    logger.warning("Обработанная ошибка: %s", event.exception)
    await _notify(event, i18n, "common.error")
    return True


async def handle_flood(event: ErrorEvent, i18n: Translator | None = None) -> bool:
    """Flood control Telegram: повторять здесь нечего, только сообщаем."""
    logger.warning("Flood control не преодолён: %s", event.exception)
    await _notify(event, i18n, "throttle.flood")
    return True


async def handle_forbidden(event: ErrorEvent) -> bool:
    """Пользователь заблокировал бота — уведомлять некого."""
    logger.info("Бот заблокирован пользователем: %s", event.exception)
    return True


async def handle_unexpected(event: ErrorEvent, i18n: Translator | None = None) -> bool:
    """Непредвиденная ошибка: полный traceback в лог, нейтральный текст в чат."""
    logger.exception(
        "Необработанное исключение при обработке апдейта", exc_info=event.exception
    )
    await _notify(event, i18n, "common.error")
    return True


async def _notify(event: ErrorEvent, i18n: Translator | None, key: str) -> None:
    """Пытается сообщить пользователю об ошибке, не поднимая новых исключений.

    :param event: Событие с исходным исключением и обновлением.
    :param i18n: Локализатор из контекста апдейта, если он успел появиться.
    :param key: Ключ строки в каталоге переводов.
    """
    localized = i18n or _fallback_translator()
    text = localized(key)
    markup = kb_to_channels(localized)

    update = event.update
    try:
        if isinstance(update.callback_query, CallbackQuery):
            await update.callback_query.answer(text[:200], show_alert=True)
            message = update.callback_query.message
            if isinstance(message, Message):
                await message.answer(text, reply_markup=markup)
        elif isinstance(update.message, Message):
            await update.message.answer(text, reply_markup=markup)
    except TelegramAPIError as exc:
        logger.warning("Не удалось доставить сообщение об ошибке: %s", exc)


#: Локализатор на язык по умолчанию. Создаётся лениво и один раз: каталоги
#: уже загружены в память, а обработчик ошибок не должен зависеть от того,
#: успел ли собраться контекст апдейта.
_FALLBACK: Translator | None = None


def _fallback_translator() -> Translator:
    """Возвращает локализатор языка по умолчанию."""
    global _FALLBACK
    if _FALLBACK is None:
        manager = TranslationManager.from_directory()
        _FALLBACK = Translator(manager, manager.default_language)
    return _FALLBACK


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
