"""Ошибки репозиторного слоя.

Отделены от доменных исключений (:mod:`db.exceptions`): здесь описаны сбои
хранилища, а не нарушения бизнес-правил. Прикладной код ловит эти типы и
не обязан знать про SQLAlchemy.
"""

from __future__ import annotations

from typing import Any, Final

#: SQLSTATE, при которых транзакцию имеет смысл повторить целиком.
SERIALIZATION_FAILURE: Final[str] = "40001"
DEADLOCK_DETECTED: Final[str] = "40P01"
RETRYABLE_SQLSTATES: Final[frozenset[str]] = frozenset(
    {SERIALIZATION_FAILURE, DEADLOCK_DETECTED}
)

#: SQLSTATE нарушений целостности.
UNIQUE_VIOLATION: Final[str] = "23505"
FOREIGN_KEY_VIOLATION: Final[str] = "23503"
CHECK_VIOLATION: Final[str] = "23514"


class RepositoryError(RuntimeError):
    """Базовая ошибка доступа к данным, пригодная для показа пользователю."""


class EntityNotFoundError(RepositoryError):
    """Запись не найдена по идентификатору."""

    def __init__(self, entity: str, identifier: Any) -> None:
        self.entity = entity
        self.identifier = identifier
        super().__init__(f"{entity} с идентификатором {identifier!r} не найден.")


class ConflictError(RepositoryError):
    """Нарушение ограничения целостности (уникальность, FK, CHECK)."""

    def __init__(self, message: str, constraint: str | None = None, sqlstate: str | None = None) -> None:
        self.constraint = constraint
        self.sqlstate = sqlstate
        super().__init__(message)


class ConcurrencyError(RepositoryError):
    """Транзакция отменена из-за взаимоблокировки или ошибки сериализации.

    Такую операцию корректно повторить целиком — см.
    :func:`db.repositories.base.run_with_retry`.
    """


def extract_error_details(exception: BaseException) -> tuple[str | None, str | None]:
    """Достаёт SQLSTATE и имя ограничения из ошибки драйвера.

    SQLAlchemy оборачивает исключение asyncpg в свой класс, а подробности
    остаются в ``__cause__``; без такого разбора отличить нарушение
    уникальности от нарушения CHECK невозможно.

    :param exception: Исключение SQLAlchemy (``exc.orig``) или драйвера.
    :return: Пара ``(sqlstate, constraint_name)``; элементы могут быть ``None``.
    """
    candidate = getattr(exception, "__cause__", None) or exception
    sqlstate = getattr(candidate, "sqlstate", None)
    constraint = getattr(candidate, "constraint_name", None)
    return sqlstate, constraint
