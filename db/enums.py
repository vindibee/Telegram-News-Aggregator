"""Перечисления доменной модели.

Все enum-ы наследуют :class:`enum.StrEnum`: в Python они ведут себя как
строки (удобно в логах и JSON), а в PostgreSQL создаются как нативные типы
через ``values_callable`` — в БД пишется *значение* (``"pro"``), а не имя
члена (``"PRO"``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy import Enum as SAEnum


class SubscriptionPlan(StrEnum):
    """Тарифный план.

    Цены и лимиты намеренно не хранятся в БД: они меняются чаще, чем схема,
    и живут в конфигурации. При переходе на несколько валют и историю цен
    план выносится в отдельную таблицу ``plans`` с FK отсюда.
    """

    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"


class SubscriptionStatus(StrEnum):
    """Состояние подписки."""

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SubscriptionSource(StrEnum):
    """Источник появления или продления подписки."""

    TRIAL = "trial"
    PAYMENT = "payment"
    REFERRAL = "referral"
    PROMO = "promo"
    MANUAL = "manual"


class SubscriptionEventKind(StrEnum):
    """Тип записи в журнале подписки."""

    CREATED = "created"
    EXTENDED = "extended"
    DOWNGRADED = "downgraded"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    EXPIRY_NOTIFIED = "expiry_notified"


class PaymentProvider(StrEnum):
    """Поддерживаемые платёжные провайдеры."""

    TELEGRAM_STARS = "telegram_stars"
    CRYPTO_BOT = "crypto_bot"


class PaymentStatus(StrEnum):
    """Состояние платежа.

    ``PROCESSING`` соответствует моменту между ``PreCheckoutQuery`` и
    ``SuccessfulPayment``: счёт подтверждён ботом, но деньги ещё не списаны.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class TrialFingerprintKind(StrEnum):
    """Признак, по которому определяется повторная активация триала."""

    PHONE = "phone"
    IP = "ip"
    DEVICE = "device"


class PostStatus(StrEnum):
    """Состояние новости в конвейере обработки."""

    NEW = "new"
    PUBLISHED = "published"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Создаёт нативный PostgreSQL ENUM для указанного класса.

    :param enum_cls: Класс перечисления.
    :param name: Имя типа в базе данных.
    :return: Готовый к использованию тип SQLAlchemy.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        # В БД сохраняем значения ("pro"), а не имена членов ("PRO"):
        # значения стабильны при переименовании атрибутов в коде.
        values_callable=lambda members: [member.value for member in members],
    )


#: Терминальные состояния платежа — из них переходы запрещены.
FINAL_PAYMENT_STATUSES: Final[frozenset[PaymentStatus]] = frozenset(
    {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.REFUNDED, PaymentStatus.EXPIRED}
)

#: Статусы, при которых подписка считается действующей.
LIVE_SUBSCRIPTION_STATUSES: Final[frozenset[SubscriptionStatus]] = frozenset(
    {SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE}
)
