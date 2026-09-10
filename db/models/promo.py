"""Промокоды и их активации."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import PromocodeKind, SubscriptionPlan, pg_enum
from db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from db.models.payment import Payment
    from db.models.user import User

logger = get_logger(__name__)

#: Длина автоматически сгенерированного кода.
PROMOCODE_LENGTH: Final[int] = 8

#: Алфавит без визуально неоднозначных символов: код диктуют голосом и
#: переписывают с картинки, и пара «0/O» стоит дороже лишнего символа.
_CODE_ALPHABET: Final[str] = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: Верхняя граница скидки в процентах.
MAX_DISCOUNT_PERCENT: Final[int] = 100


class Promocode(Base, IdMixin, TimestampMixin):
    """Промокод на бонусные дни или скидку.

    Счётчик активаций хранится в самой строке, а не считается запросом по
    таблице активаций: проверка лимита выполняется на каждое применение
    кода, и подсчёт миллионов строк ради одного числа — гарантированный
    источник тормозов. Расхождение исключено тем, что счётчик меняется в
    той же транзакции, что и запись об активации.
    """

    __tablename__ = "promocodes"

    #: Код в верхнем регистре: пользователь вводит как попало, а
    #: уникальность должна работать независимо от регистра.
    code: Mapped[str] = mapped_column(String(32), nullable=False)

    kind: Mapped[PromocodeKind] = mapped_column(
        pg_enum(PromocodeKind, "promocode_kind"),
        nullable=False,
    )

    #: Смысл зависит от типа: дни для ``bonus_days``, проценты для
    #: ``discount_percent``.
    value: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Ограничение по тарифу; ``NULL`` — код действует на любой.
    plan: Mapped[SubscriptionPlan | None] = mapped_column(
        pg_enum(SubscriptionPlan, "subscription_plan"),
        nullable=True,
    )

    #: Глобальный лимит активаций; ``NULL`` — без ограничения.
    max_activations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_by_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    redemptions: Mapped[list[PromocodeRedemption]] = relationship(
        "PromocodeRedemption",
        back_populates="promocode",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("code", name="uq_promocodes_code"),
        CheckConstraint("value > 0", name="value_positive"),
        CheckConstraint("activations >= 0", name="activations_non_negative"),
        CheckConstraint(
            "max_activations IS NULL OR max_activations > 0",
            name="max_activations_positive",
        ),
        # Счётчик не может обогнать лимит: даже ошибка в прикладном коде
        # не превратит код в бесконечный.
        CheckConstraint(
            "max_activations IS NULL OR activations <= max_activations",
            name="activations_within_limit",
        ),
        CheckConstraint(
            "kind <> 'discount_percent' OR value <= 100",
            name="discount_within_100",
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_until IS NULL OR valid_until > valid_from",
            name="validity_period_order",
        ),
        # Частичный индекс: выборка идёт только по действующим кодам.
        Index(
            "ix_promocodes_active",
            "code",
            postgresql_where=text("is_active"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<Promocode id={self.id} code={self.code!r} kind={self.kind}>"

    @property
    def is_exhausted(self) -> bool:
        """Исчерпан ли лимит активаций."""
        return self.max_activations is not None and self.activations >= self.max_activations

    def is_redeemable(self, moment: datetime, plan: SubscriptionPlan | None = None) -> bool:
        """Можно ли применить код прямо сейчас.

        Проверяет только свойства самого кода. Ограничение «один код на
        пользователя» проверяется уникальностью в
        :class:`PromocodeRedemption`, а не здесь.

        :param moment: Момент проверки (timezone-aware).
        :param plan: Тариф, к которому применяется код.
        :return: Доступность кода.
        """
        if not self.is_active or self.is_exhausted:
            return False
        if self.valid_from is not None and moment < self.valid_from:
            return False
        if self.valid_until is not None and moment >= self.valid_until:
            return False
        if self.plan is not None and plan is not None and self.plan is not plan:
            return False
        return True

    def register_activation(self) -> None:
        """Увеличивает счётчик активаций.

        :raises ValueError: Лимит активаций уже исчерпан.
        """
        if self.is_exhausted:
            raise ValueError(f"Промокод {self.code} исчерпан: {self.activations} активаций.")
        self.activations += 1
        logger.info(
            "Промокод %s активирован (%d из %s)",
            self.code, self.activations, self.max_activations or "∞",
        )

    def bonus_days_for(self, base_days: int) -> int:
        """Считает, сколько дней добавляет код к оплаченному периоду.

        Скидка в процентах пересчитывается в дни: подписка продаётся
        периодами, и «минус 20 %» осмысленно выражать сроком, а не
        возвратом части суммы.

        :param base_days: Оплаченный период в сутках.
        :return: Количество бонусных дней (не меньше нуля).
        :raises ValueError: Некорректный базовый период.
        """
        if base_days <= 0:
            raise ValueError(f"Базовый период должен быть положительным, получено: {base_days}")

        if self.kind is PromocodeKind.BONUS_DAYS:
            return self.value
        return max(0, round(base_days * self.value / MAX_DISCOUNT_PERCENT))

    @staticmethod
    def normalize_code(raw: str) -> str:
        """Приводит введённый код к каноническому виду.

        :param raw: Пользовательский ввод.
        :return: Код в верхнем регистре без пробелов и дефисов.
        :raises ValueError: Пустой или слишком длинный код.
        """
        value = raw.strip().upper().replace(" ", "").replace("-", "")
        if not value:
            raise ValueError("Промокод не может быть пустым.")
        if len(value) > 32:
            raise ValueError(f"Промокод длиннее 32 символов: {len(value)}.")
        return value

    @staticmethod
    def generate_code(length: int = PROMOCODE_LENGTH) -> str:
        """Генерирует случайный код.

        :param length: Длина кода (не меньше 4 символов).
        :return: Код из символов без визуально неоднозначных знаков.
        :raises ValueError: Запрошена слишком малая длина.
        """
        if length < 4:
            raise ValueError(f"Длина промокода должна быть не меньше 4, получено: {length}")
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


class PromocodeRedemption(Base, IdMixin):
    """Факт применения промокода пользователем.

    Существует ради идемпотентности: уникальность пары «код + пользователь»
    физически не даёт применить один код дважды, сколько бы раз человек ни
    нажал кнопку. Проверка «а не применял ли он уже» отдельным запросом
    оставляла бы окно гонки.
    """

    __tablename__ = "promocode_redemptions"

    promocode_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("promocodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Платёж, к которому применён код (для скидок).
    payment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
    )

    days_granted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    promocode: Mapped[Promocode] = relationship(
        "Promocode", back_populates="redemptions", lazy="raise"
    )
    user: Mapped[User] = relationship(
        "User", back_populates="promocode_redemptions", lazy="raise"
    )
    payment: Mapped[Payment | None] = relationship("Payment", lazy="raise")

    __table_args__ = (
        UniqueConstraint("promocode_id", "user_id", name="uq_promocode_redemptions_user"),
        CheckConstraint("days_granted >= 0", name="days_granted_non_negative"),
        Index("ix_promocode_redemptions_user_id", "user_id"),
        Index("ix_promocode_redemptions_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<PromocodeRedemption id={self.id} promocode_id={self.promocode_id} "
            f"user_id={self.user_id}>"
        )
