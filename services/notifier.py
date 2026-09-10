"""Отправка служебных уведомлений пользователям.

Рассылка из фонового процесса отличается от ответа в диалоге двумя вещами.
Во-первых, получателей много, и Telegram ограничивает исходящий поток
примерно тридцатью сообщениями в секунду — превышение приводит к flood
control на весь бот, а не только на одну рассылку. Во-вторых, часть
получателей уже заблокировала бота: такие адресаты должны отсеиваться
навсегда, а не пытаться получить сообщение при каждом запуске.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

from core.logger import get_logger
from services.ratelimit.base import RateLimiter, RateLimitRule

logger = get_logger(__name__)

#: Глобальный лимит исходящих сообщений бота (ограничение Bot API).
GLOBAL_SEND_RULE: Final[RateLimitRule] = RateLimitRule(
    limit=25, window=1.0, burst=25, scope="outbound"
)

#: Ключ общего ведра: лимит распространяется на бота целиком, а не на чат.
_GLOBAL_KEY: Final[str] = "bot"

#: Верхняя граница ожидания при flood control.
_MAX_FLOOD_WAIT: Final[float] = 60.0


class DeliveryStatus:
    """Итог доставки одного сообщения."""

    DELIVERED: Final[str] = "delivered"
    BLOCKED: Final[str] = "blocked"
    FAILED: Final[str] = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Результат отправки уведомления конкретному пользователю."""

    telegram_id: int
    status: str

    @property
    def delivered(self) -> bool:
        """Доставлено ли сообщение."""
        return self.status == DeliveryStatus.DELIVERED

    @property
    def blocked(self) -> bool:
        """Заблокировал ли пользователь бота."""
        return self.status == DeliveryStatus.BLOCKED


class TelegramNotifier:
    """Отправляет сообщения с соблюдением лимитов Bot API.

    Экземпляр создаётся один на процесс: общее ведро жетонов имеет смысл
    только когда через него проходят все исходящие сообщения.
    """

    def __init__(
        self,
        bot: Bot,
        limiter: RateLimiter,
        *,
        rule: RateLimitRule = GLOBAL_SEND_RULE,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"Число попыток должно быть не меньше 1, получено: {max_attempts}")
        self._bot = bot
        self._limiter = limiter
        self._rule = rule
        self._max_attempts = max_attempts

    async def send(
        self,
        telegram_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> DeliveryResult:
        """Отправляет сообщение одному пользователю.

        Исключения наружу не выпускаются: сбой доставки одному адресату не
        должен прерывать рассылку остальным. Итог возвращается значением,
        чтобы вызывающий код мог отличить блокировку от временной ошибки.

        :param telegram_id: Получатель.
        :param text: Текст сообщения (HTML).
        :param reply_markup: Клавиатура.
        :return: Результат доставки.
        """
        for attempt in range(1, self._max_attempts + 1):
            await self._await_slot()
            try:
                await self._bot.send_message(
                    chat_id=telegram_id, text=text, reply_markup=reply_markup
                )
            except TelegramForbiddenError:
                # Пользователь заблокировал бота или удалил чат. Повторять
                # бессмысленно — адресат исключается из будущих рассылок.
                logger.info("Пользователь %s недоступен: бот заблокирован", telegram_id)
                return DeliveryResult(telegram_id, DeliveryStatus.BLOCKED)
            except TelegramRetryAfter as exc:
                delay = min(float(exc.retry_after), _MAX_FLOOD_WAIT)
                logger.warning(
                    "Flood control при отправке %s: пауза %.1f с (попытка %d из %d)",
                    telegram_id, delay, attempt, self._max_attempts,
                )
                await asyncio.sleep(delay)
            except TelegramAPIError as exc:
                logger.warning(
                    "Ошибка отправки пользователю %s (попытка %d из %d): %s",
                    telegram_id, attempt, self._max_attempts, exc,
                )
                if attempt == self._max_attempts:
                    return DeliveryResult(telegram_id, DeliveryStatus.FAILED)
                await asyncio.sleep(min(2 ** (attempt - 1), 5))
            else:
                return DeliveryResult(telegram_id, DeliveryStatus.DELIVERED)

        return DeliveryResult(telegram_id, DeliveryStatus.FAILED)

    async def reserve_slot(self) -> None:
        """Дожидается свободного жетона перед отправкой чужим кодом.

        Нужен тем, кто обращается к Bot API мимо :meth:`send` — например,
        публикатору, который копирует сообщения. Лимит исходящих
        распространяется на бота целиком, поэтому ведро должно быть одно
        на все отправки, а не своё у каждого места в коде.
        """
        await self._await_slot()

    async def _await_slot(self) -> None:
        """Дожидается свободного жетона в общем ведре исходящих сообщений.

        Ожидание вместо отказа: рассылка не срочная, и притормозить её
        дешевле, чем получить flood control на всего бота.
        """
        while True:
            decision = await self._limiter.acquire(_GLOBAL_KEY, self._rule)
            if decision.allowed:
                return
            await asyncio.sleep(max(decision.retry_after, 0.01))
