"""Ошибки пробного периода.

Каждое исключение несёт текст, пригодный для показа пользователю: слой
хендлеров не должен знать причины отказа и подбирать формулировки — иначе
правила выдачи триала оказались бы размазаны между сервисом и Telegram.
"""

from __future__ import annotations

from db.enums import TrialFingerprintKind


class TrialError(Exception):
    """Базовая ошибка выдачи пробного периода.

    Несёт ключ перевода: язык пользователя известен слою хендлеров, а не
    сервису. Текст исключения остаётся для логов.
    """

    #: Ключ строки в каталоге переводов.
    key: str = "common.error"


class TrialDisabledError(TrialError):
    """Пробный период отключён настройками."""

    key = "trial.errors.disabled"

    def __init__(self) -> None:
        super().__init__("Пробный период сейчас недоступен.")


class TrialAlreadyClaimedError(TrialError):
    """Пользователь уже активировал пробный период."""

    key = "trial.errors.already_claimed"

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__("Пробный период уже был активирован — он выдаётся один раз.")


class SubscriptionAlreadyActiveError(TrialError):
    """У пользователя уже есть действующая подписка."""

    key = "trial.errors.subscription_active"

    def __init__(self) -> None:
        super().__init__("У вас уже есть действующая подписка — пробный период не нужен.")


class ContactRequiredError(TrialError):
    """Для выдачи триала требуется подтверждённый номер телефона."""

    key = "trial.errors.contact_required"

    def __init__(self) -> None:
        super().__init__("Чтобы активировать пробный период, подтвердите номер телефона.")


class ForeignContactError(TrialError):
    """Прислан чужой контакт, а не собственный номер.

    Ключевая защита сценария: кнопка «Поделиться номером» присылает
    контакт с ``user_id`` владельца, а вручную выбранный из адресной книги
    контакт — с чужим либо вовсе без него. Без этой проверки один человек
    активировал бы триал по номерам всех своих знакомых.
    """

    key = "trial.errors.foreign_contact"

    def __init__(self) -> None:
        super().__init__(
            "Это чужой контакт. Нажмите кнопку «Поделиться номером» — "
            "переслать номер из адресной книги нельзя."
        )


class TrialFingerprintTakenError(TrialError):
    """Признак уже использован для получения триала другим аккаунтом."""

    key = "trial.errors.fingerprint_other"

    #: Ключ перевода зависит от типа признака: «этот номер» и «это
    #: устройство» требуют разного согласования в любом языке.
    _KEYS = {
        TrialFingerprintKind.PHONE: "trial.errors.fingerprint_phone",
        TrialFingerprintKind.IP: "trial.errors.fingerprint_ip",
        TrialFingerprintKind.DEVICE: "trial.errors.fingerprint_device",
    }

    def __init__(self, kind: TrialFingerprintKind | None = None) -> None:
        self.kind = kind
        self.key = self._KEYS.get(kind, "trial.errors.fingerprint_other")
        super().__init__(f"Признак {kind} уже использовался для пробного периода.")
