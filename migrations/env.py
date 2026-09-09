"""Окружение Alembic для асинхронного движка SQLAlchemy.

URL подключения берётся из :mod:`core.config`, а не из ``alembic.ini``:
единственный источник правды для настроек — переменные окружения, и
секреты не попадают в репозиторий.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.config import load_settings
from db.base import Base

# Импорт пакета моделей обязателен: без него Base.metadata пуста и
# autogenerate сгенерировал бы миграцию на удаление всех таблиц.
import db.models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    """Возвращает DSN для подключения.

    :return: URL с раскрытым паролем (нужен драйверу, в логи не пишется).
    :raises core.config.ConfigError: Конфигурация неполна или некорректна.
    """
    settings = load_settings()
    return settings.db.url.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    """Генерирует SQL-скрипт без подключения к базе (``alembic upgrade --sql``)."""
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Выполняет миграции в рамках уже открытого соединения.

    :param connection: Синхронный «фасад» асинхронного соединения.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Без этих флагов autogenerate пропускает смену типа колонки и
        # server_default, а расхождение схемы обнаруживается уже в проде.
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Создаёт асинхронный движок и прогоняет миграции."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        # Пул закрывается всегда: иначе процесс миграции зависает на выходе.
        await connectable.dispose()


def run_migrations_online() -> None:
    """Точка входа для обычного режима работы Alembic."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
