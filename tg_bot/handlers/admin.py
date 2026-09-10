"""Панель администратора: метрики, промокоды и массовая рассылка.

Интерфейс намеренно оставлен русскоязычным, в отличие от остальной части
бота. Панель видит оператор сервиса, а не клиент; переводить её на три
языка значит удвоить объём каталогов ради экранов, которые открывает один
человек. Всё, что уходит конечному пользователю — сообщения о реферальном
бонусе, результат промокода, — локализовано как обычно.

Права проверяет фильтр :class:`~tg_bot.filters.IsAdmin`, а не первая
строка каждого хендлера. Не прошедший фильтр апдейт не считается
обработанным, поэтому для постороннего ``/admin`` неотличим от любого
другого незнакомого текста — бот на него попросту не отвечает.

Рассылка запускается фоновой задачей и переживает возврат из хендлера:
держать апдейт открытым на всё время отправки нельзя — вместе с ним
держалась бы транзакция БД, а рассылка идёт минутами.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from html import escape
from typing import Final

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.logger import get_logger
from db.enums import BroadcastAudience, PromocodeKind
from db.models import Promocode, User
from db.uow import UnitOfWork
from services.broadcaster import (
    BroadcastContent,
    BroadcastReport,
    Broadcaster,
    parse_buttons,
)
from services.metrics import DashboardMetrics, MetricsService, format_revenue
from tg_bot.callbacks import (
    ADMIN_BROADCAST,
    ADMIN_DASHBOARD,
    ADMIN_PROMOCODES,
    ADMIN_REFERRALS,
    BROADCAST_CANCEL,
    BROADCAST_PICK,
    BROADCAST_START,
    BROADCAST_STOP,
    AdminCB,
    BroadcastCB,
)
from tg_bot.filters import IsAdmin
from tg_bot.utils import get_message, safe_edit_text, shorten

logger = get_logger(__name__)

router = Router(name="admin")

# Фильтр вешается на роутер целиком: так о правах нельзя забыть, добавляя
# сюда новый хендлер. Отдельная проверка в каждом обработчике рано или
# поздно оказалась бы пропущенной ровно в одном месте.
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

#: Подписи аудиторий рассылки.
_AUDIENCE_TITLES: Final[dict[BroadcastAudience, str]] = {
    BroadcastAudience.ALL: "Все пользователи",
    BroadcastAudience.ACTIVE: "С активной подпиской",
    BroadcastAudience.EXPIRED_TRIAL: "С истёкшим триалом",
}

#: Сколько промокодов показывать в списке.
_PROMO_LIST_LIMIT: Final[int] = 15

#: Сколько пригласивших показывать в топе.
_TOP_REFERRERS_LIMIT: Final[int] = 10

#: Ссылки на фоновые задачи рассылки.
#:
#: Без сильной ссылки сборщик мусора вправе уничтожить задачу прямо
#: посреди работы: ``asyncio`` хранит на неё только слабую ссылку.
_background: Final[set[asyncio.Task[None]]] = set()


class BroadcastSG(StatesGroup):
    """Сценарий подготовки рассылки."""

    #: Ждём сообщение-образец.
    content = State()
    #: Ждём кнопки или отказ от них.
    buttons = State()
    #: Ждём выбор аудитории и подтверждение.
    audience = State()


# ------------------------------------------------------------------ метрики
@router.message(Command("admin"))
async def cmd_admin(message: Message, uow: UnitOfWork) -> None:
    """Показывает панель с ключевыми показателями."""
    metrics = await MetricsService(uow).collect(datetime.now(tz=timezone.utc))
    await message.answer(_render_dashboard(metrics), reply_markup=_kb_admin())


@router.callback_query(AdminCB.filter(F.action == ADMIN_DASHBOARD))
async def refresh_dashboard(callback: CallbackQuery, uow: UnitOfWork) -> None:
    """Пересчитывает показатели по кнопке «Обновить»."""
    await callback.answer("Пересчитываю…")
    message = get_message(callback)
    if message is None:
        return

    metrics = await MetricsService(uow).collect(datetime.now(tz=timezone.utc))
    await safe_edit_text(message, _render_dashboard(metrics), _kb_admin())


def _render_dashboard(metrics: DashboardMetrics) -> str:
    """Собирает текст панели показателей.

    :param metrics: Снимок показателей.
    :return: HTML для отправки.
    """
    users = metrics.users
    lines = [
        "📊 <b>Панель администратора</b>",
        "",
        "<b>Пользователи</b>",
        f"Всего: <b>{users.total}</b>",
        f"За сутки: <b>+{users.new_today}</b> · за неделю: <b>+{users.new_week}</b>",
        f"Пробовали триал: <b>{users.trial_used}</b>",
        f"Заблокировали бота: <b>{users.blocked}</b> ({metrics.blocked_share:.1f} %)",
        "",
        "<b>Подписки</b>",
        f"Активных: <b>{metrics.active_subscriptions}</b>",
        f"На триале: <b>{metrics.trial_users}</b>",
        "",
        "<b>Деньги</b>",
        f"За 30 дней: <b>{format_revenue(metrics.revenue_month)}</b>",
        f"За всё время: <b>{format_revenue(metrics.revenue_total)}</b>",
        f"Заплатили хотя бы раз: <b>{metrics.paying_users}</b>",
        f"Конверсия: <b>{metrics.conversion:.1f} %</b> от всех, "
        f"<b>{metrics.trial_conversion:.1f} %</b> от попробовавших",
        "",
        "<b>Привлечение</b>",
        f"Приглашений: <b>{metrics.referrals.total}</b>, "
        f"начислено <b>{metrics.referrals.bonus_days}</b> сут.",
        f"Промокодов: <b>{metrics.promocodes.active_codes}</b> действующих "
        f"из <b>{metrics.promocodes.codes}</b>, "
        f"активаций <b>{metrics.promocodes.redemptions}</b>",
        "",
        "<i>«За 30 дней» — сумма успешных платежей, а не регулярная "
        "выручка: подписка продаётся разовыми периодами.</i>",
    ]
    return "\n".join(lines)


def _kb_admin() -> InlineKeyboardMarkup:
    """Главное меню панели."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data=AdminCB(action=ADMIN_DASHBOARD))
    builder.button(text="📣 Рассылка", callback_data=AdminCB(action=ADMIN_BROADCAST))
    builder.button(text="🎟 Промокоды", callback_data=AdminCB(action=ADMIN_PROMOCODES))
    builder.button(text="👥 Рефералы", callback_data=AdminCB(action=ADMIN_REFERRALS))
    builder.adjust(1, 1, 2)
    return builder.as_markup()


# --------------------------------------------------------------- промокоды
@router.callback_query(AdminCB.filter(F.action == ADMIN_PROMOCODES))
async def show_promocodes(callback: CallbackQuery, uow: UnitOfWork) -> None:
    """Показывает последние созданные промокоды."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return

    codes = await uow.promocodes.list_recent(_PROMO_LIST_LIMIT)
    await safe_edit_text(message, _render_promocodes(codes), _kb_back())


@router.message(Command("promos"))
async def cmd_promos(message: Message, uow: UnitOfWork) -> None:
    """Список промокодов командой."""
    codes = await uow.promocodes.list_recent(_PROMO_LIST_LIMIT)
    await message.answer(_render_promocodes(codes), reply_markup=_kb_back())


def _render_promocodes(codes: Sequence[Promocode]) -> str:
    """Собирает список промокодов.

    :param codes: Промокоды, сначала свежие.
    :return: HTML для отправки.
    """
    if not codes:
        return (
            "🎟 <b>Промокоды</b>\n\nКодов пока нет.\n\n"
            "Создать: <code>/newpromo 30 100 запуск</code>\n"
            "(дней, лимит активаций, пометка)"
        )

    lines = ["🎟 <b>Промокоды</b>", ""]
    for code in codes:
        limit = code.max_activations or "∞"
        state = "✅" if code.is_active and not code.is_exhausted else "⛔"
        note = f" — {escape(shorten(code.comment, 30))}" if code.comment else ""
        lines.append(
            f"{state} <code>{escape(code.code)}</code> · "
            f"{code.value} {'сут.' if code.kind is PromocodeKind.BONUS_DAYS else '%'} · "
            f"{code.activations}/{limit}{note}"
        )

    lines.extend(["", "Создать: <code>/newpromo 30 100 запуск</code>"])
    return "\n".join(lines)


@router.message(Command("newpromo"))
async def cmd_new_promo(
    message: Message,
    command: CommandObject,
    user: User,
    uow: UnitOfWork,
) -> None:
    """Создаёт промокод на бонусные дни.

    Формат: ``/newpromo <дней> [лимит активаций] [пометка]``. Код
    генерируется автоматически — придуманные вручную коды рано или поздно
    сталкиваются друг с другом, а уникальность здесь под ограничением БД.
    """
    args = (command.args or "").split(maxsplit=2)
    if not args:
        await message.answer(
            "Формат: <code>/newpromo &lt;дней&gt; [лимит] [пометка]</code>\n"
            "Например: <code>/newpromo 30 100 запуск</code>"
        )
        return

    try:
        days = int(args[0])
        if days <= 0:
            raise ValueError("дни должны быть положительными")
        max_activations = int(args[1]) if len(args) > 1 else None
        if max_activations is not None and max_activations <= 0:
            raise ValueError("лимит должен быть положительным")
    except ValueError as exc:
        await message.answer(f"❌ Не понял параметры: {escape(str(exc))}")
        return

    comment = args[2].strip() if len(args) > 2 else None
    code = Promocode.generate_code()

    promocode = await uow.promocodes.create(
        code=code,
        kind=PromocodeKind.BONUS_DAYS,
        value=days,
        created_by_id=user.id,
        max_activations=max_activations,
        comment=comment,
    )

    logger.info(
        "Администратор id=%s создал промокод %s на %d сут.", user.id, promocode.code, days
    )
    await message.answer(
        f"🎟 Промокод создан\n\n"
        f"Код: <code>{escape(promocode.code)}</code>\n"
        f"Бонус: <b>{days}</b> сут.\n"
        f"Лимит: <b>{max_activations or '∞'}</b>\n\n"
        f"Активация у пользователя: <code>/promo {escape(promocode.code)}</code>"
    )


# ---------------------------------------------------------------- рефералы
@router.callback_query(AdminCB.filter(F.action == ADMIN_REFERRALS))
async def show_referrals(callback: CallbackQuery, uow: UnitOfWork) -> None:
    """Показывает сводку по реферальной программе."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return

    totals = await uow.referrals.totals()
    top = await uow.referrals.top_referrers(_TOP_REFERRERS_LIMIT)

    lines = [
        "👥 <b>Реферальная программа</b>",
        "",
        f"Приглашений: <b>{totals.total}</b>",
        f"С начисленным бонусом: <b>{totals.rewarded}</b>",
        f"Выдано суток: <b>{totals.bonus_days}</b>",
    ]

    if top:
        lines.extend(["", "<b>Лидеры:</b>"])
        for index, (referrer, invited, days) in enumerate(top, start=1):
            name = escape(shorten(referrer.full_name, 24))
            lines.append(f"{index}. {name} — <b>{invited}</b> пригл., {days} сут.")

    await safe_edit_text(message, "\n".join(lines), _kb_back())


# ---------------------------------------------------------------- рассылка
@router.callback_query(AdminCB.filter(F.action == ADMIN_BROADCAST))
async def start_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    """Начинает сценарий рассылки."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return

    await state.set_state(BroadcastSG.content)
    await message.answer(
        "📣 <b>Новая рассылка</b>\n\n"
        "Пришлите сообщение-образец — текст, фото, видео или документ. "
        "Оно уйдёт получателям точно в таком виде.\n\n"
        "Отмена: /cancel"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    """Начинает сценарий рассылки командой."""
    await state.set_state(BroadcastSG.content)
    await message.answer(
        "📣 <b>Новая рассылка</b>\n\n"
        "Пришлите сообщение-образец — текст, фото, видео или документ.\n\n"
        "Отмена: /cancel"
    )


@router.message(Command("cancel"), StateFilter(BroadcastSG))
async def cancel_broadcast(message: Message, state: FSMContext) -> None:
    """Прерывает подготовку рассылки."""
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=_kb_back())


@router.message(BroadcastSG.content)
async def receive_content(message: Message, state: FSMContext) -> None:
    """Запоминает сообщение-образец.

    Сохраняются координаты сообщения, а не его содержимое: копирование
    через ``copy_message`` переносит медиа и оформление как есть, а
    пересборка на нашей стороне потребовала бы отдельной ветки под каждый
    тип вложения.
    """
    await state.update_data(
        source_chat_id=message.chat.id, source_message_id=message.message_id
    )
    await state.set_state(BroadcastSG.buttons)
    await message.answer(
        "Кнопки под сообщением?\n\n"
        "Пришлите строки вида <code>Открыть | https://example.com</code>, "
        "по одной на кнопку.\n"
        "Без кнопок — /skip"
    )


@router.message(Command("skip"), BroadcastSG.buttons)
async def skip_buttons(message: Message, state: FSMContext, broadcaster: Broadcaster) -> None:
    """Пропускает добавление кнопок."""
    await _ask_audience(message, state, broadcaster, markup=None)


@router.message(BroadcastSG.buttons)
async def receive_buttons(
    message: Message, state: FSMContext, broadcaster: Broadcaster
) -> None:
    """Разбирает кнопки и переходит к выбору аудитории."""
    try:
        markup = parse_buttons(message.text or message.caption or "")
    except ValueError as exc:
        await message.answer(f"❌ {escape(str(exc))}\n\nПопробуйте ещё раз или /skip")
        return

    await _ask_audience(message, state, broadcaster, markup=markup)


async def _ask_audience(
    message: Message,
    state: FSMContext,
    broadcaster: Broadcaster,
    *,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Показывает выбор аудитории с размером каждой группы."""
    await state.update_data(
        buttons=markup.model_dump_json() if markup is not None else None
    )
    await state.set_state(BroadcastSG.audience)

    now = datetime.now(tz=timezone.utc)
    builder = InlineKeyboardBuilder()
    for audience, title in _AUDIENCE_TITLES.items():
        # Размер группы показывается сразу: выбирать аудиторию вслепую,
        # а потом узнавать, что в ней три человека, — плохая сделка.
        count = await broadcaster.count_audience(audience, now)
        builder.button(
            text=f"{title} ({count})",
            callback_data=BroadcastCB(action=BROADCAST_PICK, audience=audience.value),
        )
    builder.button(text="✖️ Отмена", callback_data=BroadcastCB(action=BROADCAST_CANCEL))
    builder.adjust(1)

    await message.answer("Кому отправляем?", reply_markup=builder.as_markup())


@router.callback_query(BroadcastCB.filter(F.action == BROADCAST_PICK), BroadcastSG.audience)
async def pick_audience(
    callback: CallbackQuery,
    callback_data: BroadcastCB,
    state: FSMContext,
    bot: Bot,
    broadcaster: Broadcaster,
) -> None:
    """Показывает подтверждение с предпросмотром."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return

    try:
        audience = BroadcastAudience(callback_data.audience)
    except ValueError:
        await message.answer("❌ Неизвестная аудитория, начните заново: /broadcast")
        await state.clear()
        return

    data = await state.get_data()
    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")
    if source_chat_id is None or source_message_id is None:
        await message.answer("❌ Образец потерян, начните заново: /broadcast")
        await state.clear()
        return

    await state.update_data(audience=audience.value)

    count = await broadcaster.count_audience(audience, datetime.now(tz=timezone.utc))
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🚀 Отправить ({count})", callback_data=BroadcastCB(action=BROADCAST_START))
    builder.button(text="✖️ Отмена", callback_data=BroadcastCB(action=BROADCAST_CANCEL))
    builder.adjust(1)

    await message.answer("Предпросмотр:")
    # Предпросмотр — та же операция копирования, что и в рассылке:
    # администратор видит ровно то, что получат пользователи. Сбой
    # здесь означает, что и рассылка не пройдёт, поэтому он виден.
    try:
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=int(source_chat_id),
            message_id=int(source_message_id),
            reply_markup=_restore_markup(data.get("buttons")),
        )
    except TelegramAPIError as exc:
        logger.warning("Предпросмотр рассылки не удался: %s", exc)
        await message.answer(
            f"⚠️ Не удалось показать предпросмотр: {escape(str(exc))}\n"
            "Скорее всего, не выйдет и разослать — пришлите образец заново."
        )

    await message.answer(
        f"Аудитория: <b>{_AUDIENCE_TITLES[audience]}</b>\n"
        f"Получателей: <b>{count}</b>\n\n"
        "Отправляем?",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(BroadcastCB.filter(F.action == BROADCAST_CANCEL))
async def abort_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменяет подготовленную рассылку."""
    await callback.answer("Отменено")
    await state.clear()
    message = get_message(callback)
    if message is not None:
        await safe_edit_text(message, "Рассылка отменена.", _kb_back())


@router.callback_query(BroadcastCB.filter(F.action == BROADCAST_START), BroadcastSG.audience)
async def launch_broadcast(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    broadcaster: Broadcaster,
    user: User,
) -> None:
    """Запускает рассылку фоновой задачей."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return

    if broadcaster.is_running:
        await message.answer("⏳ Одна рассылка уже идёт. Дождитесь её окончания.")
        return

    data = await state.get_data()
    await state.clear()

    source_chat_id = data.get("source_chat_id")
    source_message_id = data.get("source_message_id")
    audience_value = data.get("audience")
    if source_chat_id is None or source_message_id is None or audience_value is None:
        await message.answer("❌ Данные рассылки потеряны, начните заново: /broadcast")
        return

    content = BroadcastContent(
        source_chat_id=int(source_chat_id),
        source_message_id=int(source_message_id),
        reply_markup=_restore_markup(data.get("buttons")),
    )
    audience = BroadcastAudience(audience_value)

    status = await message.answer("🚀 Рассылка запущена…", reply_markup=_kb_stop())

    logger.info(
        "Администратор id=%s запустил рассылку по аудитории %s", user.id, audience
    )

    # Задача живёт дольше апдейта: держать хендлер открытым на всё время
    # рассылки значило бы держать открытой и транзакцию БД.
    # Сообщение привязывается к боту явно: фоновая задача выполняется
    # вне контекста апдейта, где aiogram подставляет его сам, и
    # status.edit_text() там упал бы без привязки.
    task = asyncio.create_task(
        _run_broadcast(broadcaster, content, audience, status.as_(bot)),
        name="broadcast",
    )
    _background.add(task)
    task.add_done_callback(_background.discard)


@router.callback_query(BroadcastCB.filter(F.action == BROADCAST_STOP))
async def stop_broadcast(callback: CallbackQuery, broadcaster: Broadcaster) -> None:
    """Останавливает идущую рассылку."""
    if broadcaster.request_stop():
        await callback.answer("Останавливаю…", show_alert=True)
    else:
        await callback.answer("Рассылка уже завершена.", show_alert=True)


async def _run_broadcast(
    broadcaster: Broadcaster,
    content: BroadcastContent,
    audience: BroadcastAudience,
    status: Message,
) -> None:
    """Выполняет рассылку и обновляет сообщение со статусом."""

    async def on_progress(report: BroadcastReport) -> None:
        markup = None if report.is_finished else _kb_stop()
        await safe_edit_text(status, _render_report(report), markup)

    try:
        await broadcaster.run(
            content,
            audience,
            now=datetime.now(tz=timezone.utc),
            on_progress=on_progress,
        )
    except Exception:
        logger.exception("Рассылка завершилась ошибкой")
        try:
            await status.answer("❌ Рассылка прервана из-за ошибки. Подробности в журнале.")
        except TelegramAPIError:
            logger.exception("Не удалось сообщить администратору о сбое рассылки")


def _render_report(report: BroadcastReport) -> str:
    """Собирает текст отчёта о рассылке."""
    title = "📣 <b>Рассылка</b>" if not report.is_finished else "📣 <b>Рассылка завершена</b>"
    percent = report.processed / report.total * 100 if report.total else 100.0

    lines = [
        title,
        "",
        f"Аудитория: {_AUDIENCE_TITLES.get(report.audience, report.audience)}",
        f"Обработано: <b>{report.processed}</b> из <b>{report.total}</b> ({percent:.0f} %)",
        f"Доставлено: <b>{report.sent}</b>",
        f"Заблокировали бота: <b>{report.blocked}</b>",
        f"Ошибок: <b>{report.failed}</b>",
        f"Скорость: <b>{report.rate:.1f}</b> сообщ./с",
    ]
    if report.cancelled:
        lines.append("\n⏹ Остановлена вручную.")
    return "\n".join(lines)


def _restore_markup(raw: str | None) -> InlineKeyboardMarkup | None:
    """Восстанавливает клавиатуру из состояния FSM.

    В состоянии клавиатура лежит строкой JSON: хранилищем может быть Redis,
    а туда попадают только сериализуемые значения, объекты aiogram — нет.

    :param raw: Сохранённая разметка либо ``None``.
    :return: Клавиатура либо ``None``.
    """
    if not raw:
        return None
    try:
        return InlineKeyboardMarkup.model_validate_json(raw)
    except ValueError:
        logger.warning("Не удалось восстановить клавиатуру рассылки, отправляю без кнопок")
        return None


def _kb_back() -> InlineKeyboardMarkup:
    """Кнопка возврата в панель."""
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ В панель", callback_data=AdminCB(action=ADMIN_DASHBOARD))
    return builder.as_markup()


def _kb_stop() -> InlineKeyboardMarkup:
    """Кнопка остановки рассылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏹ Остановить", callback_data=BroadcastCB(action=BROADCAST_STOP))
    return builder.as_markup()


__all__ = ["router"]

