"""Репозиторий новостных постов."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, TypedDict

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.models import Post
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)


class PostData(TypedDict):
    """Набор полей для вставки одного поста.

    Отпечатки (``content_hash``, ``simhash``, ``simhash_band_*``) считает
    сервис через :mod:`services.fingerprint`: репозиторий не занимается
    бизнес-логикой, он только сохраняет готовые значения.
    """

    channel_name: str
    message_id: int
    post_time: datetime
    content: str
    media_urls: list[dict[str, Any]]
    content_hash: str
    simhash: int
    simhash_band_0: int
    simhash_band_1: int
    simhash_band_2: int
    simhash_band_3: int


class PostRepository(BaseRepository[Post]):
    """Доступ к новостным постам."""

    model: ClassVar[type[Post]] = Post

    @handle_db_errors
    async def bulk_save(self, posts: Sequence[PostData]) -> int:
        """Пакетно сохраняет посты, игнорируя уже существующие.

        Выполняется одним ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``:
        это одна транзакция и один round-trip вместо N вставок в цикле, а
        уникальный индекс делает повторный парсинг канала безопасным.

        :param posts: Посты к сохранению (дубликаты внутри пачки допустимы).
        :return: Количество реально добавленных строк.
        """
        if not posts:
            return 0

        # Дедупликация внутри пачки: ON CONFLICT не защищает от повторов
        # в рамках одного оператора INSERT.
        unique: dict[tuple[str, int], PostData] = {
            (post["channel_name"], post["message_id"]): post for post in posts
        }

        stmt = (
            insert(Post)
            .values(list(unique.values()))
            .on_conflict_do_nothing(index_elements=["channel_name", "message_id"])
            .returning(Post.id)
        )
        inserted = len((await self._session.execute(stmt)).scalars().all())
        logger.debug("Сохранено %d новых постов из %d полученных", inserted, len(unique))
        return inserted

    @handle_db_errors
    async def get_recent(self, channel: str, limit: int = 10) -> Sequence[Post]:
        """Возвращает последние посты канала (сначала свежие)."""
        stmt = (
            select(Post)
            .where(Post.channel_name == channel)
            .order_by(Post.post_time.desc(), Post.message_id.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def get_by_content_hash(self, content_hash: str) -> Post | None:
        """Ищет ранее сохранённый пост с тем же нормализованным текстом.

        Первый шаг дедупликации: точные перепечатки ловятся одним поиском
        по индексу, без расчёта расстояний.
        """
        stmt = (
            select(Post)
            .where(Post.content_hash == content_hash, Post.duplicate_of_id.is_(None))
            .order_by(Post.post_time)
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
