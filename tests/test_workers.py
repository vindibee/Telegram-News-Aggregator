"""Фоновые задачи воркера: истечение подписок, уведомления, уборка счетов.

Все три задачи работают со временем, поэтому проверяются под заморозкой:
иначе тест «подписка истекла вчера» зависел бы от того, в какой момент
суток его запустили.

Заморозка охватывает и код задачи, и подготовку данных — обе стороны
должны видеть один и тот же «сейчас». Но не базу: PostgreSQL считает
``now()`` по своим часам, и там, где решение принимает сервер, время в
запрос передаётся явно.

Ключевая проверка каждой задачи — не «что-то произошло», а разграничение:
истёкшие обрабатываются, действующие не трогаются, а повторный прогон не
делает работу дважды.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from core.config import Settings
from db.enums import ChannelKind, PaymentStatus, SubscriptionStatus
from db.models import User
from db.uow import UnitOfWorkFactory
from services.notifier import DeliveryResult, DeliveryStatus
from worker.tasks.invoices import StaleInvoiceCleanupTask
from worker.tasks.subscriptions import ExpiryNotificationTask, SubscriptionExpirationTask
from tests.conftest import FROZEN_NOW

pytestmark = pytest.mark.db


def _expiration_task(
    uow_factory: UnitOfWorkFactory,
    notifier: AsyncMock,
    settings: Settings,
) -> SubscriptionExpirationTask:
    """Задача отзыва доступа с моком доставки."""
    return SubscriptionExpirationTask(uow_factory, notifier, settings)


def _notification_task(
    uow_factory: UnitOfWorkFactory,
    notifier: AsyncMock,
    settings: Settings,
) -> ExpiryNotificationTask:
    """Задача предупреждений об окончании."""
    return ExpiryNotificationTask(uow_factory, notifier, settings)


# --------------------------------------------------------------------------- #
# Отзыв доступа по истёкшим подпискам
# --------------------------------------------------------------------------- #


async def test_expiration_revokes_access_for_overdue_subscription(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )

    result = await _expiration_task(uow_factory, mock_notifier, settings).run()

    assert result.processed == 1, f"Ожидалась одна истёкшая подписка, обработано {result.processed}"

    async with uow_factory() as uow:
        subscription = await uow.subscriptions.list_history(user.id)
    assert subscription[0].status is SubscriptionStatus.EXPIRED, (
        "Истёкшая подписка должна переходить в expired"
    )


async def test_expiration_leaves_live_subscription_untouched(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    # Граница по времени: подписка кончается через час и трогать её рано.
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=29),
        expires_at=FROZEN_NOW + timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )

    result = await _expiration_task(uow_factory, mock_notifier, settings).run()

    assert result.processed == 0, "Действующая подписка не должна попадать в выборку"
    async with uow_factory() as uow:
        assert await uow.subscriptions.get_live(user.id) is not None


async def test_expiration_triggers_exactly_at_the_deadline(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    # Ровно та граница, ради проверки которой время и замораживается:
    # до срока — не трогаем, после — отзываем.
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=30),
        expires_at=FROZEN_NOW + timedelta(minutes=30),
        status=SubscriptionStatus.ACTIVE,
    )
    task = _expiration_task(uow_factory, mock_notifier, settings)

    before = await task.run()
    frozen_time.tick(timedelta(hours=1))
    after = await task.run()

    assert before.processed == 0, "До окончания срока доступ отзывать рано"
    assert after.processed == 1, "После окончания срока доступ должен быть отозван"


async def test_expiration_is_idempotent_across_runs(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    # Задача идёт по расписанию каждые несколько минут: одна и та же
    # подписка не должна обрабатываться повторно.
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )
    task = _expiration_task(uow_factory, mock_notifier, settings)

    first = await task.run()
    second = await task.run()

    assert first.processed == 1
    assert second.processed == 0, "Повторный прогон не должен находить ту же подписку"
    assert mock_notifier.send.await_count == 1, "Уведомление уходит ровно один раз"


async def test_expiration_notifies_the_owner(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )

    result = await _expiration_task(uow_factory, mock_notifier, settings).run()

    assert result.succeeded == 1
    mock_notifier.send.assert_awaited_once()
    assert mock_notifier.send.await_args.args[0] == user.telegram_id


async def test_expiration_revokes_access_even_if_notice_fails(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    delivery_result,
    frozen_time,
) -> None:
    # В отличие от предупреждения, смена статуса — самостоятельная
    # ценность: не дошедшее сообщение не повод оставить доступ открытым.
    mock_notifier.send.return_value = delivery_result(DeliveryStatus.FAILED)
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )

    result = await _expiration_task(uow_factory, mock_notifier, settings).run()

    assert result.failed == 1, "Недоставленное сообщение должно быть видно в отчёте"
    async with uow_factory() as uow:
        assert await uow.subscriptions.get_live(user.id) is None, (
            "Доступ обязан закрыться независимо от доставки"
        )


async def test_expiration_marks_user_who_blocked_the_bot(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    delivery_result,
    frozen_time,
) -> None:
    mock_notifier.send.return_value = delivery_result(DeliveryStatus.BLOCKED)
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )

    await _expiration_task(uow_factory, mock_notifier, settings).run()

    async with uow_factory() as uow:
        stored = await uow.users.get_by_id(user.id)
    assert stored is not None and stored.is_bot_blocked, (
        "Заблокировавший бота должен быть помечен, иначе его будут дёргать вечно"
    )


async def test_expiration_suspends_publishing_channels(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    make_channel,
    frozen_time,
) -> None:
    # Автопостинг — платная возможность, и с окончанием подписки он обязан
    # прекращаться. Источники при этом не трогаются: читать новости можно
    # и без подписки, а собирать список каналов заново — обидно.
    target = await make_channel(user, kind=ChannelKind.TARGET)
    source = await make_channel(user, kind=ChannelKind.SOURCE)
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )

    result = await _expiration_task(uow_factory, mock_notifier, settings).run()

    assert result.details.get("suspended_channels") == 1

    async with uow_factory() as uow:
        stored_target = await uow.channels.get_by_id(target.id)
        stored_source = await uow.channels.get_by_id(source.id)

    assert stored_target is not None and not stored_target.is_active, (
        "Целевой канал должен отключаться вместе с подпиской"
    )
    assert stored_source is not None and stored_source.is_active, (
        "Источник новостей отключать не за что"
    )


async def test_expiration_touches_only_the_right_owner(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_user,
    make_subscription,
    frozen_time,
) -> None:
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=40),
        expires_at=FROZEN_NOW - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE,
    )
    neighbour = await make_user()
    await make_subscription(
        neighbour,
        started_at=FROZEN_NOW,
        expires_at=FROZEN_NOW + timedelta(days=20),
        status=SubscriptionStatus.ACTIVE,
    )

    await _expiration_task(uow_factory, mock_notifier, settings).run()

    async with uow_factory() as uow:
        assert await uow.subscriptions.get_live(neighbour.id) is not None, (
            "Чужая действующая подписка не должна пострадать"
        )


async def test_expiration_on_empty_queue_does_nothing(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    frozen_time,
) -> None:
    result = await _expiration_task(uow_factory, mock_notifier, settings).run()

    assert result.processed == 0
    mock_notifier.send.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Предупреждение об окончании
# --------------------------------------------------------------------------- #


async def test_notification_warns_about_soon_expiring_subscription(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    hours = settings.worker.expiry_notice_hours
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=29),
        expires_at=FROZEN_NOW + timedelta(hours=hours - 1),
        status=SubscriptionStatus.ACTIVE,
    )

    result = await _notification_task(uow_factory, mock_notifier, settings).run()

    assert result.succeeded == 1, "Подписка внутри горизонта должна получить предупреждение"
    mock_notifier.send.assert_awaited_once()


async def test_notification_ignores_subscription_beyond_horizon(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    hours = settings.worker.expiry_notice_hours
    await make_subscription(
        user,
        started_at=FROZEN_NOW,
        expires_at=FROZEN_NOW + timedelta(hours=hours + 24),
        status=SubscriptionStatus.ACTIVE,
    )

    result = await _notification_task(uow_factory, mock_notifier, settings).run()

    assert result.processed == 0, "Предупреждать за неделю рано"
    mock_notifier.send.assert_not_awaited()


async def test_notification_is_sent_once_per_period(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    # Отметка ставится вместе с захватом, одним запросом: иначе два
    # воркера успели бы уведомить одного человека дважды.
    hours = settings.worker.expiry_notice_hours
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=29),
        expires_at=FROZEN_NOW + timedelta(hours=hours - 1),
        status=SubscriptionStatus.ACTIVE,
    )
    task = _notification_task(uow_factory, mock_notifier, settings)

    await task.run()
    second = await task.run()

    assert second.processed == 0, "Повторный прогон не должен уведомлять снова"
    assert mock_notifier.send.await_count == 1


async def test_notification_is_retried_after_delivery_failure(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    delivery_result,
    frozen_time,
) -> None:
    # Отметка ставится до доставки, поэтому при сбое её обязательно снять:
    # иначе следующий прогон такую подписку уже не увидит и предупреждение
    # потеряется навсегда.
    hours = settings.worker.expiry_notice_hours
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=29),
        expires_at=FROZEN_NOW + timedelta(hours=hours - 1),
        status=SubscriptionStatus.ACTIVE,
    )
    task = _notification_task(uow_factory, mock_notifier, settings)

    mock_notifier.send.return_value = delivery_result(DeliveryStatus.FAILED)
    failed = await task.run()

    mock_notifier.send.return_value = delivery_result(DeliveryStatus.DELIVERED)
    retried = await task.run()

    assert failed.failed == 1
    assert retried.succeeded == 1, (
        "После неудачной доставки предупреждение должно уйти на следующем прогоне"
    )


async def test_notification_keeps_mark_for_blocked_user(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    delivery_result,
    frozen_time,
) -> None:
    # Бот заблокирован: повторять бессмысленно, и отметку снимать не нужно.
    hours = settings.worker.expiry_notice_hours
    mock_notifier.send.return_value = delivery_result(DeliveryStatus.BLOCKED)
    await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=29),
        expires_at=FROZEN_NOW + timedelta(hours=hours - 1),
        status=SubscriptionStatus.ACTIVE,
    )
    task = _notification_task(uow_factory, mock_notifier, settings)

    await task.run()
    second = await task.run()

    assert second.processed == 0, "Заблокировавшего бота повторно не дёргаем"
    assert mock_notifier.send.await_count == 1


async def test_notification_after_renewal_is_sent_again(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
    user: User,
    make_subscription,
    frozen_time,
) -> None:
    # Продление открывает новый цикл: предупреждение должно прийти и перед
    # окончанием нового периода, а не «уже отправляли когда-то».
    hours = settings.worker.expiry_notice_hours
    subscription = await make_subscription(
        user,
        started_at=FROZEN_NOW - timedelta(days=29),
        expires_at=FROZEN_NOW + timedelta(hours=hours - 1),
        status=SubscriptionStatus.ACTIVE,
    )
    task = _notification_task(uow_factory, mock_notifier, settings)
    await task.run()

    async with uow_factory() as uow:
        renewed = await uow.subscriptions.get_by_id(subscription.id)
        renewed.expires_at = FROZEN_NOW + timedelta(days=30)
        renewed.expiry_notified_at = None
        await uow.commit()

    # Сдвигаемся почти к новому сроку, но не за него: цель — момент,
    # когда подписка снова попадает в горизонт предупреждения, а не когда
    # она уже истекла.
    frozen_time.tick(timedelta(days=29, hours=23))
    result = await task.run()

    assert result.succeeded == 1, "Перед окончанием нового периода нужно предупредить снова"


# --------------------------------------------------------------------------- #
# Уборка неоплаченных счетов
# --------------------------------------------------------------------------- #


async def test_cleanup_expires_stale_invoice(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    user: User,
    make_payment,
    frozen_time,
) -> None:
    # Пока счёт висит в pending, он занимает место в лимите незавершённых,
    # и человек не может выставить новый.
    payment = await make_payment(
        user,
        status=PaymentStatus.PENDING,
        expires_at=FROZEN_NOW - timedelta(minutes=1),
    )

    result = await StaleInvoiceCleanupTask(uow_factory, settings).run()

    assert result.processed == 1

    async with uow_factory() as uow:
        stored = await uow.payments.get_by_id(payment.id)
    assert stored is not None and stored.status is PaymentStatus.EXPIRED


async def test_cleanup_keeps_fresh_invoice(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    user: User,
    make_payment,
    frozen_time,
) -> None:
    payment = await make_payment(
        user,
        status=PaymentStatus.PENDING,
        expires_at=FROZEN_NOW + timedelta(minutes=10),
    )

    result = await StaleInvoiceCleanupTask(uow_factory, settings).run()

    assert result.processed == 0, "Действующий счёт трогать нельзя"
    async with uow_factory() as uow:
        stored = await uow.payments.get_by_id(payment.id)
    assert stored is not None and stored.status is PaymentStatus.PENDING


async def test_cleanup_never_touches_paid_invoice(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    user: User,
    make_payment,
    frozen_time,
) -> None:
    # Оплаченный счёт с истёкшим сроком — обычное дело: срок относится к
    # окну оплаты, а не к самому платежу.
    payment = await make_payment(
        user,
        status=PaymentStatus.SUCCEEDED,
        external_id="ext_paid_1",
        paid_at=FROZEN_NOW - timedelta(hours=2),
        amount=Decimal(150),
        expires_at=FROZEN_NOW - timedelta(hours=1),
    )

    result = await StaleInvoiceCleanupTask(uow_factory, settings).run()

    assert result.processed == 0
    async with uow_factory() as uow:
        stored = await uow.payments.get_by_id(payment.id)
    assert stored is not None and stored.status is PaymentStatus.SUCCEEDED, (
        "Оплаченный счёт не может быть просрочен задним числом"
    )


async def test_cleanup_expires_invoice_only_after_deadline(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    user: User,
    make_payment,
    frozen_time,
) -> None:
    await make_payment(
        user,
        status=PaymentStatus.PENDING,
        expires_at=FROZEN_NOW + timedelta(minutes=5),
    )
    task = StaleInvoiceCleanupTask(uow_factory, settings)

    before = await task.run()
    frozen_time.tick(timedelta(minutes=10))
    after = await task.run()

    assert before.processed == 0, "До истечения срока счёт действителен"
    assert after.processed == 1, "После истечения срока счёт должен быть просрочен"


async def test_cleanup_is_idempotent(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    user: User,
    make_payment,
    frozen_time,
) -> None:
    await make_payment(
        user,
        status=PaymentStatus.PENDING,
        expires_at=FROZEN_NOW - timedelta(minutes=1),
    )
    task = StaleInvoiceCleanupTask(uow_factory, settings)

    first = await task.run()
    second = await task.run()

    assert first.processed == 1
    assert second.processed == 0, "Уже просроченный счёт не должен обрабатываться снова"


async def test_cleanup_frees_room_in_pending_limit(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    user: User,
    make_payment,
    frozen_time,
) -> None:
    # Смысл задачи именно в этом: просроченный счёт перестаёт занимать
    # место в лимите незавершённых.
    await make_payment(
        user,
        status=PaymentStatus.PENDING,
        expires_at=FROZEN_NOW - timedelta(minutes=1),
    )

    async with uow_factory() as uow:
        before = await uow.payments.count_pending(user.id)

    await StaleInvoiceCleanupTask(uow_factory, settings).run()

    async with uow_factory() as uow:
        after = await uow.payments.count_pending(user.id)

    assert before == 1
    assert after == 0, "Просроченный счёт не должен мешать выставить новый"


# --------------------------------------------------------------------------- #
# Общие свойства задач
# --------------------------------------------------------------------------- #


async def test_tasks_declare_names_and_intervals(
    uow_factory: UnitOfWorkFactory,
    mock_notifier: AsyncMock,
    settings: Settings,
) -> None:
    # Планировщик опирается на оба поля: имя попадает в логи и в отчёт об
    # ошибке, интервал — в расписание.
    tasks = [
        _expiration_task(uow_factory, mock_notifier, settings),
        _notification_task(uow_factory, mock_notifier, settings),
        StaleInvoiceCleanupTask(uow_factory, settings),
    ]

    for task in tasks:
        assert task.name, f"У задачи {type(task).__name__} нет имени"
        assert task.interval > 0, f"У задачи {task.name} некорректный интервал"

    assert len({task.name for task in tasks}) == len(tasks), "Имена задач должны различаться"


async def test_delivery_result_distinguishes_blocked_from_failed() -> None:
    # На этом различии держится компенсация в задачах: заблокировавшего
    # повторять не нужно, а недоставленное — обязательно.
    blocked = DeliveryResult(1, DeliveryStatus.BLOCKED)
    failed = DeliveryResult(1, DeliveryStatus.FAILED)

    assert blocked.blocked and not blocked.delivered
    assert not failed.blocked and not failed.delivered


async def test_frozen_clock_matches_expected_moment(frozen_time) -> None:
    # Проверка самого стенда: если заморозка перестанет работать, тесты
    # границ начнут врать, а не падать.
    assert datetime.now(tz=timezone.utc) == FROZEN_NOW
