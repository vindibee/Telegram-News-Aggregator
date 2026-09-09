"""Репозиторий новостных постов.

Инкапсулирует все SQL-запросы: выше по стеку (сервисы, хендлеры) не должно
быть ни одного обращения к SQLAlchemy напрямую.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from db.models import NewsPost

logger = get_logger(__name__)


class RepositoryError(RuntimeError):
    """Ошибка уровня хранилища, пригодная для показа пользователю."""


class NewsPostData(TypedDict):
    """Набор полей для вставки одного поста."""

    channel_name: str
    message_id: int
    post_time: datetime
    content: str
    media_urls: list[dict[str, Any]]


class NewsRepo:
    """Паттерн «Репозиторий» поверх :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_save_posts(self, posts: Sequence[NewsPostData]) -> int:
        """Пакетно сохраняет посты, игнорируя уже существующие.

        Выполняется одним ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``:
        это одна транзакция и один round-trip вместо N коммитов в цикле.

        :param posts: Посты к сохранению (дубликаты внутри пачки допустимы).
        :return: Количество реально добавленных строк.
        :raises RepositoryError: При любой ошибке БД.
        """
        if not posts:
            return 0

        # Дедупликация внутри пачки: ON CONFLICT не защищает от повторов
        # в рамках одного оператора INSERT.
        unique: dict[tuple[str, int], NewsPostData] = {
            (post["channel_name"], post["message_id"]): post for post in posts
        }

        stmt = (
            insert(NewsPost)
            .values(list(unique.values()))
            .on_conflict_do_nothing(index_elements=["channel_name", "message_id"])
            .returning(NewsPost.id)
        )

        try:
            result = await self._session.execute(stmt)
            inserted = len(result.scalars().all())
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            logger.exception("Не удалось сохранить посты (%d шт.): %s", len(unique), exc)
            raise RepositoryError("Ошибка сохранения данных в базу.") from exc

        return inserted

    async def get_recent_posts(self, channel: str, limit: int = 10) -> Sequence[NewsPost]:
        """Возвращает последние посты канала (сначала свежие).

        :raises RepositoryError: При ошибке БД.
        """
        stmt = (
            select(NewsPost)
            .where(NewsPost.channel_name == channel)
            .order_by(NewsPost.post_time.desc(), NewsPost.message_id.desc())
            .limit(limit)
        )
        try:
            result = await self._session.execute(stmt)
        except SQLAlchemyError as exc:
            logger.exception("Не удалось получить посты канала @%s: %s", channel, exc)
            raise RepositoryError("Ошибка чтения данных из базы.") from exc

        return result.scalars().all()

    async def get_post_by_id(self, post_id: int) -> NewsPost | None:
        """Возвращает пост по первичному ключу либо ``None``.

        :raises RepositoryError: При ошибке БД.
        """
        try:
            return await self._session.get(NewsPost, post_id)
        except SQLAlchemyError as exc:
            logger.exception("Не удалось получить пост id=%s: %s", post_id, exc)
            raise RepositoryError("Ошибка чтения данных из базы.") from exc
