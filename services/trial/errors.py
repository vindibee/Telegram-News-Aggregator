"""Ошибки пробного периода.

Каждое исключение несёт текст, пригодный для показа пользователю: слой
хендлеров не должен знать причины отказа и подбирать формулировки — иначе
правила выдачи триала оказались бы размазаны между сервисом и Telegram.
"""

from __future__ import annotations

from db.enums import TrialFingerprintKind


class TrialError(Exception):
    """Базовая ошибка выдачи пробного периода."""


class TrialDisabledError(TrialError):
    """Пробный период отключён настройками."""

    def __init__(self) -> None:
        super().__init__("Пробный период сейчас недоступен.")


class TrialAlreadyClaimedError(TrialError):
    """Пользователь уже активировал пробный период."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__("Пробный период уже был активирован — он выдаётся один раз.")


class SubscriptionAlreadyActiveError(TrialError):
    """У пользователя уже есть действующая подписка."""

    def __init__(self) -> None:
        super().__init__("У вас уже есть действующая подписка — пробный период не нужен.")


class ContactRequiredError(TrialError):
    """Для выдачи триала требуется подтверждённый номер телефона."""

    def __init__(self) -> None:
        super().__init__("Чтобы активировать пробный период, подтвердите номер телефона.")


class ForeignContactError(TrialError):
    """Прислан чужой контакт, а не собственный номер.

    Ключевая защита сценария: кнопка «Поделиться номером» присылает
    контакт с ``user_id`` владельца, а вручную выбранный из адресной книги
    контакт — с чужим либо вовсе без него. Без этой проверки один человек
    активировал бы триал по номерам всех своих знакомых.
    """

    def __init__(self) -> None:
        super().__init__(
            "Это чужой контакт. Нажмите кнопку «Поделиться номером» — "
            "переслать номер из адресной книги нельзя."
        )


class TrialFingerprintTakenError(TrialError):
    """Признак уже использован для получения триала другим аккаунтом."""

    #: Человекочитаемые названия признаков для текста отказа.
    _LABELS = {
        TrialFingerprintKind.PHONE: "Этот номер телефона",
        TrialFingerprintKind.IP: "Этот адрес",
        TrialFingerprintKind.DEVICE: "Это устройство",
    }

    def __init__(self, kind: TrialFingerprintKind | None = None) -> None:
        self.kind = kind
        subject = self._LABELS.get(kind, "Этот признак") if kind else "Этот признак"
        super().__init__(f"{subject} уже использовался для пробного периода.")
