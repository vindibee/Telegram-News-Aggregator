"""Модель платежа с гарантиями идемпотентности."""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import (
    FINAL_PAYMENT_STATUSES,
    PaymentProvider,
    PaymentStatus,
    SubscriptionPlan,
    pg_enum,
)
from db.exceptions import InvalidStateTransitionError
from db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from db.models.subscription import SubscriptionEvent
    from db.models.user import User

logger = get_logger(__name__)

#: Допустимые переходы состояний платежа.
_ALLOWED_TRANSITIONS: Final[dict[PaymentStatus, frozenset[PaymentStatus]]] = {
    PaymentStatus.PENDING: frozenset(
        {
            PaymentStatus.PROCESSING,
            # Telegram Stars присылают successful_payment без промежуточного
            # состояния, поэтому прямой переход разрешён.
            PaymentStatus.SUCCEEDED,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
        }
    ),
    PaymentStatus.PROCESSING: frozenset(
        {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.EXPIRED}
    ),
    PaymentStatus.SUCCEEDED: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.EXPIRED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
}

#: Длина случайной части идентификатора счёта в байтах.
_INVOICE_ENTROPY_BYTES: Final[int] = 16


class Payment(Base, IdMixin, TimestampMixin):
    """Платёж пользователя за подписку.

    Идемпотентность обеспечивается тремя независимыми ограничениями:

    * ``(provider, invoice_id)`` — повторная выдача счёта не создаёт дубль;
    * ``(provider, external_id)`` — повторная доставка колбэка провайдера
      (Telegram может прислать ``successful_payment`` дважды) не создаёт
      второй платёж;
    * ``idempotency_key`` — защита на уровне прикладного запроса.

    Начисление дней подписки защищено отдельно — уникальным ``payment_id``
    в :class:`db.models.subscription.SubscriptionEvent`.
    """

    __tablename__ = "payments"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        # RESTRICT, а не CASCADE: финансовые записи не должны исчезать
        # вместе с пользователем — они нужны для отчётности и сверки.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        pg_enum(PaymentProvider, "payment_provider"), nullable=False
    )
    status: Mapped[PaymentStatus] = mapped_column(
        pg_enum(PaymentStatus, "payment_status"),
        nullable=False,
        default=PaymentStatus.PENDING,
    )

    #: Идентификатор счёта, сгенерированный нами (invoice payload).
    invoice_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Идентификатор транзакции на стороне провайдера.
    #: Для Telegram — ``telegram_payment_charge_id``.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    #: NUMERIC, а не FLOAT: двоичная плавающая точка теряет копейки,
    #: что недопустимо в финансовых расчётах и сверке MRR.
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)

    plan: Mapped[SubscriptionPlan] = mapped_column(
        pg_enum(SubscriptionPlan, "subscription_plan"), nullable=False
    )
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Сырой ответ провайдера — нужен для разбора спорных списаний.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="payments", lazy="raise")
    subscription_event: Mapped[SubscriptionEvent | None] = relationship(
        "SubscriptionEvent",
        back_populates="payment",
        uselist=False,
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("provider", "invoice_id", name="uq_payments_provider_invoice_id"),
        UniqueConstraint("provider", "external_id", name="uq_payments_provider_external_id"),
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("period_days > 0", name="period_days_positive"),
        CheckConstraint(
            "(status <> 'succeeded') OR (external_id IS NOT NULL AND paid_at IS NOT NULL)",
            name="succeeded_has_external_id",
        ),
        Index("ix_payments_user_id_created_at", "user_id", "created_at"),
        Index("ix_payments_status_created_at", "status", "created_at"),
        # Аналитика MRR читает только успешные платежи — частичный индекс
        # заметно меньше полного и не растёт от неоплаченных счетов.
        Index(
            "ix_payments_paid_at_succeeded",
            "paid_at",
            postgresql_where=text("status = 'succeeded'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<Payment id={self.id} user_id={self.user_id} provider={self.provider} "
            f"status={self.status} amount={self.amount} {self.currency}>"
        )

    @property
    def is_final(self) -> bool:
        """Находится ли платёж в терминальном состоянии."""
        return self.status in FINAL_PAYMENT_STATUSES

    def _transition_to(self, target: PaymentStatus) -> None:
        """Проверяет и выполняет переход статуса.

        :param target: Целевой статус.
        :raises InvalidStateTransitionError: Переход запрещён автоматом состояний.
        """
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if target not in allowed:
            logger.error(
                "Запрещённый переход платежа id=%s invoice_id=%s: %s -> %s",
                self.id, self.invoice_id, self.status, target,
            )
            raise InvalidStateTransitionError("Payment", self.status, target)

        previous = self.status
        self.status = target
        logger.info(
            "Платёж id=%s invoice_id=%s: статус %s -> %s",
            self.id, self.invoice_id, previous, target,
        )

    def mark_processing(self) -> None:
        """Фиксирует подтверждение счёта (ответ на ``PreCheckoutQuery``).

        :raises InvalidStateTransitionError: Платёж уже в другом состоянии.
        """
        self._transition_to(PaymentStatus.PROCESSING)

    def mark_succeeded(
        self,
        external_id: str,
        paid_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Фиксирует успешную оплату.

        Метод идемпотентен для повторной доставки одного и того же события:
        если платёж уже успешен и ``external_id`` совпадает, возвращается
        ``False`` без изменения состояния.

        :param external_id: Идентификатор транзакции у провайдера.
        :param paid_at: Момент списания средств (timezone-aware).
        :param payload: Сырой ответ провайдера для аудита.
        :return: ``True``, если статус изменился этим вызовом.
        :raises ValueError: Пустой ``external_id``.
        :raises InvalidStateTransitionError: Недопустимый переход состояния.
        """
        if not external_id:
            raise ValueError("external_id обязателен для успешного платежа.")

        if self.status is PaymentStatus.SUCCEEDED:
            if self.external_id == external_id:
                logger.info(
                    "Повторное подтверждение платежа id=%s external_id=%s проигнорировано",
                    self.id, external_id,
                )
                return False
            logger.error(
                "Конфликт подтверждения платежа id=%s: сохранён external_id=%s, получен %s",
                self.id, self.external_id, external_id,
            )
            raise InvalidStateTransitionError("Payment", self.status, PaymentStatus.SUCCEEDED)

        self._transition_to(PaymentStatus.SUCCEEDED)
        self.external_id = external_id
        self.paid_at = paid_at
        if payload is not None:
            self.payload = payload
        return True

    def mark_failed(self, reason: str) -> None:
        """Фиксирует неуспешный платёж.

        :param reason: Причина отказа (обрезается до 255 символов).
        :raises InvalidStateTransitionError: Недопустимый переход состояния.
        """
        self._transition_to(PaymentStatus.FAILED)
        self.failure_reason = reason[:255]
        logger.warning("Платёж id=%s отклонён: %s", self.id, self.failure_reason)

    def mark_expired(self) -> None:
        """Помечает неоплаченный счёт как просроченный.

        :raises InvalidStateTransitionError: Недопустимый переход состояния.
        """
        self._transition_to(PaymentStatus.EXPIRED)

    def mark_refunded(self, reason: str | None = None) -> None:
        """Фиксирует возврат средств.

        :param reason: Причина возврата.
        :raises InvalidStateTransitionError: Возврат возможен только из ``succeeded``.
        """
        self._transition_to(PaymentStatus.REFUNDED)
        if reason:
            self.failure_reason = reason[:255]
        logger.warning("Возврат по платежу id=%s: %s", self.id, reason or "причина не указана")

    @staticmethod
    def generate_invoice_id(prefix: str = "inv") -> str:
        """Генерирует идентификатор счёта.

        Значение уходит клиенту как ``invoice_payload``, поэтому оно должно
        быть непредсказуемым: по угаданному payload можно было бы подменить
        чужой счёт.

        :param prefix: Короткий префикс для читаемости в логах.
        :return: Идентификатор вида ``inv_<32 hex-символа>``.
        :raises ValueError: Пустой или слишком длинный префикс.
        """
        if not prefix or len(prefix) > 8:
            raise ValueError(f"Префикс счёта должен быть длиной 1..8 символов, получено: {prefix!r}")
        return f"{prefix}_{secrets.token_hex(_INVOICE_ENTROPY_BYTES)}"
