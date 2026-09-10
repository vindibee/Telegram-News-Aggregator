"""Репозиторный слой: единственное место, где приложение говорит на SQL."""

from db.repositories.base import BaseRepository, handle_db_errors, run_with_retry
from db.repositories.channel import ChannelCreateResult, ChannelRepository
from db.repositories.keyword import KeywordAddResult, KeywordRepository
from db.repositories.errors import (
    ConcurrencyError,
    ConflictError,
    EntityNotFoundError,
    RepositoryError,
)
from db.repositories.payment import PaymentCreateResult, PaymentRepository
from db.repositories.promo import PromocodeRepository, PromocodeTotals
from db.repositories.referral import (
    ReferralRepository,
    ReferralStats,
    ReferralTotals,
)
from db.repositories.post import PostData, PostRepository
from db.repositories.schedule import ScheduledPostRepository
from db.repositories.tracking import (
    ClickEvent,
    LinkStats,
    OwnerTotals,
    TrackedLinkRepository,
)
from db.repositories.subscription import (
    GrantResult,
    SubscriptionCreateResult,
    SubscriptionRepository,
)
from db.repositories.user import TrialClaimResult, UserRepository, UserUpsertResult

__all__ = [
    "BaseRepository",
    "ClickEvent",
    "LinkStats",
    "OwnerTotals",
    "TrackedLinkRepository",
    "ChannelCreateResult",
    "ChannelRepository",
    "ConcurrencyError",
    "ConflictError",
    "EntityNotFoundError",
    "KeywordAddResult",
    "KeywordRepository",
    "GrantResult",
    "PaymentCreateResult",
    "PaymentRepository",
    "PostData",
    "PostRepository",
    "PromocodeRepository",
    "PromocodeTotals",
    "ReferralRepository",
    "ReferralStats",
    "ReferralTotals",
    "RepositoryError",
    "ScheduledPostRepository",
    "SubscriptionCreateResult",
    "SubscriptionRepository",
    "TrialClaimResult",
    "UserRepository",
    "UserUpsertResult",
    "handle_db_errors",
    "run_with_retry",
]
