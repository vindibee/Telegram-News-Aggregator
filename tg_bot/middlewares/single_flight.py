"""Защита от повторных нажатий на инлайн-кнопки."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, TelegramObject, User

from core.logger import get_logger
from services.i18n import Translator
from services.ratelimit.base import KeyGuard
from tg_bot.flags import NO_SINGLE_FLIGHT_FLAG
from tg_bot.middlewares.i18n import I18N_KEY

logger = get_logger(__name__)



class SingleFlightMiddleware(BaseMiddleware):
    """Не позволяет одному нажатию обрабатываться дважды одновременно.

    Ограничение частоты здесь не помогает: два нажатия подряд укладываются
    в любой разумный лимит, но запускают дорогую операцию (парсинг канала,
    выставление счёта) дважды. Поэтому на время обработки берётся
    блокировка по паре «пользователь + содержимое кнопки», а повторные
    нажатия получают вежливый отказ.

    Блокировка снимается в ``finally`` и в любом случае истекает по TTL,
    так что зависший хендлер не заблокирует кнопку навсегда.
    """

    def __init__(self, guard: KeyGuard, ttl: float = 5.0) -> None:
        if ttl <= 0:
            raise ValueError(f"TTL блокировки должен быть положительным, получено: {ttl}")
        self._guard = guard
        self._ttl = ttl

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or get_flag(data, NO_SINGLE_FLIGHT_FLAG, default=False):
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        key = self._build_key(user.id, event.data)
        token = await self._guard.acquire_once(key, self._ttl)
        if token is None:
            logger.info(
                "Повторное нажатие отклонено: user_id=%s, callback=%r", user.id, event.data
            )
            await self._reject(event, data.get(I18N_KEY))
            return None

        try:
            return await handler(event, data)
        finally:
            await self._guard.release(key, token)

    @staticmethod
    def _build_key(user_id: int, callback_data: str | None) -> str:
        """Строит ключ блокировки.

        Содержимое кнопки хэшируется: оно приходит от клиента, может быть
        любым и не должно напрямую попадать в имена ключей хранилища.
        """
        digest = hashlib.blake2b((callback_data or "").encode("utf-8"), digest_size=8).hexdigest()
        return f"single-flight:{user_id}:{digest}"

    @staticmethod
    async def _reject(event: CallbackQuery, i18n: Translator | None) -> None:
        """Гасит «часики» на кнопке, не создавая сообщений в чате."""
        try:
            await event.answer(i18n("common.busy") if i18n else "")
        except TelegramAPIError as exc:
            logger.warning("Не удалось ответить на повторное нажатие: %s", exc)
