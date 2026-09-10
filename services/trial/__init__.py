"""Пробный период и защита от мультиаккаунтов."""

from services.trial.errors import (
    ContactRequiredError,
    ForeignContactError,
    SubscriptionAlreadyActiveError,
    TrialAlreadyClaimedError,
    TrialDisabledError,
    TrialError,
    TrialFingerprintTakenError,
)
from services.trial.service import TrialEligibility, TrialOutcome, TrialService

__all__ = [
    "ContactRequiredError",
    "ForeignContactError",
    "SubscriptionAlreadyActiveError",
    "TrialAlreadyClaimedError",
    "TrialDisabledError",
    "TrialEligibility",
    "TrialError",
    "TrialFingerprintTakenError",
    "TrialOutcome",
    "TrialService",
]
