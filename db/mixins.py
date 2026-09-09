"""Переиспользуемые фрагменты моделей (DRY).

Миксины не наследуют :class:`db.base.Base` — иначе SQLAlchemy попытался бы
отобразить их на собственные таблицы.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class IdMixin:
    """Суррогатный первичный ключ.

    ``BIGINT`` вместо ``INTEGER``: таблицы постов и событий в SaaS растут
    быстрее, чем кажется на старте, а миграция типа PK под нагрузкой дорога.
    ``Identity`` — стандарт SQL вместо устаревшего ``SERIAL``.
    """

    @declared_attr.directive
    def id(cls) -> Mapped[int]:  # noqa: N805 - declared_attr требует cls
        return mapped_column(BigInteger, Identity(always=False), primary_key=True)


class TimestampMixin:
    """Отметки времени создания и изменения записи.

    Значения проставляет сервер БД: это единый источник времени независимо
    от часов на машинах воркеров и веб-процессов.
    """

    @declared_attr.directive
    def created_at(cls) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            nullable=False,
        )

    @declared_attr.directive
    def updated_at(cls) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )
