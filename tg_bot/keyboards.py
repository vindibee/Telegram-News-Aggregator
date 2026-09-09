"""Сборка инлайн-клавиатур."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import tzinfo

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import MAX_BUTTON_TEXT_LENGTH, Channel
from db.models import Post
from tg_bot.callbacks import ACTION_CHANNELS, ChannelCB, MenuCB, PostCB, RefreshCB
from tg_bot.utils import shorten


def kb_channels(channels: Sequence[Channel]) -> InlineKeyboardMarkup:
    """Клавиатура выбора канала."""
    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.button(
            text=shorten(channel.label, MAX_BUTTON_TEXT_LENGTH),
            callback_data=ChannelCB(username=channel.username),
        )
    builder.adjust(2)
    return builder.as_markup()


def kb_posts(
    posts: Sequence[Post],
    username: str,
    display_tz: tzinfo,
) -> InlineKeyboardMarkup:
    """Клавиатура со списком постов канала и кнопками управления."""
    builder = InlineKeyboardBuilder()

    for post in posts:
        stamp = post.post_time.astimezone(display_tz).strftime("%d.%m %H:%M")
        preview = shorten(post.content or "медиа без текста", MAX_BUTTON_TEXT_LENGTH - len(stamp) - 6)
        builder.button(text=f"📅 {stamp} | {preview}", callback_data=PostCB(id=post.id))

    builder.button(text="🔄 Обновить", callback_data=RefreshCB(username=username))
    builder.button(text="◀️ К каналам", callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_back(username: str) -> InlineKeyboardMarkup:
    """Клавиатура возврата из карточки поста."""
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад к постам", callback_data=ChannelCB(username=username))
    builder.button(text="🏠 К каналам", callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_to_channels() -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки возврата в главное меню (экраны ошибок)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ К каналам", callback_data=MenuCB(action=ACTION_CHANNELS))
    return builder.as_markup()
