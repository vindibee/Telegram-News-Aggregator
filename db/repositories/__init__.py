"""Репозиторный слой: единственное место, где приложение говорит на SQL."""

from db.repositories.base import BaseRepository, handle_db_errors, run_with_retry
from db.repositories.errors import (
    ConcurrencyError,
    ConflictError,
    EntityNotFoundError,
    RepositoryError,
)
from db.repositories.payment import PaymentCreateResult, PaymentRepository
from db.repositories.post import PostData, PostRepository
from db.repositories.subscription import (
    GrantResult,
    SubscriptionCreateResult,
    SubscriptionRepository,
)
from db.repositories.user import TrialClaimResult, UserRepository, UserUpsertResult

__all__ = [
    "BaseRepository",
    "ConcurrencyError",
    "ConflictError",
    "EntityNotFoundError",
    "GrantResult",
    "PaymentCreateResult",
    "PaymentRepository",
    "PostData",
    "PostRepository",
    "RepositoryError",
    "SubscriptionCreateResult",
    "SubscriptionRepository",
    "TrialClaimResult",
    "UserRepository",
    "UserUpsertResult",
    "handle_db_errors",
    "run_with_retry",
]
