"""Инфраструктура доступа к базе данных."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import DatabaseConfig
from core.logger import get_logger
from db.base import Base
import db.models  # noqa: F401  — регистрирует все модели в Base.metadata

logger = get_logger(__name__)


class DatabaseNotReadyError(RuntimeError):
    """База недоступна или её схема не приведена к актуальной версии."""


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

    async def check_ready(self) -> None:
        """Проверяет доступность базы и наличие применённой схемы.

        Пул соединений создаётся лениво, поэтому без явной проверки
        приложение стартует «успешно» при неверном хосте или ненакаченных
        миграциях, а падает только на первом обращении пользователя —
        причём в виде общей ошибки в чате, а не сообщения в логе запуска.

        :raises DatabaseNotReadyError: База недоступна либо схема не создана.
        """
        expected = set(Base.metadata.tables)
        try:
            async with self._engine.connect() as connection:
                present = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = current_schema()"
                            )
                        )
                    ).scalars()
                )
        except (SQLAlchemyError, OSError, asyncio.TimeoutError) as exc:
            # OSError ловится наравне с ошибками SQLAlchemy: неизвестное имя
            # хоста и отказ в соединении приходят от сокета напрямую, минуя
            # обёртки драйвера, и без этого превращались бы в «Фатальная
            # ошибка: getaddrinfo failed» без единого намёка на причину.
            raise DatabaseNotReadyError(
                f"Не удалось подключиться к базе данных ({exc.__class__.__name__}: {exc}). "
                "Проверьте DB_HOST, DB_PORT и то, что PostgreSQL запущен."
            ) from exc

        missing = expected - present
        if missing:
            # Сверяются все таблицы, а не одна «контрольная»: незавершённая
            # миграция оставляет часть схемы на месте, и проверка по одной
            # таблице такую ситуацию пропустит.
            raise DatabaseNotReadyError(
                f"Схема базы данных неполна, отсутствуют таблицы: {', '.join(sorted(missing))}. "
                "Выполните 'alembic upgrade head'."
            )

        logger.info("База данных доступна, схема на месте.")

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
