"""Базовый репозиторий и общие механизмы работы с БД."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from typing import Any, ClassVar, Generic, ParamSpec, TypeVar

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from db.base import Base
from db.repositories.errors import (
    RETRYABLE_SQLSTATES,
    ConcurrencyError,
    ConflictError,
    EntityNotFoundError,
    RepositoryError,
    extract_error_details,
)

logger = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=Base)
P = ParamSpec("P")
R = TypeVar("R")

#: Параметры повторов для транзакций, отменённых сервером.
DEFAULT_RETRY_ATTEMPTS = 3
_BASE_RETRY_DELAY = 0.05
_MAX_RETRY_DELAY = 1.0


def handle_db_errors(func_: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Переводит исключения SQLAlchemy в ошибки репозиторного слоя.

    Декоратор вместо ``try/except`` в каждом методе: обработка одинакова
    везде, а дублирование её тридцать раз нарушало бы DRY и неизбежно
    разъехалось бы.

    :param func_: Асинхронный метод репозитория.
    :return: Метод с единообразной трансляцией ошибок.
    """

    @wraps(func_)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func_(*args, **kwargs)
        except IntegrityError as exc:
            sqlstate, constraint = extract_error_details(exc.orig or exc)
            logger.warning(
                "Нарушение целостности в %s: sqlstate=%s constraint=%s",
                func_.__qualname__, sqlstate, constraint,
            )
            raise ConflictError(
                "Операция нарушает ограничение целостности данных.",
                constraint=constraint,
                sqlstate=sqlstate,
            ) from exc
        except DBAPIError as exc:
            sqlstate, _ = extract_error_details(exc.orig or exc)
            if sqlstate in RETRYABLE_SQLSTATES:
                logger.warning(
                    "Транзакция отменена сервером в %s: sqlstate=%s", func_.__qualname__, sqlstate
                )
                raise ConcurrencyError(
                    "Конфликт параллельных транзакций, повторите операцию."
                ) from exc
            logger.exception("Ошибка драйвера БД в %s", func_.__qualname__)
            raise RepositoryError("Ошибка обращения к базе данных.") from exc
        except SQLAlchemyError as exc:
            logger.exception("Ошибка SQLAlchemy в %s", func_.__qualname__)
            raise RepositoryError("Ошибка обращения к базе данных.") from exc

    return wrapper


async def run_with_retry(
    operation: Callable[[], Awaitable[R]],
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> R:
    """Повторяет операцию при взаимоблокировке или ошибке сериализации.

    Повторять можно только транзакцию целиком: после отмены на сервере
    её состояние потеряно, и «дорезать» остаток нельзя.

    :param operation: Фабрика корутины — вызывается заново на каждой попытке.
    :param attempts: Максимальное число попыток (не меньше 1).
    :return: Результат успешной попытки.
    :raises ConcurrencyError: Попытки исчерпаны.
    :raises ValueError: Некорректное число попыток.
    """
    if attempts < 1:
        raise ValueError(f"Число попыток должно быть не меньше 1, получено: {attempts}")

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except ConcurrencyError:
            if attempt == attempts:
                logger.error("Операция отменена: исчерпаны %d попыток", attempts)
                raise
            # Экспоненциальная задержка со случайным разбросом: без разброса
            # конкурирующие транзакции повторяются синхронно и снова конфликтуют.
            delay = min(_BASE_RETRY_DELAY * 2 ** (attempt - 1), _MAX_RETRY_DELAY)
            delay *= 0.5 + random.random()
            logger.info("Повтор транзакции через %.3f с (попытка %d из %d)", delay, attempt, attempts)
            await asyncio.sleep(delay)

    raise ConcurrencyError("Не удалось выполнить операцию за отведённое число попыток.")


class BaseRepository(Generic[ModelT]):
    """Общая часть всех репозиториев.

    Наследники обязаны объявить :attr:`model`. Репозиторий *не* управляет
    транзакцией: фиксацией занимается :class:`db.uow.UnitOfWork`, иначе
    одна прикладная операция распадалась бы на несколько независимых
    транзакций и потеряла атомарность.
    """

    model: ClassVar[type[Base]]

    def __init__(self, session: AsyncSession) -> None:
        if not hasattr(self, "model"):
            raise TypeError(f"{type(self).__name__} должен объявить атрибут model.")
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """Сессия, в которой работает репозиторий."""
        return self._session

    @handle_db_errors
    async def get(self, entity_id: Any) -> ModelT | None:
        """Возвращает запись по первичному ключу или ``None``."""
        return await self._session.get(self.model, entity_id)

    @handle_db_errors
    async def get_or_fail(self, entity_id: Any) -> ModelT:
        """Возвращает запись по первичному ключу.

        :raises EntityNotFoundError: Записи с таким ключом нет.
        """
        entity = await self._session.get(self.model, entity_id)
        if entity is None:
            raise EntityNotFoundError(self.model.__name__, entity_id)
        return entity

    @handle_db_errors
    async def get_for_update(self, entity_id: Any, *, skip_locked: bool = False) -> ModelT | None:
        """Читает запись, блокируя её до конца транзакции.

        Обязательна перед любым «прочитать — изменить — записать»: без
        блокировки два параллельных обработчика прочитают одно значение и
        второй перезапишет результат первого.

        :param entity_id: Первичный ключ.
        :param skip_locked: Не ждать освобождения, а вернуть ``None``.
        :return: Заблокированная запись или ``None``.
        """
        stmt = (
            select(self.model)
            .where(self.model.id == entity_id)
            .with_for_update(skip_locked=skip_locked)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    @handle_db_errors
    async def add(self, entity: ModelT) -> ModelT:
        """Помещает запись в сессию и синхронизирует её с БД.

        ``flush`` без ``commit``: идентификатор становится доступен сразу,
        но транзакция остаётся открытой для остальных шагов операции.
        """
        self._session.add(entity)
        await self._session.flush()
        return entity

    @handle_db_errors
    async def delete(self, entity: ModelT) -> None:
        """Удаляет запись."""
        await self._session.delete(entity)
        await self._session.flush()

    @handle_db_errors
    async def exists(self, *conditions: ColumnElement[bool]) -> bool:
        """Проверяет наличие хотя бы одной подходящей записи."""
        stmt = select(select(self.model.id).where(*conditions).exists())
        return bool(await self._session.scalar(stmt))

    @handle_db_errors
    async def count(self, *conditions: ColumnElement[bool]) -> int:
        """Считает записи, удовлетворяющие условиям."""
        stmt = select(func.count()).select_from(self.model)
        if conditions:
            stmt = stmt.where(*conditions)
        return int(await self._session.scalar(stmt) or 0)

    @handle_db_errors
    async def _fetch_all(self, stmt: Select[tuple[ModelT]]) -> Sequence[ModelT]:
        """Выполняет запрос и возвращает список сущностей."""
        result = await self._session.execute(stmt)
        return result.scalars().all()

    @handle_db_errors
    async def _fetch_one(self, stmt: Select[tuple[ModelT]]) -> ModelT | None:
        """Выполняет запрос и возвращает одну сущность или ``None``."""
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
