"""Инфраструктура доступа к базе данных."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import DatabaseConfig
from core.logger import get_logger
from db.base import Base
from db.models import NewsPost  # noqa: F401  — регистрация модели в metadata

logger = get_logger(__name__)


class Database:
    """Владелец движка и фабрики сессий.

    Объект создаётся один раз на старте приложения и передаётся зависимостям
    явно (DI), а не импортируется как глобальный синглтон: так модули остаются
    тестируемыми и не создают побочных эффектов на импорте.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self._engine: AsyncEngine = create_async_engine(
            config.url,
            echo=config.echo,
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_recycle=config.pool_recycle,
            # Отсеивает «мертвые» соединения после рестарта PostgreSQL.
            pool_pre_ping=True,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Фабрика сессий для middleware и фоновых задач."""
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Сессия с автоматическим откатом транзакции при исключении."""
        async with self._session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        """Создаёт таблицы, которых ещё нет.

        Для продакшена схему следует версионировать через Alembic; этот метод
        оставлен как безопасный bootstrap для локального и docker-compose запуска.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Схема базы данных синхронизирована.")

    async def dispose(self) -> None:
        """Корректно закрывает пул соединений при остановке приложения."""
        await self._engine.dispose()
        logger.info("Пул соединений с базой данных закрыт.")
