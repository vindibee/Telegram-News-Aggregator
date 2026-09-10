"""Сборка клавиатур с локализованными подписями.

Каждая функция принимает :class:`~services.i18n.Translator`. Передавать
язык вместо готового локализатора было бы соблазнительно, но тогда каждая
клавиатура сама решала бы, где взять каталог, и в проекте появилось бы
несколько путей к одним и тем же строкам.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import tzinfo

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import MAX_BUTTON_TEXT_LENGTH, Channel
from core.pricing import CRYPTO_ASSET, PlanOption
from db.enums import KeywordKind, Language
from db.models import Post, UserChannel
from services.i18n import Translator
from tg_bot.callbacks import (
    ACTION_PROMO,
    ACTION_REFERRAL,
    ACTION_CABINET,
    ACTION_CHANNELS,
    ACTION_LANGUAGE,
    ACTION_PLANS,
    ACTION_SEARCH,
    ACTION_SUBSCRIPTION,
    ACTION_TRIAL,
    CHANNEL_DELETE,
    CHANNEL_VERIFY,
    KEYWORD_ADD,
    KEYWORD_CLEAR,
    PAY_CRYPTO,
    PAY_STARS,
    SECTION_KEYWORDS,
    SECTION_MENU,
    SECTION_SOURCES,
    SECTION_SUBSCRIPTION,
    SECTION_TARGETS,
    CabinetCB,
    ChannelActionCB,
    ChannelCB,
    KeywordActionCB,
    LanguageCB,
    SearchPageCB,
    PayMethodCB,
    MenuCB,
    PlanCB,
    PostCB,
    RefreshCB,
)
from tg_bot.utils import shorten


def kb_channels(channels: Sequence[Channel], i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура выбора канала.

    Названия каналов не переводятся: это имена собственные, и «Хабр» на
    любом языке остаётся «Хабром».
    """
    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.button(
            text=shorten(channel.label, MAX_BUTTON_TEXT_LENGTH),
            callback_data=ChannelCB(username=channel.username),
        )
    builder.adjust(2)
    # Кнопка языка отдельной строкой под сеткой каналов: сюда попадает
    # человек, который не понимает остального интерфейса, и она должна
    # быть заметной, а не теряться среди названий каналов.
    builder.row(
        InlineKeyboardButton(
            text=i18n("buttons.search"),
            callback_data=MenuCB(action=ACTION_SEARCH).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=i18n("buttons.cabinet"),
            callback_data=MenuCB(action=ACTION_CABINET).pack(),
        ),
        InlineKeyboardButton(
            text=i18n("buttons.language"),
            callback_data=MenuCB(action=ACTION_LANGUAGE).pack(),
        ),
    )
    return builder.as_markup()


def kb_posts(
    posts: Sequence[Post],
    username: str,
    display_tz: tzinfo,
    i18n: Translator,
) -> InlineKeyboardMarkup:
    """Клавиатура со списком постов канала и кнопками управления."""
    builder = InlineKeyboardBuilder()

    for post in posts:
        stamp = post.post_time.astimezone(display_tz).strftime("%d.%m %H:%M")
        preview = shorten(
            post.content or i18n("post.empty"), MAX_BUTTON_TEXT_LENGTH - len(stamp) - 6
        )
        builder.button(text=f"📅 {stamp} | {preview}", callback_data=PostCB(id=post.id))

    builder.button(text=i18n("buttons.refresh"), callback_data=RefreshCB(username=username))
    builder.button(
        text=i18n("buttons.back_to_channels"), callback_data=MenuCB(action=ACTION_CHANNELS)
    )
    builder.adjust(1)
    return builder.as_markup()


def kb_back(username: str, i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура возврата из карточки поста."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("buttons.back_to_posts"), callback_data=ChannelCB(username=username))
    builder.button(text=i18n("buttons.home"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_to_channels(i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки возврата в главное меню (экраны ошибок)."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=i18n("buttons.back_to_channels"), callback_data=MenuCB(action=ACTION_CHANNELS)
    )
    return builder.as_markup()


def kb_plans(options: Sequence[PlanOption], i18n: Translator) -> InlineKeyboardMarkup:
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
    builder.button(text=i18n("buttons.back"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_pay_methods(
    option: PlanOption,
    i18n: Translator,
    *,
    crypto_enabled: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура выбора способа оплаты.

    Криптовалюта показывается только когда она настроена: кнопка,
    ведущая в сообщение «временно недоступно», хуже её отсутствия.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=i18n("billing.pay_stars", amount=f"{option.stars} ⭐"),
        callback_data=PayMethodCB(method=PAY_STARS, option_id=option.id),
    )
    if crypto_enabled:
        builder.button(
            text=i18n(
                "billing.pay_crypto",
                amount=option.usdt,
                asset=CRYPTO_ASSET,
            ),
            callback_data=PayMethodCB(method=PAY_CRYPTO, option_id=option.id),
        )
    builder.button(text=i18n("buttons.back"), callback_data=MenuCB(action=ACTION_PLANS))
    builder.adjust(1)
    return builder.as_markup()


def kb_crypto_invoice(pay_url: str, i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура со ссылкой на оплату счёта в CryptoBot."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("billing.crypto_button"), url=pay_url)
    builder.button(text=i18n("buttons.my_subscription"), callback_data=MenuCB(action=ACTION_SUBSCRIPTION))
    builder.adjust(1)
    return builder.as_markup()


def kb_subscription(
    has_subscription: bool,
    i18n: Translator,
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
            text=i18n("buttons.trial", days=i18n.plural("units.days", trial_days)),
            callback_data=MenuCB(action=ACTION_TRIAL),
        )
    builder.button(
        text=i18n("buttons.extend") if has_subscription else i18n("buttons.buy"),
        callback_data=MenuCB(action=ACTION_PLANS),
    )
    builder.button(text=i18n("buttons.channels"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_request_contact(i18n: Translator) -> ReplyKeyboardMarkup:
    """Клавиатура запроса телефона.

    Это единственный способ получить номер, подтверждённый самим Telegram:
    введённый вручную текст ничего не доказывает, а контакт из адресной
    книги принадлежит другому человеку.

    ``one_time_keyboard`` не отменяет необходимости убирать клавиатуру
    явно: флаг лишь сворачивает её на клиенте, а не снимает.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=i18n("buttons.share_phone"), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=True,
    )


def kb_hide_contact_request() -> ReplyKeyboardRemove:
    """Убирает клавиатуру запроса телефона."""
    return ReplyKeyboardRemove()


def kb_trial_declined(i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура после отказа от подтверждения телефона."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("buttons.buy"), callback_data=MenuCB(action=ACTION_PLANS))
    builder.button(text=i18n("buttons.channels"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_after_trial(i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура после успешной активации пробного периода."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("buttons.channels"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.button(
        text=i18n("buttons.my_subscription"), callback_data=MenuCB(action=ACTION_SUBSCRIPTION)
    )
    builder.adjust(1)
    return builder.as_markup()


def kb_after_payment(i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура после успешной оплаты."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("buttons.channels"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.button(
        text=i18n("buttons.my_subscription"), callback_data=MenuCB(action=ACTION_SUBSCRIPTION)
    )
    builder.adjust(1)
    return builder.as_markup()


def kb_languages(current: Language, i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура выбора языка интерфейса.

    Названия языков намеренно записаны на них самих: человек, попавший в
    бот с незнакомым ему языком интерфейса, должен узнать свой вариант, не
    понимая остального текста. Текущий язык помечается галочкой.
    """
    builder = InlineKeyboardBuilder()
    for language in Language:
        label = i18n(f"buttons.language_{language.value}")
        builder.button(
            text=f"✅ {label}" if language is current else label,
            callback_data=LanguageCB(code=language.value),
        )
    builder.button(text=i18n("buttons.channels"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1)
    return builder.as_markup()


def kb_cabinet(i18n: Translator) -> InlineKeyboardMarkup:
    """Главное меню личного кабинета."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("buttons.cab_sources"), callback_data=CabinetCB(section=SECTION_SOURCES))
    builder.button(text=i18n("buttons.cab_targets"), callback_data=CabinetCB(section=SECTION_TARGETS))
    builder.button(text=i18n("buttons.cab_keywords"), callback_data=CabinetCB(section=SECTION_KEYWORDS))
    builder.button(
        text=i18n("buttons.cab_subscription"), callback_data=CabinetCB(section=SECTION_SUBSCRIPTION)
    )
    builder.button(
        text=i18n("buttons.referral"), callback_data=MenuCB(action=ACTION_REFERRAL)
    )
    builder.button(
        text=i18n("buttons.promo"), callback_data=MenuCB(action=ACTION_PROMO)
    )
    builder.button(text=i18n("buttons.channels"), callback_data=MenuCB(action=ACTION_CHANNELS))
    builder.adjust(1, 1, 1, 1, 2, 1)
    return builder.as_markup()


def kb_channel_list(
    channels: Sequence[UserChannel],
    i18n: Translator,
    *,
    add_button: str,
    show_status: bool = False,
) -> InlineKeyboardMarkup:
    """Список подключённых каналов с кнопками удаления.

    Каждая строка — сам канал и корзина рядом. Отдельный экран «выберите,
    что удалить» стоил бы лишнего шага в самом частом действии раздела.

    :param channels: Подключённые каналы.
    :param i18n: Локализатор.
    :param add_button: Ключ подписи кнопки добавления.
    :param show_status: Показывать ли готовность к публикации.
    """
    builder = InlineKeyboardBuilder()

    for channel in channels:
        label = channel.display_name
        if show_status:
            status = (
                i18n("cabinet.targets.status_ok")
                if channel.bot_is_admin
                else i18n("cabinet.targets.status_no_rights")
            )
            label = f"{label} · {status}"
        builder.row(
            InlineKeyboardButton(
                text=shorten(label, MAX_BUTTON_TEXT_LENGTH - 4),
                callback_data=ChannelActionCB(action=CHANNEL_VERIFY, channel_id=channel.id).pack(),
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=ChannelActionCB(action=CHANNEL_DELETE, channel_id=channel.id).pack(),
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text=i18n(add_button),
            callback_data=CabinetCB(section=SECTION_SOURCES if not show_status else SECTION_TARGETS).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=i18n("buttons.cab_back"),
            callback_data=CabinetCB(section=SECTION_MENU).pack(),
        )
    )
    return builder.as_markup()


def kb_keywords(i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура раздела словесного фильтра."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=i18n("buttons.add_triggers"),
        callback_data=KeywordActionCB(action=KEYWORD_ADD, kind=KeywordKind.TRIGGER.value),
    )
    builder.button(
        text=i18n("buttons.add_stop_words"),
        callback_data=KeywordActionCB(action=KEYWORD_ADD, kind=KeywordKind.STOP.value),
    )
    builder.button(
        text=i18n("buttons.clear_triggers"),
        callback_data=KeywordActionCB(action=KEYWORD_CLEAR, kind=KeywordKind.TRIGGER.value),
    )
    builder.button(
        text=i18n("buttons.clear_stop_words"),
        callback_data=KeywordActionCB(action=KEYWORD_CLEAR, kind=KeywordKind.STOP.value),
    )
    builder.button(text=i18n("buttons.cab_back"), callback_data=CabinetCB(section=SECTION_MENU))
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def kb_cabinet_back(i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки возврата в кабинет."""
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("buttons.cab_back"), callback_data=CabinetCB(section=SECTION_MENU))
    return builder.as_markup()


def kb_cabinet_subscription(
    has_subscription: bool,
    i18n: Translator,
    *,
    trial_available: bool = False,
    trial_days: int = 0,
) -> InlineKeyboardMarkup:
    """Клавиатура раздела подписки внутри кабинета."""
    builder = InlineKeyboardBuilder()
    if trial_available:
        builder.button(
            text=i18n("buttons.trial", days=i18n.plural("units.days", trial_days)),
            callback_data=MenuCB(action=ACTION_TRIAL),
        )
    builder.button(
        text=i18n("buttons.extend") if has_subscription else i18n("buttons.buy"),
        callback_data=MenuCB(action=ACTION_PLANS),
    )
    builder.button(text=i18n("buttons.cab_back"), callback_data=CabinetCB(section=SECTION_MENU))
    builder.adjust(1)
    return builder.as_markup()


def kb_renew(i18n: Translator) -> InlineKeyboardMarkup:
    """Клавиатура уведомления об окончании подписки.

    Одна кнопка и ничего больше: уведомление приходит без спроса, и
    единственное осмысленное действие в этот момент — продлить.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=i18n("buttons.extend"), callback_data=MenuCB(action=ACTION_PLANS))
    return builder.as_markup()


def kb_search_results(
    page: int,
    i18n: Translator,
    *,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура постраничной выдачи поиска.

    Кнопка показывается только когда есть куда идти: неактивная стрелка,
    которая ничего не делает, раздражает сильнее, чем её отсутствие.
    """
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []

    if has_prev:
        row.append(
            InlineKeyboardButton(
                text=i18n("buttons.prev_page"),
                callback_data=SearchPageCB(page=page - 1).pack(),
            )
        )
    if has_next:
        row.append(
            InlineKeyboardButton(
                text=i18n("buttons.next_page"),
                callback_data=SearchPageCB(page=page + 1).pack(),
            )
        )
    if row:
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(
            text=i18n("buttons.channels"),
            callback_data=MenuCB(action=ACTION_CHANNELS).pack(),
        )
    )
    return builder.as_markup()
