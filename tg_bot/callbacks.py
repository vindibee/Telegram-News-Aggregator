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


class CabinetCB(CallbackData, prefix="cab"):
    """Навигация по разделам личного кабинета."""

    section: str


class ChannelActionCB(CallbackData, prefix="chan"):
    """Действие над подключённым каналом."""

    action: str
    channel_id: int


class KeywordActionCB(CallbackData, prefix="kw"):
    """Действие над словесным фильтром."""

    action: str
    kind: str


class PayMethodCB(CallbackData, prefix="pay"):
    """Выбор способа оплаты выбранного тарифа."""

    method: str
    option_id: str


class SearchPageCB(CallbackData, prefix="sp"):
    """Переход между страницами выдачи.

    Несёт только номер страницы: сам запрос в callback_data не
    помещается — там 64 байта, а запрос бывает длиннее. Текст
    запроса хранится в состоянии FSM, где ему и место.
    """

    page: int


class AdminCB(CallbackData, prefix="adm"):
    """Навигация по разделам панели администратора."""

    action: str


class BroadcastCB(CallbackData, prefix="bc"):
    """Управление массовой рассылкой.

    ``audience`` пустая у действий, которые аудитории не касаются
    (подтверждение, отмена, остановка): собственного типа под каждое
    действие заводить незачем, а 64 байта callback_data это переживает.
    """

    action: str
    audience: str = ""


class HelpCB(CallbackData, prefix="hlp"):
    """Открыть объяснение одной функции."""

    topic: str


class LanguageCB(CallbackData, prefix="lang"):
    """Выбрать язык интерфейса."""

    code: str


#: Действие «начать» — первый шаг знакомства с ботом.
ACTION_START = "begin"

#: Действие «открыть главное меню».
ACTION_MENU = "menu"

#: Действие «рассказать, что умеет бот».
ACTION_ABOUT = "about"

#: Действие «открыть справку по функциям».
ACTION_HELP = "help"

#: Действие «показать список команд».
ACTION_COMMANDS = "cmds"

#: Действие «показать статистику ссылок».
ACTION_STATS = "stats"

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

#: Действие «открыть поиск по архиву».
ACTION_SEARCH = "search"

#: Действие «открыть личный кабинет».
ACTION_CABINET = "cabinet"

#: Действие «показать приглашения и ссылку».
ACTION_REFERRAL = "referral"

#: Действие «ввести промокод».
ACTION_PROMO = "promo"

#: Разделы панели администратора.
ADMIN_DASHBOARD = "dash"
ADMIN_BROADCAST = "cast"
ADMIN_PROMOCODES = "promo"
ADMIN_REFERRALS = "ref"

#: Действия рассылки.
BROADCAST_PICK = "pick"
BROADCAST_START = "go"
BROADCAST_CANCEL = "no"
BROADCAST_STOP = "stop"

#: Разделы личного кабинета.
SECTION_MENU = "menu"
SECTION_SOURCES = "sources"
SECTION_TARGETS = "targets"
SECTION_KEYWORDS = "keywords"
SECTION_SUBSCRIPTION = "subscription"

#: Действия над каналом.
CHANNEL_ADD = "add"
CHANNEL_DELETE = "del"
CHANNEL_VERIFY = "check"

#: Способы оплаты.
PAY_STARS = "stars"
PAY_CRYPTO = "crypto"

#: Действия над фильтром.
KEYWORD_ADD = "add"
KEYWORD_CLEAR = "clear"
