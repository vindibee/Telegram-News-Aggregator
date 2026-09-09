"""ORM-модели приложения.

Импорт всех модулей пакета обязателен до первой работы с ORM: SQLAlchemy
разрешает строковые ссылки в ``relationship`` только среди уже
зарегистрированных классов, а Alembic собирает ``Base.metadata`` именно
отсюда.
"""

from db.models.payment import Payment
from db.models.post import FTS_CONFIG, SIMHASH_BANDS, Post
from db.models.subscription import Subscription, SubscriptionEvent
from db.models.user import REFERRAL_CODE_LENGTH, TrialClaim, User

__all__ = [
    "FTS_CONFIG",
    "REFERRAL_CODE_LENGTH",
    "SIMHASH_BANDS",
    "Payment",
    "Post",
    "Subscription",
    "SubscriptionEvent",
    "TrialClaim",
    "User",
]
