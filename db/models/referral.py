"""Реферальные начисления."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import FINAL_REFERRAL_STATUSES, ReferralStatus, pg_enum
from db.exceptions import InvalidStateTransitionError
from db.mixins import IdMixin

if TYPE_CHECKING:
    from db.models.payment import Payment
    from db.models.user import User

logger = get_logger(__name__)

#: Допустимые переходы состояния реферала.
_TRANSITIONS: dict[ReferralStatus, frozenset[ReferralStatus]] = {
    ReferralStatus.PENDING: frozenset({ReferralStatus.QUALIFIED, ReferralStatus.REJECTED}),
    ReferralStatus.QUALIFIED: frozenset({ReferralStatus.REWARDED, ReferralStatus.REJECTED}),
    ReferralStatus.REWARDED: frozenset(),
    ReferralStatus.REJECTED: frozenset(),
}


class Referral(Base, IdMixin):
    """Приглашение одного пользователя другим.

    Связь «кто кого привёл» уже хранится в ``users.referred_by_id``, но
    этого мало: бонус выдаётся не сразу, и нужно помнить, был ли он уже
    начислен, сколько дней и за какой платёж. Без отдельной записи
    повторный запуск обработчика оплаты начислил бы дни второй раз.

    Вознаграждение привязано к первой оплате приглашённого, а не к его
    регистрации: иначе реферальную программу выгодно фармить пустыми
    аккаунтами.
    """

    __tablename__ = "referrals"

    referrer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Приглашённый. Уникален: человека можно привести только один раз.
    referred_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Код, по которому пришёл приглашённый. Хранится копией: владелец
    #: вправе сменить код, но история начислений от этого меняться не должна.
    code: Mapped[str] = mapped_column(String(16), nullable=False)

    status: Mapped[ReferralStatus] = mapped_column(
        pg_enum(ReferralStatus, "referral_status"),
        nullable=False,
        default=ReferralStatus.PENDING,
        server_default=ReferralStatus.PENDING.value,
    )

    #: Сколько дней начислено пригласившему.
    bonus_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: Платёж приглашённого, сделавший приглашение «зачётным».
    payment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
    )

    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    referrer: Mapped[User] = relationship(
        "User",
        back_populates="referrals_made",
        foreign_keys=[referrer_id],
        lazy="raise",
    )
    referred: Mapped[User] = relationship(
        "User",
        back_populates="referral_source",
        foreign_keys=[referred_id],
        lazy="raise",
    )
    payment: Mapped[Payment | None] = relationship("Payment", lazy="raise")

    __table_args__ = (
        # Ключ идемпотентности всей реферальной программы: приглашённый
        # учитывается ровно один раз, сколько бы раз ни пришёл вебхук.
        UniqueConstraint("referred_id", name="uq_referrals_referred_id"),
        CheckConstraint("referrer_id <> referred_id", name="no_self_referral"),
        CheckConstraint("bonus_days >= 0", name="bonus_days_non_negative"),
        CheckConstraint(
            "status <> 'rewarded' OR (rewarded_at IS NOT NULL AND bonus_days > 0)",
            name="rewarded_has_bonus",
        ),
        Index("ix_referrals_referrer_id_status", "referrer_id", "status"),
        Index("ix_referrals_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<Referral id={self.id} referrer_id={self.referrer_id} "
            f"referred_id={self.referred_id} status={self.status}>"
        )

    @property
    def is_final(self) -> bool:
        """Достигнуто ли состояние, из которого нет переходов."""
        return self.status in FINAL_REFERRAL_STATUSES

    def _transition_to(self, target: ReferralStatus) -> None:
        """Переводит запись в новое состояние.

        :param target: Целевое состояние.
        :raises InvalidStateTransitionError: Переход запрещён правилами.
        """
        if target not in _TRANSITIONS[self.status]:
            logger.warning(
                "Запрещённый переход реферала id=%s: %s -> %s", self.id, self.status, target
            )
            raise InvalidStateTransitionError("Referral", self.status, target)
        self.status = target

    def qualify(self, payment_id: int, moment: datetime) -> bool:
        """Отмечает приглашение зачётным после оплаты приглашённого.

        Повторный вызов безопасен: уже зачтённое приглашение возвращает
        ``False`` вместо ошибки, потому что дубль вебхука оплаты — штатная
        ситуация, а не сбой.

        :param payment_id: Оплата, подтвердившая зачёт.
        :param moment: Момент зачёта (timezone-aware).
        :return: ``True``, если состояние изменилось этим вызовом.
        """
        if self.status is not ReferralStatus.PENDING:
            logger.debug("Реферал id=%s уже обработан (%s)", self.id, self.status)
            return False

        self._transition_to(ReferralStatus.QUALIFIED)
        self.payment_id = payment_id
        self.qualified_at = moment
        logger.info("Реферал id=%s зачтён по платежу id=%s", self.id, payment_id)
        return True

    def reward(self, days: int, moment: datetime) -> bool:
        """Фиксирует выданный пригласившему бонус.

        :param days: Начисленные дни (строго положительные).
        :param moment: Момент начисления (timezone-aware).
        :return: ``True``, если бонус зафиксирован этим вызовом.
        :raises ValueError: Некорректное число дней.
        :raises InvalidStateTransitionError: Приглашение ещё не зачтено.
        """
        if days <= 0:
            raise ValueError(f"Бонус должен быть положительным, получено: {days}")
        if self.status is ReferralStatus.REWARDED:
            logger.debug("Реферал id=%s уже вознаграждён", self.id)
            return False

        self._transition_to(ReferralStatus.REWARDED)
        self.bonus_days = days
        self.rewarded_at = moment
        logger.info("Рефереру начислено %d дн. по рефералу id=%s", days, self.id)
        return True

    def reject(self, moment: datetime) -> bool:
        """Отклоняет приглашение (возврат средств, злоупотребление).

        :param moment: Момент отклонения (timezone-aware).
        :return: ``True``, если состояние изменилось этим вызовом.
        :raises InvalidStateTransitionError: Приглашение уже вознаграждено.
        """
        if self.status is ReferralStatus.REJECTED:
            return False

        self._transition_to(ReferralStatus.REJECTED)
        self.qualified_at = self.qualified_at or moment
        logger.info("Реферал id=%s отклонён", self.id)
        return True
