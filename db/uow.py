"""Unit of Work — граница транзакции для прикладных операций.

Репозитории намеренно не вызывают ``commit``: одна бизнес-операция почти
всегда затрагивает несколько таблиц (платёж, подписка, журнал событий), и
если бы каждый репозиторий фиксировал свои изменения сам, атомарность
терялась бы — сбой на третьем шаге оставил бы систему в состоянии
«деньги списаны, дни не начислены».

Здесь же собран повтор транзакции при отмене со стороны сервера: повторять
можно только операцию целиком, поэтому точка повтора совпадает с границей
транзакции.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.logger import get_logger
from db.locks import LockNamespace, acquire_xact_lock, try_acquire_xact_lock
from db.repositories.base import DEFAULT_RETRY_ATTEMPTS, run_with_retry
from db.repositories.channel import ChannelRepository
from db.repositories.keyword import KeywordRepository
from db.repositories.payment import PaymentRepository
from db.repositories.post import PostRepository
from db.repositories.subscription import SubscriptionRepository
from db.repositories.user import UserRepository

logger = get_logger(__name__)

R = TypeVar("R")


class UnitOfWork:
    """Сессия БД вместе с набором репозиториев.

    Использование::

        async with UnitOfWork(session_factory) as uow:
            result = await uow.subscriptions.apply_payment_grant(...)
            await uow.commit()

    Выход из блока без ``commit`` откатывает транзакцию. Это осознанно:
    забытая фиксация не должна приводить к частичной записи, поэтому
    поведение по умолчанию — откат.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

        self._users: UserRepository | None = None
        self._subscriptions: SubscriptionRepository | None = None
        self._payments: PaymentRepository | None = None
        self._posts: PostRepository | None = None
        self._channels: ChannelRepository | None = None
        self._keywords: KeywordRepository | None = None

    # ------------------------------------------------------------- контекст
    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self._committed = False
        self._users = UserRepository(self._session)
        self._subscriptions = SubscriptionRepository(self._session)
        self._payments = PaymentRepository(self._session)
        self._posts = PostRepository(self._session)
        self._channels = ChannelRepository(self._session)
        self._keywords = KeywordRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None:
                logger.warning("Транзакция откатывается из-за исключения %s", exc_type.__name__)
                await session.rollback()
            elif not self._committed and self._has_pending_changes(session):
                # Не ошибка, а защита: незафиксированные изменения не должны
                # «просачиваться» в БД по случайности.
                logger.debug("Выход из UnitOfWork без commit — выполняю откат")
                await session.rollback()
            # Для блока без изменений явный rollback не нужен: close() и так
            # закрывает транзакцию, но, в отличие от rollback(), не обесценивает
            # прочитанные объекты. Иначе обращение к полю сущности после выхода
            # из блока падало бы с DetachedInstanceError.
        finally:
            await session.close()
            self._session = None
            self._users = None
            self._subscriptions = None
            self._payments = None
            self._posts = None
            self._channels = None
            self._keywords = None

    @staticmethod
    def _has_pending_changes(session: AsyncSession) -> bool:
        """Есть ли в сессии несохранённые изменения ORM-объектов."""
        return bool(session.new or session.dirty or session.deleted)

    # ---------------------------------------------------------- репозитории
    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork используется вне контекстного менеджера.")
        return self._session

    @property
    def session(self) -> AsyncSession:
        """Активная сессия."""
        return self._require_session()

    @property
    def users(self) -> UserRepository:
        """Репозиторий пользователей."""
        self._require_session()
        assert self._users is not None  # noqa: S101 - гарантируется __aenter__
        return self._users

    @property
    def subscriptions(self) -> SubscriptionRepository:
        """Репозиторий подписок."""
        self._require_session()
        assert self._subscriptions is not None  # noqa: S101
        return self._subscriptions

    @property
    def payments(self) -> PaymentRepository:
        """Репозиторий платежей."""
        self._require_session()
        assert self._payments is not None  # noqa: S101
        return self._payments

    @property
    def posts(self) -> PostRepository:
        """Репозиторий постов."""
        self._require_session()
        assert self._posts is not None  # noqa: S101
        return self._posts

    @property
    def channels(self) -> ChannelRepository:
        """Репозиторий каналов пользователя."""
        self._require_session()
        assert self._channels is not None  # noqa: S101
        return self._channels

    @property
    def keywords(self) -> KeywordRepository:
        """Репозиторий пользовательских фильтров."""
        self._require_session()
        assert self._keywords is not None  # noqa: S101
        return self._keywords

    # ------------------------------------------------------------ управление
    async def commit(self) -> None:
        """Фиксирует транзакцию."""
        await self._require_session().commit()
        self._committed = True

    async def rollback(self) -> None:
        """Откатывает транзакцию."""
        await self._require_session().rollback()
        self._committed = False

    async def flush(self) -> None:
        """Отправляет накопленные изменения в БД, не фиксируя транзакцию."""
        await self._require_session().flush()

    async def lock(self, namespace: LockNamespace, value: str | int) -> None:
        """Берёт advisory-блокировку до конца транзакции."""
        await acquire_xact_lock(self._require_session(), namespace, value)

    async def try_lock(self, namespace: LockNamespace, value: str | int) -> bool:
        """Пытается взять advisory-блокировку без ожидания."""
        return await try_acquire_xact_lock(self._require_session(), namespace, value)


class UnitOfWorkFactory:
    """Фабрика единиц работы.

    Передаётся в сервисы вместо готовой сессии: сервис сам решает, где
    начинается и заканчивается транзакция, а фоновая задача может открыть
    столько независимых транзакций, сколько ей нужно.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> UnitOfWork:
        """Создаёт новую единицу работы."""
        return UnitOfWork(self._session_factory)

    async def transaction(
        self,
        operation: Callable[[UnitOfWork], Awaitable[R]],
        *,
        attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> R:
        """Выполняет операцию в транзакции, повторяя её при конфликте.

        Фиксация выполняется автоматически при успешном завершении
        ``operation``; при исключении транзакция откатывается.

        :param operation: Функция, принимающая :class:`UnitOfWork`.
        :param attempts: Число попыток при взаимоблокировке или ошибке
            сериализации.
        :return: Результат операции.
        :raises db.repositories.errors.ConcurrencyError: Попытки исчерпаны.
        """

        async def run_once() -> R:
            async with UnitOfWork(self._session_factory) as uow:
                result = await operation(uow)
                await uow.commit()
                return result

        return await run_with_retry(run_once, attempts=attempts)
