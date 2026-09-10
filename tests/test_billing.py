"""Оплата звёздами Telegram: три шага и их идемпотентность.

Платёж проходит через выставление счёта, подтверждение до списания
(``PreCheckoutQuery``) и учёт списания (``SuccessfulPayment``). Опасен
здесь последний шаг: Telegram доставляет событие повторно, если ответ бота
не дошёл, и наивная реализация начислила бы срок подписки дважды.

Однократность обеспечивают два независимых ограничения, и проверяются оба:
блокировка строки платежа при подтверждении и уникальность ``payment_id`` в
журнале подписки. Второе — окончательное: платёж мог быть подтверждён
раньше, а упасть уже на начислении, и тогда «статус не менялся» ничего не
говорит о том, начислены ли дни.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from core.pricing import PLAN_OPTIONS, get_plan_option
from db.enums import PaymentProvider, PaymentStatus, SubscriptionSource, SubscriptionStatus
from db.models import User
from db.uow import UnitOfWork
from services.billing import BillingService
from services.billing.errors import (
    BillingError,
    PaymentMismatchError,
    PaymentNotFoundError,
)
from tests.conftest import FROZEN_NOW

pytestmark = pytest.mark.db

#: Вариант тарифа, на котором ведутся проверки.
OPTION_ID = PLAN_OPTIONS[0].id
OPTION = PLAN_OPTIONS[0]

CHARGE_ID = "tg_charge_0001"


async def _issue_invoice(billing: BillingService, user: User):
    """Выставляет счёт и возвращает запрос на оплату."""
    return await billing.create_invoice(user, OPTION_ID)


# --------------------------------------------------------------------------- #
# Шаг 1: выставление счёта
# --------------------------------------------------------------------------- #


async def test_create_invoice_stores_pending_payment(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    payment = await uow.payments.get_by_invoice_id(
        PaymentProvider.TELEGRAM_STARS, request.payload
    )
    assert payment is not None, "Счёт должен сохраняться до отправки пользователю"
    assert payment.status is PaymentStatus.PENDING
    assert payment.amount == Decimal(OPTION.stars)
    assert payment.period_days == OPTION.period_days


async def test_create_invoice_twice_issues_two_distinct_invoices(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    # Так задумано: ключ идемпотентности содержит случайную часть, потому
    # что человек вправе выставить новый счёт, если прежний протух. От
    # двойного нажатия защищает не этот слой, а middleware.
    first = await _issue_invoice(billing, user)
    second = await _issue_invoice(billing, user)

    assert first.payload != second.payload, (
        "Повторный запрос должен давать новый счёт, а не переиспользовать старый"
    )
    assert await uow.payments.count_pending(user.id) == 2


async def test_create_invoice_stops_at_pending_limit(
    billing: BillingService,
    settings,
    user: User,
) -> None:
    # Безграничная выдача счетов позволила бы одному человеку засорить
    # таблицу платежей, поэтому число незавершённых ограничено.
    from services.billing.errors import TooManyPendingInvoicesError

    for _ in range(settings.billing.max_pending_invoices):
        await _issue_invoice(billing, user)

    with pytest.raises(TooManyPendingInvoicesError):
        await _issue_invoice(billing, user)


async def test_paid_invoice_frees_room_for_the_next_one(
    billing: BillingService,
    settings,
    user: User,
) -> None:
    # Лимит считает именно незавершённые счета: оплатив, человек должен
    # снова иметь возможность купить продление.
    from services.billing.errors import TooManyPendingInvoicesError

    requests = [
        await _issue_invoice(billing, user)
        for _ in range(settings.billing.max_pending_invoices)
    ]

    with pytest.raises(TooManyPendingInvoicesError):
        await _issue_invoice(billing, user)

    await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=requests[0].payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert await _issue_invoice(billing, user), "После оплаты счёт снова выставляется"


async def test_create_invoice_rejects_unknown_option(
    billing: BillingService,
    user: User,
) -> None:
    with pytest.raises(BillingError):
        await billing.create_invoice(user, "no_such_plan")


async def test_plan_option_lookup_is_exact(user: User) -> None:
    assert get_plan_option(OPTION_ID) is OPTION
    assert get_plan_option("no_such_plan") is None


# --------------------------------------------------------------------------- #
# Шаг 2: подтверждение до списания
# --------------------------------------------------------------------------- #


async def test_pre_checkout_accepts_matching_invoice(
    billing: BillingService,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    decision = await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload=request.payload,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert decision.ok, f"Корректный запрос отклонён: {decision.error_key}"


async def test_pre_checkout_marks_invoice_processing(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    # Переход в processing нужен, чтобы отличать «счёт выставлен» от
    # «пользователь дошёл до оплаты»: только вторые стоит разбирать вручную,
    # если деньги списались, а событие не пришло.
    request = await _issue_invoice(billing, user)

    await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload=request.payload,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    payment = await uow.payments.get_by_invoice_id(
        PaymentProvider.TELEGRAM_STARS, request.payload
    )
    assert payment is not None and payment.status is PaymentStatus.PROCESSING


async def test_pre_checkout_rejects_unknown_payload(
    billing: BillingService,
    user: User,
) -> None:
    decision = await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload="inv_nonexistent",
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert not decision.ok
    assert decision.error_key == "billing.precheckout.not_found"


async def test_pre_checkout_rejects_foreign_invoice(
    billing: BillingService,
    user: User,
    make_user,
) -> None:
    # invoice_payload приходит от клиента: без сверки плательщика чужим
    # счётом можно было бы оплатить свой тариф.
    stranger = await make_user()
    request = await _issue_invoice(billing, stranger)

    decision = await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload=request.payload,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert not decision.ok
    assert decision.error_key == "billing.precheckout.foreign"


async def test_pre_checkout_rejects_wrong_amount(
    billing: BillingService,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    decision = await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload=request.payload,
        total_amount=1,
        currency="XTR",
    )

    assert not decision.ok
    assert decision.error_key == "billing.precheckout.amount_mismatch"


async def test_pre_checkout_rejects_wrong_currency(
    billing: BillingService,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    decision = await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload=request.payload,
        total_amount=OPTION.stars,
        currency="USD",
    )

    assert not decision.ok
    assert decision.error_key == "billing.precheckout.currency_mismatch"


async def test_pre_checkout_rejects_expired_invoice(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)
    payment = await uow.payments.get_by_invoice_id(
        PaymentProvider.TELEGRAM_STARS, request.payload
    )
    assert payment is not None
    payment.expires_at = FROZEN_NOW - timedelta(days=400)
    await uow.flush()

    decision = await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload=request.payload,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert not decision.ok
    assert decision.error_key == "billing.precheckout.expired"


async def test_pre_checkout_rejects_already_paid_invoice(
    billing: BillingService,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)
    await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    decision = await billing.validate_pre_checkout(
        telegram_id=user.telegram_id,
        payload=request.payload,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert not decision.ok
    assert decision.error_key == "billing.precheckout.already_paid"


# --------------------------------------------------------------------------- #
# Шаг 3: учёт списания и идемпотентность
# --------------------------------------------------------------------------- #


async def test_successful_payment_activates_subscription(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    outcome = await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert outcome.newly_applied, "Первая оплата обязана начислить дни"
    assert outcome.days_granted == OPTION.period_days

    subscription = await uow.subscriptions.get_live(user.id)
    assert subscription is not None, "После оплаты должна появиться подписка"
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.source is SubscriptionSource.PAYMENT


async def test_successful_payment_marks_invoice_succeeded(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    payment = await uow.payments.get_by_invoice_id(
        PaymentProvider.TELEGRAM_STARS, request.payload
    )
    assert payment is not None
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.external_id == CHARGE_ID, "Транзакция провайдера должна сохраняться"
    assert payment.paid_at is not None


async def test_repeated_successful_payment_does_not_extend_subscription(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    # Главная проверка модуля. Telegram повторяет доставку события, если
    # ответ бота не дошёл; второе начисление удвоило бы оплаченный срок.
    request = await _issue_invoice(billing, user)

    first = await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )
    expires_after_first = (await uow.subscriptions.get_live(user.id)).expires_at

    second = await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )
    expires_after_second = (await uow.subscriptions.get_live(user.id)).expires_at

    assert first.newly_applied, "Первая доставка должна начислить дни"
    assert second.already_processed, "Повтор обязан сообщить, что платёж уже учтён"
    assert second.days_granted == 0, "Повтор не начисляет ни одного дня"
    assert expires_after_second == expires_after_first, (
        f"Срок подписки сдвинулся при повторе: было {expires_after_first}, "
        f"стало {expires_after_second}"
    )


async def test_repeated_payment_writes_single_subscription_event(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    # Событие подписки уникально по payment_id — это и есть тот ключ, на
    # котором держится однократность начисления.
    request = await _issue_invoice(billing, user)

    for _ in range(3):
        await billing.apply_successful_payment(
            telegram_id=user.telegram_id,
            payload=request.payload,
            charge_id=CHARGE_ID,
            total_amount=OPTION.stars,
            currency="XTR",
        )

    subscription = await uow.subscriptions.get_live(user.id)
    assert subscription is not None
    events = await uow.subscriptions.list_events(subscription.id)
    paid_events = [event for event in events if event.payment_id is not None]

    assert len(paid_events) == 1, (
        f"Три доставки одного платежа дали {len(paid_events)} начислений"
    )


async def test_payment_extends_existing_subscription_once(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
    make_subscription,
) -> None:
    # У уже существующей подписки оплата продлевает срок, а не создаёт
    # вторую. Повтор при этом по-прежнему не должен ничего добавлять.
    existing = await make_subscription(
        user, started_at=FROZEN_NOW, status=SubscriptionStatus.ACTIVE
    )
    original_expiry = existing.expires_at
    request = await _issue_invoice(billing, user)

    await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )
    extended = (await uow.subscriptions.get_live(user.id)).expires_at

    await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )
    after_repeat = (await uow.subscriptions.get_live(user.id)).expires_at

    assert extended > original_expiry, "Оплата должна продлевать существующую подписку"
    assert after_repeat == extended, "Повтор не должен продлевать её ещё раз"


async def test_new_subscription_is_not_extended_twice_on_creation(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    # Тонкость, которую легко потерять: подписка создаётся уже с
    # оплаченным периодом, поэтому дополнительно продлевать её нельзя —
    # иначе первая же оплата дала бы двойной срок.
    request = await _issue_invoice(billing, user)

    await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    subscription = await uow.subscriptions.get_live(user.id)
    assert subscription is not None
    granted = subscription.expires_at - subscription.started_at

    assert granted <= timedelta(days=OPTION.period_days) + timedelta(minutes=1), (
        f"Оплачен период {OPTION.period_days} сут., а выдано {granted}"
    )


async def test_successful_payment_of_unknown_invoice_is_rejected(
    billing: BillingService,
    user: User,
) -> None:
    # Деньги списаны, а счёта нет: молча проглотить нельзя, нужен разбор.
    with pytest.raises(PaymentNotFoundError):
        await billing.apply_successful_payment(
            telegram_id=user.telegram_id,
            payload="inv_nonexistent",
            charge_id=CHARGE_ID,
            total_amount=OPTION.stars,
            currency="XTR",
        )


async def test_successful_payment_with_wrong_amount_is_rejected(
    billing: BillingService,
    uow: UnitOfWork,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    with pytest.raises(PaymentMismatchError):
        await billing.apply_successful_payment(
            telegram_id=user.telegram_id,
            payload=request.payload,
            charge_id=CHARGE_ID,
            total_amount=OPTION.stars + 1,
            currency="XTR",
        )

    assert await uow.subscriptions.get_live(user.id) is None, (
        "Несовпавшая сумма не должна давать подписку"
    )


async def test_successful_payment_with_wrong_currency_is_rejected(
    billing: BillingService,
    user: User,
) -> None:
    request = await _issue_invoice(billing, user)

    with pytest.raises(PaymentMismatchError):
        await billing.apply_successful_payment(
            telegram_id=user.telegram_id,
            payload=request.payload,
            charge_id=CHARGE_ID,
            total_amount=OPTION.stars,
            currency="USD",
        )


async def test_payment_outcome_reports_expiry_for_repeat(
    billing: BillingService,
    user: User,
) -> None:
    # Повтор должен возвращать актуальный срок, а не пустоту: хендлер
    # показывает его пользователю в ответ «оплата уже учтена».
    request = await _issue_invoice(billing, user)
    first = await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    second = await billing.apply_successful_payment(
        telegram_id=user.telegram_id,
        payload=request.payload,
        charge_id=CHARGE_ID,
        total_amount=OPTION.stars,
        currency="XTR",
    )

    assert second.expires_at == first.expires_at, (
        "Повтор обязан вернуть ту же дату окончания, что и первая оплата"
    )
