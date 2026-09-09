"""Модели пользователя и защиты пробного периода."""

from __future__ import annotations

import hashlib
import hmac
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
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import TrialFingerprintKind, pg_enum
from db.exceptions import TrialAlreadyUsedError
from db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from db.models.payment import Payment
    from db.models.subscription import Subscription

logger = get_logger(__name__)

#: Длина реферального кода в символах.
REFERRAL_CODE_LENGTH: Final[int] = 10

#: Алфавит без визуально неоднозначных символов (0/O, 1/l/I).
_CODE_ALPHABET: Final[str] = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class User(Base, IdMixin, TimestampMixin):
    """Пользователь бота.

    ``telegram_id`` — естественный ключ из Telegram, но первичным ключом
    остаётся суррогатный ``id``: внешние идентификаторы не должны
    просачиваться во все FK системы.
    """

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(8), nullable=True)

    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Выставляется, когда Telegram возвращает 403 при рассылке: такие
    # пользователи исключаются из Broadcast Engine без повторных попыток.
    is_bot_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    referral_code: Mapped[str] = mapped_column(String(16), nullable=False)
    referred_by_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    trial_activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    referrer: Mapped[User | None] = relationship(
        "User",
        remote_side="User.id",
        back_populates="referrals",
        lazy="raise",
    )
    referrals: Mapped[list[User]] = relationship(
        "User",
        back_populates="referrer",
        lazy="raise",
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        "Subscription",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    payments: Mapped[list[Payment]] = relationship(
        "Payment",
        back_populates="user",
        lazy="raise",
    )
    trial_claims: Mapped[list[TrialClaim]] = relationship(
        "TrialClaim",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
        UniqueConstraint("referral_code", name="uq_users_referral_code"),
        # Пользователь не может пригласить сам себя.
        CheckConstraint("referred_by_id IS NULL OR referred_by_id <> id", name="self_referral"),
        Index("ix_users_referred_by_id", "referred_by_id"),
        # Частичный индекс: рассылки всегда идут по незаблокировавшим бота.
        Index(
            "ix_users_broadcastable",
            "id",
            postgresql_where=text("NOT is_bot_blocked AND NOT is_banned"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<User id={self.id} telegram_id={self.telegram_id} username={self.username!r}>"

    @property
    def full_name(self) -> str:
        """Отображаемое имя пользователя."""
        parts = [part for part in (self.first_name, self.last_name) if part]
        return " ".join(parts) if parts else (self.username or f"id{self.telegram_id}")

    @property
    def has_used_trial(self) -> bool:
        """Активировал ли пользователь пробный период."""
        return self.trial_activated_at is not None

    def can_start_trial(self) -> bool:
        """Доступен ли пользователю пробный период.

        Проверяет только состояние самого пользователя. Защита от
        мультиаккаунтов реализуется отдельно через :class:`TrialClaim`.
        """
        return not self.has_used_trial and not self.is_banned

    def mark_trial_started(self, moment: datetime) -> None:
        """Фиксирует активацию пробного периода.

        :param moment: Момент активации (timezone-aware).
        :raises TrialAlreadyUsedError: Пробный период уже был использован.
        """
        if self.has_used_trial:
            logger.warning(
                "Повторная активация триала отклонена: user_id=%s, первая активация %s",
                self.id,
                self.trial_activated_at,
            )
            raise TrialAlreadyUsedError(self.id)

        self.trial_activated_at = moment
        logger.info("Пробный период активирован: user_id=%s, telegram_id=%s", self.id, self.telegram_id)

    @staticmethod
    def generate_referral_code(length: int = REFERRAL_CODE_LENGTH) -> str:
        """Генерирует криптостойкий реферальный код.

        Используется :mod:`secrets`, а не :mod:`random`: код является частью
        публичной ссылки, и предсказуемость позволила бы угонять рефералов.

        :param length: Длина кода (не меньше 6 символов).
        :return: Код из символов без визуально неоднозначных знаков.
        :raises ValueError: Запрошена слишком малая длина.
        """
        if length < 6:
            raise ValueError(f"Длина реферального кода должна быть не меньше 6, получено: {length}")
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


class TrialClaim(Base, IdMixin):
    """Отпечаток, по которому пробный период выдаётся только один раз.

    Хранятся исключительно HMAC-хэши: телефон и IP-адрес не попадают в базу
    в открытом виде, поэтому утечка дампа не раскрывает персональные данные.
    Уникальность пары ``(kind, fingerprint)`` физически не даёт получить
    второй триал с того же телефона или адреса.
    """

    __tablename__ = "trial_claims"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[TrialFingerprintKind] = mapped_column(
        pg_enum(TrialFingerprintKind, "trial_fingerprint_kind"),
        nullable=False,
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship("User", back_populates="trial_claims", lazy="raise")

    __table_args__ = (
        UniqueConstraint("kind", "fingerprint", name="uq_trial_claims_kind_fingerprint"),
        Index("ix_trial_claims_user_id", "user_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<TrialClaim id={self.id} user_id={self.user_id} kind={self.kind}>"

    @staticmethod
    def build_fingerprint(kind: TrialFingerprintKind, raw_value: str, secret: str) -> str:
        """Вычисляет отпечаток значения.

        HMAC, а не «голый» SHA-256: без секрета множество телефонных номеров
        и IPv4-адресов перебирается целиком, и обычный хэш не защищает.

        :param kind: Тип признака (влияет на нормализацию).
        :param raw_value: Исходное значение (телефон, IP, идентификатор устройства).
        :param secret: Серверный секрет (из конфигурации, не из репозитория).
        :return: Отпечаток в виде 64 hex-символов.
        :raises ValueError: Пустое значение или пустой секрет.
        """
        normalized = raw_value.strip().lower()
        if kind is TrialFingerprintKind.PHONE:
            # Телефон может прийти как "+7 (900) 123-45-67" — оставляем только цифры.
            normalized = "".join(char for char in normalized if char.isdigit())

        if not normalized:
            raise ValueError(f"Пустое значение отпечатка для типа {kind}.")
        if not secret:
            raise ValueError("Секрет для вычисления отпечатка не задан.")

        digest = hmac.new(
            key=secret.encode("utf-8"),
            msg=f"{kind.value}:{normalized}".encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        logger.debug("Вычислен отпечаток триала: kind=%s", kind)
        return digest
