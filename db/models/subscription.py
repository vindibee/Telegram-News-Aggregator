"""Модели подписки и журнала операций над ней."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import (
    LIVE_SUBSCRIPTION_STATUSES,
    SubscriptionEventKind,
    SubscriptionPlan,
    SubscriptionSource,
    SubscriptionStatus,
    pg_enum,
)
from db.exceptions import InvalidPeriodError, InvalidStateTransitionError
from db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from db.models.payment import Payment
    from db.models.user import User

logger = get_logger(__name__)

#: Допустимые переходы состояний подписки.
_ALLOWED_TRANSITIONS: Final[dict[SubscriptionStatus, frozenset[SubscriptionStatus]]] = {
    SubscriptionStatus.TRIALING: frozenset(
        {SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED}
    ),
    SubscriptionStatus.ACTIVE: frozenset(
        {
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.EXPIRED,
            SubscriptionStatus.CANCELLED,
        }
    ),
    SubscriptionStatus.PAST_DUE: frozenset(
        {SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED}
    ),
    # Из терминальных состояний вернуться можно только оплатой.
    SubscriptionStatus.EXPIRED: frozenset({SubscriptionStatus.ACTIVE}),
    SubscriptionStatus.CANCELLED: frozenset({SubscriptionStatus.ACTIVE}),
}


class Subscription(Base, IdMixin, TimestampMixin):
    """Подписка пользователя на тарифный план.

    История подписок сохраняется целиком (одна строка на цикл), но
    действующей может быть только одна — это гарантирует частичный
    уникальный индекс, а не проверка в коде: параллельные воркеры и
    хендлеры иначе создали бы дубликаты в состоянии гонки.
    """

    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan: Mapped[SubscriptionPlan] = mapped_column(
        pg_enum(SubscriptionPlan, "subscription_plan"),
        nullable=False,
        default=SubscriptionPlan.FREE,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        pg_enum(SubscriptionStatus, "subscription_status"),
        nullable=False,
        default=SubscriptionStatus.TRIALING,
    )
    source: Mapped[SubscriptionSource] = mapped_column(
        pg_enum(SubscriptionSource, "subscription_source"),
        nullable=False,
        default=SubscriptionSource.TRIAL,
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    auto_renew: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Момент отправки уведомления «истекает через 24 часа». Хранится именно
    # отметка времени, а не флаг: она делает рассылку идемпотентной при
    # перезапуске воркера и переиспользуется для следующего цикла.
    expiry_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship("User", back_populates="subscriptions", lazy="raise")
    events: Mapped[list[SubscriptionEvent]] = relationship(
        "SubscriptionEvent",
        back_populates="subscription",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint("expires_at > started_at", name="period_order"),
        # Ровно одна действующая подписка на пользователя.
        Index(
            "uq_subscriptions_one_live_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('trialing', 'active')"),
        ),
        # Основной запрос воркера: «что истекает в ближайшие сутки».
        Index("ix_subscriptions_status_expires_at", "status", "expires_at"),
        Index("ix_subscriptions_user_id_created_at", "user_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<Subscription id={self.id} user_id={self.user_id} "
            f"plan={self.plan} status={self.status} expires_at={self.expires_at}>"
        )

    def is_live(self, moment: datetime) -> bool:
        """Действует ли подписка в указанный момент.

        :param moment: Момент проверки (timezone-aware).
        :return: ``True``, если статус действующий и срок не истёк.
        """
        return self.status in LIVE_SUBSCRIPTION_STATUSES and self.expires_at > moment

    def days_left(self, moment: datetime) -> int:
        """Сколько полных суток осталось до окончания подписки.

        :param moment: Момент отсчёта (timezone-aware).
        :return: Неотрицательное число суток (округление вверх).
        """
        if self.expires_at <= moment:
            return 0
        return max(0, math.ceil((self.expires_at - moment).total_seconds() / 86400))

    def transition_to(self, target: SubscriptionStatus, moment: datetime) -> None:
        """Переводит подписку в новое состояние.

        :param target: Целевой статус.
        :param moment: Момент перехода (timezone-aware).
        :raises InvalidStateTransitionError: Переход запрещён автоматом состояний.
        """
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if target not in allowed:
            logger.warning(
                "Запрещённый переход подписки id=%s: %s -> %s", self.id, self.status, target
            )
            raise InvalidStateTransitionError("Subscription", self.status, target)

        previous = self.status
        self.status = target
        if target is SubscriptionStatus.CANCELLED:
            self.cancelled_at = moment
            self.auto_renew = False

        logger.info("Подписка id=%s: статус %s -> %s", self.id, previous, target)

    def extend(self, days: int, moment: datetime) -> datetime:
        """Продлевает подписку на указанное число суток.

        Если срок ещё не истёк, дни прибавляются к текущей дате окончания
        (пользователь не теряет остаток), иначе отсчёт идёт от текущего
        момента.

        :param days: Количество суток (строго положительное).
        :param moment: Момент выполнения операции (timezone-aware).
        :return: Новая дата окончания подписки.
        :raises InvalidPeriodError: Некорректное число суток.
        """
        if days <= 0:
            logger.error("Некорректное продление подписки id=%s на %s суток", self.id, days)
            raise InvalidPeriodError(days)

        base = self.expires_at if self.expires_at > moment else moment
        self.expires_at = base + timedelta(days=days)
        # Продление снимает ранее отправленное предупреждение об окончании.
        self.expiry_notified_at = None
        logger.info(
            "Подписка id=%s продлена на %d суток, новая дата окончания %s",
            self.id, days, self.expires_at,
        )
        return self.expires_at

    def mark_expiry_notified(self, moment: datetime) -> bool:
        """Отмечает отправку уведомления об окончании подписки.

        :param moment: Момент отправки (timezone-aware).
        :return: ``False``, если уведомление уже отправлялось для этого цикла.
        """
        if self.expiry_notified_at is not None:
            logger.debug(
                "Уведомление об окончании подписки id=%s уже отправлено в %s",
                self.id, self.expiry_notified_at,
            )
            return False

        self.expiry_notified_at = moment
        logger.info("Отмечено уведомление об окончании подписки id=%s", self.id)
        return True


class SubscriptionEvent(Base, IdMixin):
    """Журнал операций над подпиской.

    Ключевой элемент защиты от двойного зачисления: ``payment_id`` уникален,
    поэтому один платёж физически не способен начислить дни дважды — даже
    при повторной доставке вебхука или гонке двух воркеров вторая вставка
    завершится нарушением уникальности.
    """

    __tablename__ = "subscription_events"

    subscription_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    payment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[SubscriptionEventKind] = mapped_column(
        pg_enum(SubscriptionEventKind, "subscription_event_kind"),
        nullable=False,
    )
    days_granted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    subscription: Mapped[Subscription] = relationship(
        "Subscription", back_populates="events", lazy="raise"
    )
    payment: Mapped[Payment | None] = relationship(
        "Payment", back_populates="subscription_event", lazy="raise"
    )

    __table_args__ = (
        # NULL-значения в PostgreSQL не конфликтуют между собой, поэтому
        # ограничение действует только на события, связанные с платежом.
        UniqueConstraint("payment_id", name="uq_subscription_events_payment_id"),
        CheckConstraint("days_granted >= 0", name="days_granted_non_negative"),
        Index("ix_subscription_events_subscription_id_created_at", "subscription_id", "created_at"),
        Index("ix_subscription_events_user_id_created_at", "user_id", "created_at"),
        Index("ix_subscription_events_kind_created_at", "kind", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<SubscriptionEvent id={self.id} subscription_id={self.subscription_id} "
            f"kind={self.kind} days_granted={self.days_granted}>"
        )
