"""ORM-модели приложения.

Импорт всех модулей пакета обязателен до первой работы с ORM: SQLAlchemy
разрешает строковые ссылки в ``relationship`` только среди уже
зарегистрированных классов, а Alembic собирает ``Base.metadata`` именно
отсюда.
"""

from db.models.channel import UserChannel
from db.models.keyword import MAX_KEYWORD_LENGTH, UserKeyword
from db.models.payment import Payment
from db.models.post import FTS_CONFIG, SIMHASH_BANDS, Post
from db.models.promo import PROMOCODE_LENGTH, Promocode, PromocodeRedemption
from db.models.referral import Referral
from db.models.schedule import MAX_PUBLISH_ATTEMPTS, ScheduledPost
from db.models.subscription import Subscription, SubscriptionEvent
from db.models.tracking import LINK_TOKEN_LENGTH, ClickLog, TrackedLink
from db.models.user import REFERRAL_CODE_LENGTH, TrialClaim, User

__all__ = [
    "FTS_CONFIG",
    "LINK_TOKEN_LENGTH",
    "MAX_KEYWORD_LENGTH",
    "MAX_PUBLISH_ATTEMPTS",
    "PROMOCODE_LENGTH",
    "REFERRAL_CODE_LENGTH",
    "SIMHASH_BANDS",
    "ClickLog",
    "Payment",
    "Post",
    "Promocode",
    "PromocodeRedemption",
    "Referral",
    "ScheduledPost",
    "Subscription",
    "SubscriptionEvent",
    "TrackedLink",
    "TrialClaim",
    "User",
    "UserChannel",
    "UserKeyword",
]
