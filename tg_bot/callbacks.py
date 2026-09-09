"""Типизированные callback-данные инлайн-кнопок.

Telegram ограничивает callback_data 64 байтами, поэтому в payload хранится
минимум: идентификаторы, а не сами данные.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    """Навигация по статичным экранам бота."""

    action: str


class ChannelCB(CallbackData, prefix="ch"):
    """Открыть список постов канала."""

    username: str


class RefreshCB(CallbackData, prefix="upd"):
    """Принудительно обновить посты канала."""

    username: str


class PostCB(CallbackData, prefix="post"):
    """Открыть конкретный пост по первичному ключу."""

    id: int


#: Действие «вернуться к списку каналов».
ACTION_CHANNELS = "channels"
