"""Личный кабинет: источники, каналы автопостинга и словесный фильтр.

Три сценария настройки построены на FSM, потому что каждый требует
свободного ввода: ссылку на канал, пересланное сообщение или список слов
нельзя уместить в callback_data — там 64 байта и данные приходят от
клиента.

Валидация везде идёт до записи в базу и опирается на внешний источник
правды, а не на форму строки. Источник проверяется попыткой прочитать
канал: имя вида ``@channel`` бывает синтаксически безупречным и при этом
принадлежать закрытому или несуществующему каналу. Цель публикации
проверяется через ``get_chat_member``: единственный способ узнать, может
ли бот туда писать, — спросить об этом Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message

from core.config import Settings
from core.logger import get_logger
from db.enums import ChannelKind, KeywordKind, SubscriptionStatus
from db.models import User, UserChannel, UserKeyword
from db.uow import UnitOfWork
from services.i18n import Translator
from services.parser import ChannelUnavailableError, ParserError, TelegramWebParser
from services.trial import TrialService
from tg_bot.callbacks import (
    ACTION_CABINET,
    CHANNEL_DELETE,
    CHANNEL_VERIFY,
    KEYWORD_ADD,
    KEYWORD_CLEAR,
    SECTION_KEYWORDS,
    SECTION_MENU,
    SECTION_SOURCES,
    SECTION_SUBSCRIPTION,
    SECTION_TARGETS,
    CabinetCB,
    ChannelActionCB,
    KeywordActionCB,
    MenuCB,
)
from tg_bot.flags import rate_limit
from tg_bot.keyboards import (
    kb_cabinet,
    kb_cabinet_back,
    kb_cabinet_subscription,
    kb_channel_list,
    kb_keywords,
)
from tg_bot.states import AddSourceChannelSG, AddTargetChannelSG, SetKeywordsSG
from tg_bot.utils import get_message, safe_edit_text

logger = get_logger(__name__)

router = Router(name="cabinet")

#: Сколько каналов каждого вида можно подключить. Ограничение защищает от
#: неограниченного роста нагрузки на парсер и от разрастания очереди.
#: Когда лимиты станут частью тарифа, они переедут в каталог планов.
MAX_SOURCES: Final[int] = 20
MAX_TARGETS: Final[int] = 5

#: Предел на размер словаря фильтра одного вида.
MAX_KEYWORDS: Final[int] = 100

#: Максимальная длина одного слова — совпадает с колонкой в базе.
MAX_KEYWORD_LENGTH: Final[int] = 64

#: Сколько записей канала достаточно, чтобы признать источник читаемым.
_PROBE_LIMIT: Final[int] = 1


# --------------------------------------------------------------------------- #
# Главное меню
# --------------------------------------------------------------------------- #


@router.message(Command("cabinet"), **rate_limit(10, 60, scope="cabinet"))
async def cmd_cabinet(message: Message, user: User, uow: UnitOfWork, i18n: Translator, state: FSMContext) -> None:
    """Открывает личный кабинет командой."""
    await state.clear()
    await message.answer(await _menu_text(user, uow, i18n), reply_markup=kb_cabinet(i18n))


@router.callback_query(MenuCB.filter(F.action == ACTION_CABINET))
async def open_cabinet(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Открывает личный кабинет по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return
    await state.clear()
    await safe_edit_text(target, await _menu_text(user, uow, i18n), kb_cabinet(i18n))


@router.callback_query(CabinetCB.filter(F.section == SECTION_MENU))
async def back_to_cabinet(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Возвращает в главное меню кабинета, прерывая начатый ввод."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return
    await state.clear()
    await safe_edit_text(target, await _menu_text(user, uow, i18n), kb_cabinet(i18n))


async def _menu_text(user: User, uow: UnitOfWork, i18n: Translator) -> str:
    """Собирает сводку кабинета."""
    sources = await uow.channels.count_active(user.id, ChannelKind.SOURCE)
    targets = await uow.channels.count_active(user.id, ChannelKind.TARGET)
    keywords = await uow.keywords.count_for_user(user.id)
    return i18n("cabinet.menu", sources=sources, targets=targets, keywords=keywords)


# --------------------------------------------------------------------------- #
# Источники
# --------------------------------------------------------------------------- #


@router.callback_query(CabinetCB.filter(F.section == SECTION_SOURCES))
async def show_sources(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Показывает список источников и запрашивает новый."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    channels = await uow.channels.list_for_user(user.id, ChannelKind.SOURCE)
    if len(channels) >= MAX_SOURCES:
        await state.clear()
        await safe_edit_text(
            target,
            i18n("cabinet.sources.limit", limit=MAX_SOURCES),
            kb_channel_list(channels, i18n, add_button="buttons.add_source"),
        )
        return

    await state.set_state(AddSourceChannelSG.waiting_for_link)
    header = i18n("cabinet.sources.title" if channels else "cabinet.sources.empty")
    await safe_edit_text(
        target,
        f"{header}\n\n{i18n('cabinet.sources.prompt')}",
        kb_channel_list(channels, i18n, add_button="buttons.add_source"),
    )


@router.message(AddSourceChannelSG.waiting_for_link, Command("cancel"))
async def cancel_source(message: Message, i18n: Translator, state: FSMContext) -> None:
    """Прерывает добавление источника."""
    await state.clear()
    await message.answer(i18n("cabinet.cancelled"), reply_markup=kb_cabinet_back(i18n))


@router.message(AddSourceChannelSG.waiting_for_link, F.text)
async def receive_source(
    message: Message,
    user: User,
    uow: UnitOfWork,
    parser: TelegramWebParser,
    settings: Settings,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Проверяет присланный канал и подключает его как источник."""
    raw = message.text or ""
    try:
        username = UserChannel.normalize_username(raw)
    except ValueError:
        logger.info("Некорректное имя канала от пользователя %s: %r", user.id, raw[:64])
        await message.answer(i18n("cabinet.sources.invalid"))
        return

    if await uow.channels.count_active(user.id, ChannelKind.SOURCE) >= MAX_SOURCES:
        await state.clear()
        await message.answer(
            i18n("cabinet.sources.limit", limit=MAX_SOURCES), reply_markup=kb_cabinet_back(i18n)
        )
        return

    probe = await message.answer(i18n("cabinet.sources.checking"))

    # Проверяем не форму строки, а саму доступность канала: синтаксически
    # верное имя вполне может принадлежать закрытому или несуществующему.
    try:
        await parser.fetch_posts(username, limit=_PROBE_LIMIT)
    except ChannelUnavailableError:
        logger.info("Канал @%s недоступен для пользователя %s", username, user.id)
        await probe.edit_text(i18n("cabinet.sources.unavailable", channel=escape(username)))
        return
    except ParserError as exc:
        logger.warning("Сбой проверки канала @%s: %s", username, exc)
        await probe.edit_text(i18n("cabinet.sources.network"))
        return

    result = await uow.channels.connect(
        user_id=user.id,
        kind=ChannelKind.SOURCE,
        username=username,
        title=f"@{username}",
    )
    await state.clear()

    key = "cabinet.sources.added" if result.created else "cabinet.sources.exists"
    channels = await uow.channels.list_for_user(user.id, ChannelKind.SOURCE)
    await probe.edit_text(
        i18n(key, channel=escape(username)),
        reply_markup=kb_channel_list(channels, i18n, add_button="buttons.add_source"),
    )


# --------------------------------------------------------------------------- #
# Каналы для автопостинга
# --------------------------------------------------------------------------- #


@router.callback_query(CabinetCB.filter(F.section == SECTION_TARGETS))
async def show_targets(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Показывает целевые каналы и запрашивает новый."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    channels = await uow.channels.list_for_user(user.id, ChannelKind.TARGET)
    if len(channels) >= MAX_TARGETS:
        await state.clear()
        await safe_edit_text(
            target,
            i18n("cabinet.targets.limit", limit=MAX_TARGETS),
            kb_channel_list(channels, i18n, add_button="buttons.add_target", show_status=True),
        )
        return

    await state.set_state(AddTargetChannelSG.waiting_for_channel)
    header = i18n("cabinet.targets.title" if channels else "cabinet.targets.empty")
    await safe_edit_text(
        target,
        f"{header}\n\n{i18n('cabinet.targets.prompt')}",
        kb_channel_list(channels, i18n, add_button="buttons.add_target", show_status=True),
    )


@router.message(AddTargetChannelSG.waiting_for_channel, Command("cancel"))
async def cancel_target(message: Message, i18n: Translator, state: FSMContext) -> None:
    """Прерывает привязку целевого канала."""
    await state.clear()
    await message.answer(i18n("cabinet.cancelled"), reply_markup=kb_cabinet_back(i18n))


@router.message(AddTargetChannelSG.waiting_for_channel)
async def receive_target(
    message: Message,
    user: User,
    uow: UnitOfWork,
    bot: Bot,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Проверяет права бота в канале и привязывает его для публикаций."""
    reference = _extract_channel_reference(message)
    if reference is None:
        await message.answer(i18n("cabinet.targets.not_found"))
        return

    if await uow.channels.count_active(user.id, ChannelKind.TARGET) >= MAX_TARGETS:
        await state.clear()
        await message.answer(
            i18n("cabinet.targets.limit", limit=MAX_TARGETS), reply_markup=kb_cabinet_back(i18n)
        )
        return

    probe = await message.answer(i18n("cabinet.targets.checking"))

    try:
        chat = await bot.get_chat(reference)
    except TelegramAPIError as exc:
        logger.info("Канал %r не найден для пользователя %s: %s", reference, user.id, exc)
        await probe.edit_text(i18n("cabinet.targets.not_found"))
        return

    if chat.type != ChatType.CHANNEL:
        await probe.edit_text(i18n("cabinet.targets.not_a_channel"))
        return

    title = chat.title or str(chat.id)
    verdict = await _check_bot_rights(bot, chat.id)
    if verdict is not True:
        await probe.edit_text(i18n(verdict, channel=escape(title)))
        return

    result = await uow.channels.connect(
        user_id=user.id,
        kind=ChannelKind.TARGET,
        username=chat.username,
        chat_id=chat.id,
        title=title,
    )
    await uow.channels.set_bot_admin(result.channel.id, is_admin=True)
    await state.clear()

    key = "cabinet.targets.added" if result.created else "cabinet.targets.exists"
    channels = await uow.channels.list_for_user(user.id, ChannelKind.TARGET)
    await probe.edit_text(
        i18n(key, channel=escape(title)),
        reply_markup=kb_channel_list(
            channels, i18n, add_button="buttons.add_target", show_status=True
        ),
    )


def _extract_channel_reference(message: Message) -> str | int | None:
    """Достаёт из сообщения ссылку на канал.

    Принимаются два способа: имя канала текстом и пересланное из него
    сообщение. Второй способ важнее — у приватного канала имени нет
    вовсе, и указать его иначе как пересылкой невозможно.

    :param message: Сообщение пользователя.
    :return: Имя с «@» либо числовой идентификатор чата; ``None``, если
        сослаться не на что.
    """
    origin: Chat | None = getattr(message, "forward_from_chat", None)
    if isinstance(origin, Chat):
        return origin.id

    text = (message.text or "").strip()
    if not text:
        return None

    try:
        return f"@{UserChannel.normalize_username(text)}"
    except ValueError:
        return None


async def _check_bot_rights(bot: Bot, chat_id: int) -> bool | str:
    """Проверяет, может ли бот публиковать в канале.

    Единственный достоверный источник — сам Telegram: настройки прав
    меняются в любой момент и без ведома бота.

    :param bot: Экземпляр бота.
    :param chat_id: Идентификатор канала.
    :return: ``True`` либо ключ перевода с причиной отказа.
    """
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except TelegramBadRequest as exc:
        logger.info("Не удалось прочитать права бота в канале %s: %s", chat_id, exc)
        return "cabinet.targets.not_admin"
    except TelegramAPIError as exc:
        logger.warning("Ошибка проверки прав в канале %s: %s", chat_id, exc)
        return "cabinet.targets.not_admin"

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return "cabinet.targets.not_admin"

    # can_post_messages есть только у администратора канала и вполне может
    # быть выключен: администратор без права публикации — обычное дело.
    if not getattr(member, "can_post_messages", False):
        return "cabinet.targets.cannot_post"

    return True


# --------------------------------------------------------------------------- #
# Управление подключёнными каналами
# --------------------------------------------------------------------------- #


@router.callback_query(ChannelActionCB.filter(F.action == CHANNEL_DELETE))
async def remove_channel(
    callback: CallbackQuery,
    callback_data: ChannelActionCB,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
) -> None:
    """Отключает канал пользователя."""
    channel = await uow.channels.get_by_id(callback_data.channel_id)
    kind = channel.kind if channel is not None else ChannelKind.SOURCE

    # Владелец проверяется в самом запросе: идентификатор приходит от
    # клиента, и подобранный номер не должен удалять чужой канал.
    removed = await uow.channels.disconnect(user.id, callback_data.channel_id)
    if not removed:
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    is_target = kind is ChannelKind.TARGET
    await callback.answer(
        i18n("cabinet.targets.removed" if is_target else "cabinet.sources.removed")
    )

    message = get_message(callback)
    if message is None:
        return

    channels = await uow.channels.list_for_user(user.id, kind)
    header = i18n(
        f"cabinet.{'targets' if is_target else 'sources'}.{'title' if channels else 'empty'}"
    )
    await safe_edit_text(
        message,
        header,
        kb_channel_list(
            channels,
            i18n,
            add_button="buttons.add_target" if is_target else "buttons.add_source",
            show_status=is_target,
        ),
    )


@router.callback_query(ChannelActionCB.filter(F.action == CHANNEL_VERIFY))
async def verify_channel(
    callback: CallbackQuery,
    callback_data: ChannelActionCB,
    user: User,
    uow: UnitOfWork,
    bot: Bot,
    i18n: Translator,
) -> None:
    """Перепроверяет права бота в целевом канале.

    Права снимают так же легко, как выдают, поэтому у пользователя должен
    быть способ убедиться, что автопостинг всё ещё работает.
    """
    channel = await uow.channels.get_by_id(callback_data.channel_id)
    if channel is None or channel.user_id != user.id:
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    if channel.kind is not ChannelKind.TARGET or channel.chat_id is None:
        await callback.answer(i18n("cabinet.sources.title"), show_alert=False)
        return

    verdict = await _check_bot_rights(bot, channel.chat_id)
    granted = verdict is True
    await uow.channels.set_bot_admin(channel.id, is_admin=granted)

    await callback.answer(
        i18n("cabinet.targets.verified")
        if granted
        else i18n(str(verdict), channel=escape(channel.display_name)),
        show_alert=not granted,
    )

    message = get_message(callback)
    if message is None:
        return

    channels = await uow.channels.list_for_user(user.id, ChannelKind.TARGET)
    await safe_edit_text(
        message,
        i18n("cabinet.targets.title"),
        kb_channel_list(channels, i18n, add_button="buttons.add_target", show_status=True),
    )


# --------------------------------------------------------------------------- #
# Словесный фильтр
# --------------------------------------------------------------------------- #


@router.callback_query(CabinetCB.filter(F.section == SECTION_KEYWORDS))
async def show_keywords(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Показывает текущий фильтр."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    await state.clear()
    await safe_edit_text(target, await _keywords_text(user, uow, i18n), kb_keywords(i18n))


@router.callback_query(KeywordActionCB.filter(F.action == KEYWORD_ADD))
async def ask_keywords(
    callback: CallbackQuery,
    callback_data: KeywordActionCB,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Запрашивает список слов выбранного вида."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    kind = _parse_kind(callback_data.kind)
    if kind is None:
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    await state.set_state(
        SetKeywordsSG.waiting_for_triggers
        if kind is KeywordKind.TRIGGER
        else SetKeywordsSG.waiting_for_stop_words
    )
    prompt = (
        "cabinet.keywords.prompt_triggers"
        if kind is KeywordKind.TRIGGER
        else "cabinet.keywords.prompt_stop"
    )
    await safe_edit_text(target, i18n(prompt), kb_cabinet_back(i18n))


@router.message(SetKeywordsSG.waiting_for_triggers, Command("cancel"))
@router.message(SetKeywordsSG.waiting_for_stop_words, Command("cancel"))
async def cancel_keywords(message: Message, i18n: Translator, state: FSMContext) -> None:
    """Прерывает ввод слов."""
    await state.clear()
    await message.answer(i18n("cabinet.cancelled"), reply_markup=kb_cabinet_back(i18n))


@router.message(SetKeywordsSG.waiting_for_triggers, F.text)
async def receive_triggers(
    message: Message,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Сохраняет триггерные слова."""
    await _store_keywords(message, user, uow, i18n, state, KeywordKind.TRIGGER)


@router.message(SetKeywordsSG.waiting_for_stop_words, F.text)
async def receive_stop_words(
    message: Message,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Сохраняет стоп-слова."""
    await _store_keywords(message, user, uow, i18n, state, KeywordKind.STOP)


@router.callback_query(KeywordActionCB.filter(F.action == KEYWORD_CLEAR))
async def clear_keywords(
    callback: CallbackQuery,
    callback_data: KeywordActionCB,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Очищает список слов выбранного вида."""
    kind = _parse_kind(callback_data.kind)
    if kind is None:
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    removed = await uow.keywords.clear(user.id, kind)
    await callback.answer(i18n("cabinet.keywords.cleared", count=removed))

    target = get_message(callback)
    if target is None:
        return

    await state.clear()
    await safe_edit_text(target, await _keywords_text(user, uow, i18n), kb_keywords(i18n))


async def _store_keywords(
    message: Message,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
    kind: KeywordKind,
) -> None:
    """Разбирает список слов и сохраняет его.

    :param message: Сообщение со словами через запятую.
    :param user: Владелец фильтра.
    :param uow: Единица работы.
    :param i18n: Локализатор.
    :param state: Контекст FSM.
    :param kind: Вид слов.
    """
    words, invalid = _parse_keywords(message.text or "")

    if invalid is not None:
        await message.answer(
            i18n("cabinet.keywords.too_long", limit=MAX_KEYWORD_LENGTH, word=escape(invalid))
        )
        return

    if not words:
        await message.answer(i18n("cabinet.keywords.empty_input"))
        return

    existing = await uow.keywords.count_for_user(user.id, kind)
    if existing + len(words) > MAX_KEYWORDS:
        await state.clear()
        await message.answer(
            i18n("cabinet.keywords.limit", limit=MAX_KEYWORDS),
            reply_markup=kb_cabinet_back(i18n),
        )
        return

    result = await uow.keywords.add_many(user.id, kind, words)
    await state.clear()

    lines: list[str] = []
    if result.added:
        lines.append(i18n("cabinet.keywords.added", words=escape(", ".join(result.added))))
    if result.skipped:
        lines.append(i18n("cabinet.keywords.skipped", words=escape(", ".join(result.skipped))))
    if not lines:
        lines.append(i18n("cabinet.keywords.nothing_added"))

    await message.answer("\n".join(lines))
    await message.answer(await _keywords_text(user, uow, i18n), reply_markup=kb_keywords(i18n))


def _parse_keywords(raw: str) -> tuple[list[str], str | None]:
    """Разбирает строку со словами через запятую.

    :param raw: Пользовательский ввод.
    :return: Пара «список нормализованных слов, первое слишком длинное
        слово». Второй элемент — ``None``, если все слова подходят.
    """
    words: list[str] = []
    for chunk in raw.split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        try:
            words.append(UserKeyword.normalize(candidate))
        except ValueError:
            return [], candidate
    # dict.fromkeys сохраняет порядок ввода — человек видит свои слова в
    # том же виде, в каком их прислал.
    return list(dict.fromkeys(words)), None


def _parse_kind(raw: str) -> KeywordKind | None:
    """Разбирает вид слова из callback_data."""
    try:
        return KeywordKind(raw)
    except ValueError:
        logger.info("Неизвестный вид слова в callback_data: %r", raw)
        return None


async def _keywords_text(user: User, uow: UnitOfWork, i18n: Translator) -> str:
    """Собирает экран словесного фильтра."""
    triggers = await uow.keywords.list_for_user(user.id, KeywordKind.TRIGGER)
    stop_words = await uow.keywords.list_for_user(user.id, KeywordKind.STOP)

    return "\n\n".join(
        (
            i18n("cabinet.keywords.title"),
            i18n(
                "cabinet.keywords.triggers",
                count=len(triggers),
                words=escape(", ".join(item.word for item in triggers))
                or i18n("cabinet.keywords.none"),
            ),
            i18n(
                "cabinet.keywords.stop_words",
                count=len(stop_words),
                words=escape(", ".join(item.word for item in stop_words))
                or i18n("cabinet.keywords.none"),
            ),
        )
    )


# --------------------------------------------------------------------------- #
# Статус подписки
# --------------------------------------------------------------------------- #


@router.callback_query(CabinetCB.filter(F.section == SECTION_SUBSCRIPTION))
async def show_subscription_status(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    trial: TrialService,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Показывает остаток дней подписки или пробного периода."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    await state.clear()
    subscription = await uow.subscriptions.get_live(user.id)
    eligibility = await trial.check_eligibility(user)

    if subscription is None:
        await safe_edit_text(
            target,
            i18n("cabinet.subscription.none"),
            kb_cabinet_subscription(
                False,
                i18n,
                trial_available=eligibility.available,
                trial_days=trial.days,
            ),
        )
        return

    now = datetime.now(tz=timezone.utc)
    expires = subscription.expires_at.astimezone(settings.display_timezone)
    key = (
        "cabinet.subscription.trial"
        if subscription.status is SubscriptionStatus.TRIALING
        else "cabinet.subscription.active"
    )

    await safe_edit_text(
        target,
        i18n(
            key,
            plan=escape(subscription.plan.value),
            left=i18n.plural("units.days", subscription.days_left(now)),
            expires=escape(expires.strftime("%d.%m.%Y %H:%M")),
        ),
        kb_cabinet_subscription(
            True,
            i18n,
            trial_available=eligibility.available,
            trial_days=trial.days,
        ),
    )
