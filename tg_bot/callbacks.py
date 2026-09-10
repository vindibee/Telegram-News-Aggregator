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


class PlanCB(CallbackData, prefix="plan"):
    """Выставить счёт на выбранный тариф."""

    option_id: str


class LanguageCB(CallbackData, prefix="lang"):
    """Выбрать язык интерфейса."""

    code: str


#: Действие «вернуться к списку каналов».
ACTION_CHANNELS = "channels"

#: Действие «показать тарифы».
ACTION_PLANS = "plans"

#: Действие «показать состояние подписки».
ACTION_SUBSCRIPTION = "subscription"

#: Действие «активировать пробный период».
ACTION_TRIAL = "trial"

#: Действие «показать выбор языка».
ACTION_LANGUAGE = "language"
