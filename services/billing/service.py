"""Прикладной сервис биллинга Telegram Stars.

Модуль намеренно не зависит от aiogram: он принимает и возвращает простые
данные, а превращение их в вызовы Bot API — задача слоя хендлеров. Так
платёжную логику можно проверить без Telegram, а замена библиотеки не
затронет правила начисления.

Полный путь оплаты состоит из трёх шагов, и каждый может прийти повторно:

1. пользователь запросил счёт — создаётся строка ``payments`` в состоянии
   ``pending``; повторный запрос по тому же ключу возвращает тот же счёт;
2. Telegram присылает ``PreCheckoutQuery`` — сверяем сумму, валюту и
   плательщика, переводим счёт в ``processing`` и отвечаем за отведённые
   10 секунд, иначе оплата срывается;
3. приходит ``SuccessfulPayment`` — подтверждаем платёж и начисляем дни.
   Обе операции идемпотентны на уровне БД, поэтому повторная доставка
   события не приводит к двойному начислению.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final

from core.logger import get_logger
from core.pricing import STARS_CURRENCY, PlanOption, get_plan_option
from db.enums import PaymentProvider, PaymentStatus, SubscriptionPlan, SubscriptionSource, SubscriptionStatus
from db.models import Payment, User
from db.uow import UnitOfWork
from services.billing.errors import (
    PaymentMismatchError,
    PaymentNotFoundError,
    PlanNotFoundError,
    TooManyPendingInvoicesError,
)

logger = get_logger(__name__)

#: Сколько живёт неоплаченный счёт.
DEFAULT_INVOICE_TTL: Final[timedelta] = timedelta(minutes=15)

#: Сколько неоплаченных счетов допустимо держать одновременно.
MAX_PENDING_INVOICES: Final[int] = 3


@dataclass(frozen=True, slots=True)
class InvoiceRequest:
    """Данные для вызова ``sendInvoice``.

    Возвращаются в виде простых полей, а не готовых объектов aiogram, чтобы
    сервис оставался независимым от библиотеки.
    """

    payment_id: int
    title: str
    description: str
    payload: str
    currency: str
    label: str
    amount: int


@dataclass(frozen=True, slots=True)
class PreCheckoutDecision:
    """Ответ на ``PreCheckoutQuery``."""

    ok: bool
    error_message: str | None = None
    payment_id: int | None = None


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    """Итог обработки успешной оплаты."""

    payment_id: int
    plan: SubscriptionPlan
    days_granted: int
    expires_at: datetime | None
    newly_applied: bool

    @property
    def already_processed(self) -> bool:
        """Была ли оплата учтена ранее (повторная доставка события)."""
        return not self.newly_applied


class BillingService:
    """Сценарии оплаты подписки звёздами Telegram."""

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        invoice_ttl: timedelta = DEFAULT_INVOICE_TTL,
        max_pending: int = MAX_PENDING_INVOICES,
    ) -> None:
        self._uow = uow
        self._invoice_ttl = invoice_ttl
        self._max_pending = max_pending

    # ------------------------------------------------------------ шаг 1
    async def create_invoice(self, user: User, option_id: str) -> InvoiceRequest:
        """Готовит счёт на оплату выбранного тарифа.

        :param user: Плательщик.
        :param option_id: Идентификатор варианта оплаты из каталога.
        :return: Данные для отправки счёта в Telegram.
        :raises PlanNotFoundError: Неизвестный вариант оплаты.
        :raises TooManyPendingInvoicesError: Слишком много неоплаченных счетов.
        """
        option = get_plan_option(option_id)
        if option is None:
            logger.warning("Запрошен неизвестный тариф %r пользователем %s", option_id, user.id)
            raise PlanNotFoundError(option_id)

        pending = await self._uow.payments.count_pending(user.id)
        if pending >= self._max_pending:
            logger.warning(
                "Отказ в выставлении счёта: у пользователя %s уже %d незавершённых", user.id, pending
            )
            raise TooManyPendingInvoicesError(self._max_pending)

        now = datetime.now(tz=timezone.utc)
        invoice_id = Payment.generate_invoice_id()
        result = await self._uow.payments.create_invoice(
            user_id=user.id,
            provider=PaymentProvider.TELEGRAM_STARS,
            invoice_id=invoice_id,
            # Ключ идемпотентности включает случайную часть: пользователь
            # вправе выставить второй счёт на тот же тариф, если первый
            # протух. От двойного нажатия защищает SingleFlightMiddleware.
            idempotency_key=f"stars:{user.id}:{option.id}:{secrets.token_hex(6)}",
            amount=Decimal(option.stars),
            currency=STARS_CURRENCY,
            plan=option.plan,
            period_days=option.period_days,
            expires_at=now + self._invoice_ttl,
            payload={"option_id": option.id, "title": option.title},
        )

        logger.info(
            "Выставлен счёт id=%s пользователю %s: %s, %d звёзд",
            result.payment.id, user.id, option.id, option.stars,
        )
        return InvoiceRequest(
            payment_id=result.payment.id,
            title=option.title,
            description=option.description,
            payload=result.payment.invoice_id,
            currency=STARS_CURRENCY,
            label=option.title,
            amount=option.stars,
        )

    # ------------------------------------------------------------ шаг 2
    async def validate_pre_checkout(
        self,
        *,
        telegram_id: int,
        payload: str,
        total_amount: int,
        currency: str,
    ) -> PreCheckoutDecision:
        """Проверяет запрос подтверждения оплаты.

        Telegram ждёт ответ не дольше 10 секунд, поэтому здесь нет ни
        сетевых обращений, ни тяжёлых запросов — только сверка с уже
        сохранённым счётом.

        Проверяются все параметры, а не только существование счёта:
        ``invoice_payload`` приходит от клиента, и без сверки суммы и
        плательщика по чужому счёту можно было бы оплатить свой тариф.

        :param telegram_id: Идентификатор плательщика в Telegram.
        :param payload: Значение ``invoice_payload`` из запроса.
        :param total_amount: Сумма в минимальных единицах (для XTR — звёзды).
        :param currency: Валюта запроса.
        :return: Решение с текстом ошибки для показа пользователю.
        """
        payment = await self._uow.payments.get_by_invoice_id_for_update(
            PaymentProvider.TELEGRAM_STARS, payload
        )
        if payment is None:
            logger.warning("PreCheckout по неизвестному счёту %r от %s", payload, telegram_id)
            return PreCheckoutDecision(ok=False, error_message="Счёт не найден. Выставьте новый.")

        user = await self._uow.users.get(payment.user_id)
        if user is None or user.telegram_id != telegram_id:
            logger.error(
                "PreCheckout: счёт id=%s принадлежит другому пользователю (ожидался %s)",
                payment.id, telegram_id,
            )
            return PreCheckoutDecision(ok=False, error_message="Счёт выставлен другому пользователю.")

        if payment.status is PaymentStatus.SUCCEEDED:
            logger.info("PreCheckout по уже оплаченному счёту id=%s", payment.id)
            return PreCheckoutDecision(ok=False, error_message="Этот счёт уже оплачен.")

        if payment.status not in (PaymentStatus.PENDING, PaymentStatus.PROCESSING):
            logger.warning(
                "PreCheckout по счёту id=%s в состоянии %s", payment.id, payment.status
            )
            return PreCheckoutDecision(ok=False, error_message="Счёт больше не действителен.")

        now = datetime.now(tz=timezone.utc)
        if payment.expires_at is not None and payment.expires_at <= now:
            logger.info("PreCheckout по просроченному счёту id=%s", payment.id)
            return PreCheckoutDecision(ok=False, error_message="Срок действия счёта истёк.")

        if currency.upper() != payment.currency:
            logger.error(
                "PreCheckout: валюта %s не совпадает с счётом id=%s (%s)",
                currency, payment.id, payment.currency,
            )
            return PreCheckoutDecision(ok=False, error_message="Валюта платежа не совпадает со счётом.")

        if Decimal(total_amount) != payment.amount:
            logger.error(
                "PreCheckout: сумма %s не совпадает с счётом id=%s (%s)",
                total_amount, payment.id, payment.amount,
            )
            return PreCheckoutDecision(ok=False, error_message="Сумма платежа не совпадает со счётом.")

        if payment.status is PaymentStatus.PENDING:
            payment.mark_processing()
            await self._uow.flush()

        logger.info("PreCheckout подтверждён для счёта id=%s", payment.id)
        return PreCheckoutDecision(ok=True, payment_id=payment.id)

    # ------------------------------------------------------------ шаг 3
    async def apply_successful_payment(
        self,
        *,
        telegram_id: int,
        payload: str,
        charge_id: str,
        total_amount: int,
        currency: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> PaymentOutcome:
        """Подтверждает оплату и начисляет дни подписки.

        Операция целиком идемпотентна: подтверждение платежа защищено
        блокировкой строки и сверкой ``external_id``, а начисление — тем,
        что событие подписки уникально по ``payment_id``. Повторная
        доставка события вернёт тот же результат с ``newly_applied=False``.

        :param telegram_id: Плательщик.
        :param payload: ``invoice_payload`` из ``SuccessfulPayment``.
        :param charge_id: ``telegram_payment_charge_id``.
        :param total_amount: Списанная сумма.
        :param currency: Валюта списания.
        :param raw_payload: Сырое событие для аудита.
        :return: Итог с новой датой окончания подписки.
        :raises PaymentNotFoundError: Счёт не найден.
        :raises PaymentMismatchError: Параметры не совпали со счётом.
        """
        now = datetime.now(tz=timezone.utc)
        # Признак изменения статуса здесь не нужен: источником истины о
        # начислении служит grant.applied — платёж мог быть подтверждён
        # раньше, но упасть до начисления дней.
        payment, _ = await self._uow.payments.confirm_payment(
            provider=PaymentProvider.TELEGRAM_STARS,
            invoice_id=payload,
            external_id=charge_id,
            paid_at=now,
            payload=raw_payload or {},
        )
        if payment is None:
            # Деньги списаны, а счёта нет — ситуация требует ручного разбора,
            # поэтому пишем в лог всё, что известно о платеже.
            logger.error(
                "Оплата по неизвестному счёту: payload=%r charge_id=%s telegram_id=%s",
                payload, charge_id, telegram_id,
            )
            raise PaymentNotFoundError(payload)

        self._ensure_matches(payment, total_amount, currency)

        subscription_result = await self._uow.subscriptions.get_or_create_live(
            payment.user_id,
            plan=payment.plan,
            source=SubscriptionSource.PAYMENT,
            status=SubscriptionStatus.ACTIVE,
            period_days=payment.period_days,
            now=now,
        )

        # Событие фиксируется в обоих случаях — оно и есть ключ
        # идемпотентности. Но продление выполняется только если подписка
        # существовала раньше: у только что созданной оплаченный период уже
        # заложен в срок, и повторное начисление удвоило бы его.
        grant = await self._uow.subscriptions.apply_payment_grant(
            payment_id=payment.id,
            subscription_id=subscription_result.subscription.id,
            user_id=payment.user_id,
            days=payment.period_days,
            plan=payment.plan,
            payload={"charge_id": charge_id, "currency": currency},
            extend=not subscription_result.created,
        )

        outcome = PaymentOutcome(
            payment_id=payment.id,
            plan=payment.plan,
            days_granted=payment.period_days if grant.applied else 0,
            expires_at=grant.expires_at or subscription_result.subscription.expires_at,
            newly_applied=grant.applied,
        )

        logger.info(
            "Оплата счёта id=%s учтена: план %s, подписка до %s, начислено=%s",
            payment.id, payment.plan, outcome.expires_at, outcome.newly_applied,
        )
        return outcome

    # ------------------------------------------------------------ возвраты
    async def mark_refunded(self, payment_id: int, reason: str | None = None) -> Payment:
        """Отмечает платёж возвращённым.

        Вызывается после успешного ``refundStarPayment``: сначала деньги
        возвращает Telegram, затем состояние фиксируется у нас. Обратный
        порядок оставил бы платёж «возвращённым» при неудачном возврате.

        :param payment_id: Идентификатор платежа.
        :param reason: Причина возврата.
        :return: Обновлённый платёж.
        :raises PaymentNotFoundError: Платёж не найден.
        """
        payment = await self._uow.payments.get_for_update(payment_id)
        if payment is None:
            raise PaymentNotFoundError(str(payment_id))

        payment.mark_refunded(reason)
        await self._uow.flush()
        return payment

    @staticmethod
    def _ensure_matches(payment: Payment, total_amount: int, currency: str) -> None:
        """Сверяет фактическое списание с выставленным счётом.

        :raises PaymentMismatchError: Сумма или валюта отличаются.
        """
        if currency.upper() != payment.currency:
            raise PaymentMismatchError(
                f"Валюта платежа {currency} не совпадает со счётом {payment.currency}."
            )
        if Decimal(total_amount) != payment.amount:
            raise PaymentMismatchError(
                f"Сумма платежа {total_amount} не совпадает со счётом {payment.amount}."
            )

    @staticmethod
    def describe_option(option: PlanOption) -> str:
        """Формирует человекочитаемое описание тарифа."""
        features = "\n".join(f"• {item}" for item in option.features)
        return f"{option.description}\n\n{features}" if features else option.description
