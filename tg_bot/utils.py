"""Вспомогательные утилиты слоя Telegram."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final, TypeVar

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from core.config import MAX_MESSAGE_LENGTH
from core.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

_NOT_MODIFIED: Final[str] = "message is not modified"
_MAX_FLOOD_WAIT: Final[float] = 30.0


def get_message(callback: CallbackQuery) -> Message | None:
    """Возвращает доступное для редактирования сообщение колбэка.

    Для старых сообщений Telegram присылает ``InaccessibleMessage``, у которого
    нет ни текста, ни методов редактирования — обращение к ним падало бы.
    """
    message = callback.message
    return message if isinstance(message, Message) else None


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Редактирует сообщение, гася предсказуемые ошибки Bot API.

    «message is not modified» — штатная ситуация (пользователь нажал ту же
    кнопку), остальные ошибки редактирования обрабатываются отправкой нового
    сообщения, чтобы диалог не «зависал».
    """
    try:
        await with_flood_retry(
            lambda: message.edit_text(text=text, reply_markup=reply_markup)
        )
    except TelegramBadRequest as exc:
        if _NOT_MODIFIED in str(exc).lower():
            return
        logger.info("Не удалось отредактировать сообщение (%s), отправляю новое", exc)
        await message.answer(text=text, reply_markup=reply_markup)


async def with_flood_retry(action: Callable[[], Awaitable[T]], attempts: int = 2) -> T:
    """Выполняет вызов Bot API, повторяя его при срабатывании flood control."""
    total = max(1, attempts)
    for attempt in range(1, total + 1):
        try:
            return await action()
        except TelegramRetryAfter as exc:
            if attempt == total:
                raise
            delay = min(float(exc.retry_after), _MAX_FLOOD_WAIT)
            logger.warning("Flood control: повтор через %.1f с (попытка %d из %d)", delay, attempt, total)
            await asyncio.sleep(delay)

    # Недостижимо: цикл либо возвращает результат, либо пробрасывает исключение.
    raise RuntimeError("with_flood_retry завершился без результата")


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Режет текст на части, укладывающиеся в лимит сообщения Telegram.

    Разрыв делается по границе строки, а при её отсутствии — жёстко по лимиту.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line

    if current:
        chunks.append(current)

    return [chunk.strip("\n") for chunk in chunks if chunk.strip()]


def shorten(text: str, limit: int) -> str:
    """Обрезает строку до ``limit`` символов, добавляя многоточие."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"
