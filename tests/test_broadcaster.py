"""Массовая рассылка: выборка аудитории, лимиты и недоступные адресаты.

Рассылка — единственное место, где бот обращается к Bot API тысячи раз
подряд, поэтому проверяется не «сообщение ушло», а поведение на границах:
что делает отправщик при 403, при flood control, при остановке посреди
работы и не превышает ли он лимит исходящих.

Почти везде взят один отправщик (``workers=1``). Очередь подготовленных
ответов в :class:`MockedSession` общая и отдаётся по порядку, а при
нескольких параллельных отправщиках порядок обращений недетерминирован —
тест начал бы мигать.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.methods import CopyMessage
from aiogram.types import InlineKeyboardMarkup

from db.enums import BroadcastAudience, SubscriptionStatus
from db.models import User
from db.uow import UnitOfWorkFactory
from services.broadcaster import (
    _PROGRESS_EVERY,
    BroadcastContent,
    Broadcaster,
    audience_from_value,
    parse_buttons,
)
from services.notifier import TelegramNotifier
from services.ratelimit.base import RateLimitRule
from tests.conftest import FROZEN_NOW, MockedBot

pytestmark = pytest.mark.db

SOURCE_CHAT = 555
SOURCE_MESSAGE = 777


@pytest.fixture
def content() -> BroadcastContent:
    """Образец рассылаемого сообщения."""
    return BroadcastContent(source_chat_id=SOURCE_CHAT, source_message_id=SOURCE_MESSAGE)


@pytest.fixture
def notifier(bot: MockedBot, limiter) -> TelegramNotifier:
    """Нотификатор с широким ведром: лимит здесь не предмет проверки."""
    return TelegramNotifier(
        bot, limiter, rule=RateLimitRule(limit=1000, window=1.0, burst=1000, scope="outbound")
    )


def build_broadcaster(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    *,
    workers: int = 1,
    page_size: int = 500,
) -> Broadcaster:
    """Собирает рассылку с предсказуемым числом отправщиков."""
    return Broadcaster(bot, uow_factory, notifier, workers=workers, page_size=page_size)


# ------------------------------------------------------------- аудитория
async def test_audience_all_excludes_banned_and_blocked(
    uow_factory: UnitOfWorkFactory,
    make_user,
) -> None:
    await make_user()
    await make_user(is_banned=True)
    await make_user(is_bot_blocked=True)

    async with uow_factory() as uow:
        count = await uow.users.count_audience(BroadcastAudience.ALL, FROZEN_NOW)

    assert count == 1, (
        "Забаненным писать не нужно, заблокировавшим бесполезно — "
        "оба должны выпадать из аудитории"
    )


async def test_audience_active_selects_only_paid_subscribers(
    uow_factory: UnitOfWorkFactory,
    make_user,
    make_subscription,
) -> None:
    paid = await make_user()
    await make_subscription(paid, started_at=FROZEN_NOW, status=SubscriptionStatus.ACTIVE)

    trialing = await make_user()
    await make_subscription(trialing, started_at=FROZEN_NOW, status=SubscriptionStatus.TRIALING)

    await make_user()  # вовсе без подписки

    async with uow_factory() as uow:
        recipients = await uow.users.fetch_audience_page(
            BroadcastAudience.ACTIVE, now=FROZEN_NOW
        )

    assert [item.id for item in recipients] == [paid.id], (
        "Пробный период — не оплаченная подписка и в эту группу не входит"
    )


async def test_audience_active_excludes_subscription_expired_by_date(
    uow_factory: UnitOfWorkFactory,
    make_user,
    make_subscription,
) -> None:
    # Воркер помечает подписки истёкшими не мгновенно: между окончанием
    # срока и его проходом статус ещё active, а доступа уже нет.
    stale = await make_user()
    await make_subscription(
        stale,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(days=1),
        status=SubscriptionStatus.ACTIVE,
    )

    async with uow_factory() as uow:
        count = await uow.users.count_audience(BroadcastAudience.ACTIVE, FROZEN_NOW)

    assert count == 0, "Просроченная по дате подписка не должна считаться активной"


async def test_audience_expired_trial_selects_churned_users(
    uow_factory: UnitOfWorkFactory,
    make_user,
    make_subscription,
) -> None:
    churned = await make_user(trial_activated_at=FROZEN_NOW - timedelta(days=30))
    await make_subscription(
        churned,
        started_at=FROZEN_NOW - timedelta(days=30),
        expires_at=FROZEN_NOW - timedelta(days=23),
        status=SubscriptionStatus.EXPIRED,
    )

    still_trialing = await make_user(trial_activated_at=FROZEN_NOW - timedelta(days=1))
    await make_subscription(
        still_trialing, started_at=FROZEN_NOW, status=SubscriptionStatus.TRIALING
    )

    never_tried = await make_user()

    async with uow_factory() as uow:
        recipients = await uow.users.fetch_audience_page(
            BroadcastAudience.EXPIRED_TRIAL, now=FROZEN_NOW
        )

    ids = [item.id for item in recipients]
    assert ids == [churned.id], (
        f"Ожидался только ушедший после триала, получено {ids} "
        f"(на триале: {still_trialing.id}, не пробовал: {never_tried.id})"
    )


async def test_audience_page_walks_all_users_by_cursor(
    uow_factory: UnitOfWorkFactory,
    make_user,
) -> None:
    # Постраничность по курсору, а не по OFFSET: страницы не должны
    # пропускать или повторять пользователей.
    created = [await make_user() for _ in range(5)]

    seen: list[int] = []
    after_id = 0
    async with uow_factory() as uow:
        while True:
            page = await uow.users.fetch_audience_page(
                BroadcastAudience.ALL, now=FROZEN_NOW, after_id=after_id, limit=2
            )
            if not page:
                break
            seen.extend(item.id for item in page)
            after_id = page[-1].id

    assert seen == sorted(user.id for user in created), (
        "Обход страницами должен вернуть каждого пользователя ровно один раз"
    )


async def test_fetch_audience_page_rejects_non_positive_limit(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        with pytest.raises(ValueError):
            await uow.users.fetch_audience_page(
                BroadcastAudience.ALL, now=FROZEN_NOW, limit=0
            )


# -------------------------------------------------------------- рассылка
async def test_broadcast_delivers_copy_to_every_recipient(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    recipients = [await make_user() for _ in range(3)]
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    assert report.sent == 3, f"Ожидалось три доставки, получено {report.sent}"
    assert report.blocked == 0 and report.failed == 0

    sent = bot.session.requests_of(CopyMessage)
    assert {request.chat_id for request in sent} == {
        user.telegram_id for user in recipients
    }, "Копия должна уйти каждому получателю"
    assert all(request.from_chat_id == SOURCE_CHAT for request in sent), (
        "Источником копии должен быть чат администратора"
    )


async def test_broadcast_marks_blocked_user_in_database(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    blocked_user = await make_user()
    bot.add_result_for(
        CopyMessage,
        ok=False,
        error_code=403,
        description="Forbidden: bot was blocked by the user",
    )
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    assert report.blocked == 1, "Ответ 403 должен учитываться как блокировка"
    assert report.sent == 0

    async with uow_factory() as uow:
        stored = await uow.users.get_by_id(blocked_user.id)
    assert stored is not None and stored.is_bot_blocked, (
        "Заблокировавший бота должен быть помечен, иначе следующая рассылка "
        "снова потратит на него жетон лимита"
    )


async def test_broadcast_excludes_previously_blocked_user(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    await make_user(is_bot_blocked=True)
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    assert report.total == 0, "Помеченный ранее адресат не должен попадать в аудиторию"
    assert not bot.session.requests_of(CopyMessage), "Обращений к Bot API быть не должно"


async def test_broadcast_treats_missing_chat_as_unreachable(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    # Чат не найден: аккаунт удалён либо диалог с ботом не начинался.
    # Повторять так же бесполезно, как и при 403.
    await make_user()
    bot.add_result_for(
        CopyMessage, ok=False, error_code=400, description="Bad Request: chat not found"
    )
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    assert report.blocked == 1, "Отсутствующий чат должен считаться недоступным навсегда"
    assert len(bot.session.requests_of(CopyMessage)) == 1, "Повторять такую отправку не нужно"


async def test_broadcast_retries_after_flood_control(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    await make_user()
    bot.add_result_for(
        CopyMessage,
        ok=False,
        error_code=429,
        description="Too Many Requests: retry after 1",
        retry_after=1,
    )
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    assert report.sent == 1, "После паузы отправка должна повториться и пройти"
    assert len(bot.session.requests_of(CopyMessage)) == 2, (
        "Ожидались две попытки: отбитая flood control и успешная"
    )


async def test_broadcast_does_not_retry_rejected_request(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    # Сообщение у всех получателей одно и то же: если Telegram отклонил
    # его разбором, повтор ничего не изменит.
    await make_user()
    bot.add_result_for(
        CopyMessage,
        ok=False,
        error_code=400,
        description="Bad Request: message text is empty",
    )
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    assert report.failed == 1
    assert len(bot.session.requests_of(CopyMessage)) == 1, "Повторять отклонённый запрос незачем"


async def test_broadcast_reports_progress_and_finishes(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    await make_user()
    broadcaster = build_broadcaster(bot, uow_factory, notifier)
    seen: list[bool] = []

    async def on_progress(report) -> None:
        seen.append(report.is_finished)

    report = await broadcaster.run(
        content, BroadcastAudience.ALL, now=FROZEN_NOW, on_progress=on_progress
    )

    assert seen and seen[-1] is True, "Последний отчёт должен приходить уже завершённым"
    assert report.is_finished and report.finished_at is not None
    assert report.processed == report.total


async def test_broadcast_survives_failing_progress_callback(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    # Сообщение со статусом администратор может удалить — сбой его
    # обновления не повод останавливать рассылку.
    await make_user()
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    async def on_progress(report) -> None:
        raise RuntimeError("сообщение со статусом удалено")

    report = await broadcaster.run(
        content, BroadcastAudience.ALL, now=FROZEN_NOW, on_progress=on_progress
    )

    assert report.sent == 1, "Ошибка обратного вызова не должна срывать доставку"


async def test_broadcast_stops_on_request(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    # Получателей заведомо больше шага отчёта: иначе обратный вызов
    # придёт только по завершении, и останавливать будет уже нечего.
    total = _PROGRESS_EVERY + 10
    for _ in range(total):
        await make_user()

    broadcaster = build_broadcaster(bot, uow_factory, notifier)
    cancel = asyncio.Event()

    async def on_progress(report) -> None:
        if not report.is_finished:
            cancel.set()

    report = await broadcaster.run(
        content,
        BroadcastAudience.ALL,
        now=FROZEN_NOW,
        on_progress=on_progress,
        cancel=cancel,
    )

    assert report.processed < report.total, "Остановка должна прервать рассылку до конца списка"
    assert report.cancelled, "Прерванная рассылка должна помечаться как остановленная"


async def test_broadcast_refuses_second_run_while_busy(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
    make_user,
) -> None:
    for _ in range(_PROGRESS_EVERY + 5):
        await make_user()

    broadcaster = build_broadcaster(bot, uow_factory, notifier)
    blocker = asyncio.Event()

    async def on_progress(report) -> None:
        # Промежуточный отчёт приходит, пока рассылка ещё идёт — самый
        # момент, чтобы попробовать запустить вторую.
        if report.is_finished or blocker.is_set():
            return
        blocker.set()
        with pytest.raises(RuntimeError):
            await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    await broadcaster.run(
        content, BroadcastAudience.ALL, now=FROZEN_NOW, on_progress=on_progress
    )

    assert blocker.is_set(), "Проверка второй рассылки должна была выполниться"


async def test_broadcast_with_empty_audience_makes_no_calls(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    content: BroadcastContent,
) -> None:
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)

    assert report.total == 0 and report.is_finished
    assert not bot.session.requests_of(CopyMessage)


async def test_broadcast_respects_outbound_rate_limit(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    limiter,
    content: BroadcastContent,
    make_user,
) -> None:
    # Лимит общий на бота: превышение отзывается flood control на всего
    # бота, а не на одну рассылку. Ведро на 3 сообщения в секунду означает,
    # что шесть отправок не могут уложиться в мгновение.
    for _ in range(6):
        await make_user()

    slow = TelegramNotifier(
        bot, limiter, rule=RateLimitRule(limit=3, window=1.0, burst=3, scope="outbound")
    )
    broadcaster = build_broadcaster(bot, uow_factory, slow, workers=4)

    started = datetime.now(tz=timezone.utc)
    report = await broadcaster.run(content, BroadcastAudience.ALL, now=FROZEN_NOW)
    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()

    assert report.sent == 6
    assert elapsed >= 0.5, (
        f"Шесть отправок при лимите 3/с не могут занять {elapsed:.2f} с — "
        "ведро жетонов не соблюдается"
    )


async def test_broadcast_passes_reply_markup_to_copy(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    make_user,
) -> None:
    await make_user()
    markup = parse_buttons("Открыть | https://example.com")
    broadcaster = build_broadcaster(bot, uow_factory, notifier)

    await broadcaster.run(
        BroadcastContent(
            source_chat_id=SOURCE_CHAT,
            source_message_id=SOURCE_MESSAGE,
            reply_markup=markup,
        ),
        BroadcastAudience.ALL,
        now=FROZEN_NOW,
    )

    request = bot.session.requests_of(CopyMessage)[0]
    assert isinstance(request.reply_markup, InlineKeyboardMarkup)
    assert request.reply_markup.inline_keyboard[0][0].url == "https://example.com", (
        "Кнопки должны доходить до получателя: у копии своя разметка"
    )


@pytest.mark.parametrize("workers", [0, -1])
def test_broadcaster_rejects_invalid_worker_count(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    workers: int,
) -> None:
    with pytest.raises(ValueError):
        Broadcaster(bot, uow_factory, notifier, workers=workers)


def test_broadcaster_rejects_invalid_page_size(
    bot: MockedBot,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
) -> None:
    with pytest.raises(ValueError):
        Broadcaster(bot, uow_factory, notifier, page_size=0)


# ---------------------------------------------------------------- кнопки
def test_parse_buttons_builds_url_keyboard() -> None:
    markup = parse_buttons("Сайт | https://example.com\nКанал | https://t.me/channel")

    assert markup is not None
    assert len(markup.inline_keyboard) == 2, "Каждая строка ввода — отдельная кнопка"
    assert markup.inline_keyboard[0][0].text == "Сайт"


def test_parse_buttons_returns_none_for_blank_input() -> None:
    assert parse_buttons("   \n\n  ") is None, "Пустой ввод означает отсутствие кнопок"


@pytest.mark.parametrize(
    "raw",
    [
        "Кнопка без ссылки",
        " | https://example.com",
        "Кнопка | ftp://example.com",
        "Кнопка | example.com",
    ],
)
def test_parse_buttons_rejects_malformed_lines(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_buttons(raw)


def test_audience_from_value_accepts_known_values() -> None:
    assert audience_from_value("expired_trial") is BroadcastAudience.EXPIRED_TRIAL


def test_audience_from_value_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        audience_from_value("everyone")


def test_recipient_carries_both_identifiers(user: User) -> None:
    # Пометить заблокировавшего можно только по первичному ключу, а
    # отправить — только по telegram_id: нужны оба.
    from db.repositories.user import Recipient

    recipient = Recipient(id=user.id, telegram_id=user.telegram_id)
    assert recipient.id != recipient.telegram_id
