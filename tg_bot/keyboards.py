"""Сборка инлайн-клавиатур."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import tzinfo

from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import MAX_BUTTON_TEXT_LENGTH, Channel
from core.pricing import PlanOption
from db.models import Post
from tg_bot.callbacks import (
    ACTION_CHANNELS,
    ACTION_PLANS,
    ACTION_SUBSCRIPTION,
    ACTION_TRIAL,
    ChannelCB,
    MenuCB,
    PlanCB,
    PostCB,
    RefreshCB,
)
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


def kb_plans(options: Sequence[PlanOption]) -> InlineKeyboardMarkup:
    """Клавиатура выбора тарифа.

    Цена выносится в текст кнопки: пользователь видит сумму до открытия
    счёта, а не после.
    """
    builder = InlineKeyboardBuilder()
    for option in options:
        builder.button(
            text=f"{option.title} — {option.stars} ⭐",
            callback_data=PlanCB(option_id=option.id),
        )
    builder.button(text="◀️ Назад", callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_subscription(
    has_subscription: bool,
    *,
    trial_available: bool = False,
    trial_days: int = 0,
) -> InlineKeyboardMarkup:
    """Клавиатура экрана подписки.

    Кнопка триала показывается только когда он действительно доступен:
    предлагать бесплатный период тому, кто его уже использовал, — верный
    способ получить отказ в ответ на собственное предложение.
    """
    builder = InlineKeyboardBuilder()
    if trial_available:
        builder.button(
            text=f"🎁 Пробный период на {trial_days} дн.",
            callback_data=MenuCB(action=ACTION_TRIAL),
        )
    builder.button(
        text="⭐ Продлить" if has_subscription else "⭐ Оформить подписку",
        callback_data=MenuCB(action=ACTION_PLANS),
    )
    builder.button(text="📡 К каналам", callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_request_contact(prompt: str = "📱 Поделиться номером") -> ReplyKeyboardMarkup:
    """Клавиатура запроса телефона.

    Это единственный способ получить номер, подтверждённый самим Telegram:
    введённый вручную текст ничего не доказывает, а контакт из адресной
    книги принадлежит другому человеку.

    ``one_time_keyboard`` не отменяет необходимости убирать клавиатуру
    явно: флаг лишь сворачивает её на клиенте, а не снимает.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=prompt, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже",
        selective=True,
    )


def kb_hide_contact_request() -> ReplyKeyboardRemove:
    """Убирает клавиатуру запроса телефона."""
    return ReplyKeyboardRemove()


def kb_trial_declined() -> InlineKeyboardMarkup:
    """Клавиатура после отказа от подтверждения телефона."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ Оформить подписку", callback_data=MenuCB(action=ACTION_PLANS))
    builder.button(text="📡 К каналам", callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_after_trial() -> InlineKeyboardMarkup:
    """Клавиатура после успешной активации пробного периода."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📡 К каналам", callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.button(text="💳 Моя подписка", callback_data=MenuCB(action=ACTION_SUBSCRIPTION))
    builder.adjust(1)
    return builder.as_markup()


def kb_after_payment() -> InlineKeyboardMarkup:
    """Клавиатура после успешной оплаты."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📡 К каналам", callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.button(text="💳 Моя подписка", callback_data=MenuCB(action=ACTION_SUBSCRIPTION))
    builder.adjust(1)
    return builder.as_markup()
