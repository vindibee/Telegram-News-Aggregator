"""Слой доступа к данным: модели, репозитории и инфраструктура подключения."""

from db.base import Base
from db.database import Database
from db.enums import (
    PaymentProvider,
    PaymentStatus,
    PostStatus,
    SubscriptionEventKind,
    SubscriptionPlan,
    SubscriptionSource,
    SubscriptionStatus,
    TrialFingerprintKind,
)
from db.exceptions import (
    DomainError,
    InvalidPeriodError,
    InvalidStateTransitionError,
    TrialAlreadyUsedError,
)
from db.locks import LockNamespace
from db.models import Payment, Post, Subscription, SubscriptionEvent, TrialClaim, User
from db.repositories import (
    ConcurrencyError,
    ConflictError,
    EntityNotFoundError,
    GrantResult,
    PaymentRepository,
    PostData,
    PostRepository,
    RepositoryError,
    SubscriptionRepository,
    UserRepository,
)
from db.uow import UnitOfWork, UnitOfWorkFactory

__all__ = [
    "Base",
    "ConcurrencyError",
    "ConflictError",
    "Database",
    "DomainError",
    "EntityNotFoundError",
    "GrantResult",
    "InvalidPeriodError",
    "InvalidStateTransitionError",
    "LockNamespace",
    "Payment",
    "PaymentProvider",
    "PaymentRepository",
    "PaymentStatus",
    "Post",
    "PostData",
    "PostRepository",
    "PostStatus",
    "RepositoryError",
    "Subscription",
    "SubscriptionEvent",
    "SubscriptionEventKind",
    "SubscriptionPlan",
    "SubscriptionRepository",
    "SubscriptionSource",
    "SubscriptionStatus",
    "TrialAlreadyUsedError",
    "TrialClaim",
    "TrialFingerprintKind",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "User",
    "UserRepository",
]
