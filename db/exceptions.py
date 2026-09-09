"""Доменные исключения слоя данных.

Отделены от ошибок инфраструктуры (:class:`db.repositories.errors.RepositoryError`):
эти исключения означают нарушение бизнес-правила, а не сбой БД.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Базовое доменное исключение."""


class InvalidStateTransitionError(DomainError):
    """Попытка недопустимого перехода в конечном автомате состояний."""

    def __init__(self, entity: str, current: Any, target: Any) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(
            f"{entity}: переход {current} -> {target} запрещён правилами домена."
        )


class TrialAlreadyUsedError(DomainError):
    """Пробный период уже был активирован этим пользователем."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(f"Пробный период уже активирован пользователем id={user_id}.")


class InvalidPeriodError(DomainError):
    """Некорректная длительность периода подписки."""

    def __init__(self, days: int) -> None:
        self.days = days
        super().__init__(f"Длительность периода должна быть положительной, получено: {days}.")
