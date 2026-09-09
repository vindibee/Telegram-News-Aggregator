"""Репозиторий новостных постов."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar, TypedDict

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.enums import PostStatus
from db.models import Post
from db.models.post import FTS_CONFIG
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
    duplicate_of_id: int | None
    status: PostStatus


class PostRepository(BaseRepository[Post]):
    """Доступ к новостным постам."""

    model: ClassVar[type[Post]] = Post

    @handle_db_errors
    async def bulk_save(self, posts: Sequence[PostData]) -> dict[tuple[str, int], int]:
        """Пакетно сохраняет посты, игнорируя уже существующие.

        Выполняется одним ``INSERT ... ON CONFLICT DO NOTHING RETURNING``:
        это одна транзакция и один round-trip вместо N вставок в цикле, а
        уникальный индекс делает повторный парсинг канала безопасным.

        Возвращается не счётчик, а соответствие «ключ поста → идентификатор»:
        оно нужно дедупликации, чтобы связать записи, вставленные одной
        пачкой, — у канонической записи до вставки просто нет ``id``.

        :param posts: Посты к сохранению (дубликаты внутри пачки допустимы).
        :return: Отображение ``(канал, message_id) -> id`` для добавленных.
        """
        if not posts:
            return {}

        # Дедупликация внутри пачки: ON CONFLICT не защищает от повторов
        # в рамках одного оператора INSERT.
        unique: dict[tuple[str, int], PostData] = {
            (post["channel_name"], post["message_id"]): post for post in posts
        }

        stmt = (
            insert(Post)
            .values(list(unique.values()))
            .on_conflict_do_nothing(index_elements=["channel_name", "message_id"])
            .returning(Post.channel_name, Post.message_id, Post.id)
        )
        rows = (await self._session.execute(stmt)).all()
        inserted = {(channel, message_id): post_id for channel, message_id, post_id in rows}
        logger.debug("Сохранено %d новых постов из %d полученных", len(inserted), len(unique))
        return inserted

    @handle_db_errors
    async def get_recent(self, channel: str, limit: int = 10) -> Sequence[Post]:
        """Возвращает последние посты канала (сначала свежие).

        Дубликаты исключаются: пользователю показывается по одной записи на
        новость, а не одна и та же новость из трёх источников.
        """
        stmt = (
            select(Post)
            .where(Post.channel_name == channel, Post.duplicate_of_id.is_(None))
            .order_by(Post.post_time.desc(), Post.message_id.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def get_by_content_hash(
        self,
        content_hash: str,
        *,
        since: datetime | None = None,
    ) -> Post | None:
        """Ищет ранее сохранённый пост с тем же нормализованным текстом.

        Первый шаг дедупликации: точные перепечатки ловятся одним поиском по
        индексу, без расчёта расстояний. Возвращается самая ранняя запись —
        она и становится канонической.

        :param content_hash: Хэш нормализованного текста.
        :param since: Нижняя граница по времени публикации.
        :return: Каноническая запись либо ``None``.
        """
        stmt = select(Post).where(
            Post.content_hash == content_hash,
            # Кандидатом может быть только канон: цепочки дубликатов
            # запрещены моделью.
            Post.duplicate_of_id.is_(None),
        )
        if since is not None:
            stmt = stmt.where(Post.post_time >= since)
        stmt = stmt.order_by(Post.post_time).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def find_similar_candidates(
        self,
        bands: Sequence[int],
        *,
        since: datetime,
        limit: int = 200,
        exclude_ids: Sequence[int] = (),
    ) -> Sequence[Post]:
        """Отбирает кандидатов на близкий дубликат по срезам simhash.

        Полный перебор пар недопустим: на миллионе записей это миллион
        сравнений для каждой новости. Совпадение хотя бы одного 16-битного
        среза — индексируемое условие, оставляющее десятки кандидатов вместо
        всей таблицы (схема LSH by banding). Точное расстояние считается уже
        в сервисе, на этой небольшой выборке.

        :param bands: Четыре среза simhash искомого текста.
        :param since: Нижняя граница по времени публикации: новости
            повторяются в пределах короткого окна, и старые записи только
            замедляли бы поиск.
        :param limit: Верхняя граница числа кандидатов.
        :param exclude_ids: Записи, которые не нужно возвращать.
        :return: Кандидаты, отсортированные от старых к новым.
        :raises ValueError: Передано неверное число срезов.
        """
        if len(bands) != 4:
            raise ValueError(f"Ожидается четыре среза simhash, получено: {len(bands)}")

        stmt = select(Post).where(
            or_(
                Post.simhash_band_0 == bands[0],
                Post.simhash_band_1 == bands[1],
                Post.simhash_band_2 == bands[2],
                Post.simhash_band_3 == bands[3],
            ),
            Post.post_time >= since,
            Post.duplicate_of_id.is_(None),
            Post.simhash.is_not(None),
        )
        if exclude_ids:
            stmt = stmt.where(Post.id.not_in(tuple(exclude_ids)))
        stmt = stmt.order_by(Post.post_time).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def find_candidates_by_text(
        self,
        terms: Sequence[str],
        *,
        since: datetime,
        limit: int = 50,
    ) -> Sequence[Post]:
        """Отбирает кандидатов через полнотекстовый поиск.

        Второй канал отбора, дополняющий срезы simhash. Он нужен из-за
        устройства LSH: при четырёх срезах по 16 бит различающиеся биты
        перепечатки с дописанным абзацем попадают во все срезы сразу, и
        совпадения не находится вовсе. Полнотекстовый индекс ищет по общей
        лексике и такие пары находит.

        Ранжирование по ``ts_rank_cd`` ставит впереди записи с наибольшим
        пересечением словаря — именно они и есть вероятные оригиналы.

        :param terms: Характерные слова искомого текста.
        :param since: Нижняя граница по времени публикации.
        :param limit: Верхняя граница числа кандидатов.
        :return: Кандидаты в порядке убывания релевантности.
        """
        if not terms:
            return []

        # Слова уже нормализованы (только буквы и цифры), а сама строка
        # уходит связанным параметром, поэтому подстановка безопасна.
        query = func.to_tsquery(FTS_CONFIG, " | ".join(terms))
        stmt = (
            select(Post)
            .where(
                Post.search_vector.op("@@")(query),
                Post.post_time >= since,
                Post.duplicate_of_id.is_(None),
            )
            .order_by(func.ts_rank_cd(Post.search_vector, query).desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def mark_duplicates(self, links: Mapping[int, int]) -> int:
        """Помечает записи дубликатами указанных канонических.

        Условие ``duplicate_of_id IS NULL`` в запросе делает операцию
        идемпотентной: повторный проход не переписывает уже установленную
        связь и не ломает ранее собранный кластер.

        :param links: Отображение «id дубликата → id канонической записи».
        :return: Сколько записей помечено.
        """
        if not links:
            return 0

        marked = 0
        for duplicate_id, canonical_id in links.items():
            if duplicate_id == canonical_id:
                logger.error("Попытка пометить запись id=%s дубликатом самой себя", duplicate_id)
                continue

            stmt = (
                update(Post)
                .where(Post.id == duplicate_id, Post.duplicate_of_id.is_(None))
                .values(duplicate_of_id=canonical_id, status=PostStatus.DUPLICATE)
                .returning(Post.id)
                .execution_options(synchronize_session=False)
            )
            if (await self._session.execute(stmt)).scalar_one_or_none() is not None:
                marked += 1

        if marked:
            logger.info("Помечено дубликатами: %d записей", marked)
        return marked

    @handle_db_errors
    async def count_duplicates(self, channel: str | None = None) -> int:
        """Сколько записей признано дубликатами (метрика качества фильтра)."""
        stmt = select(func.count()).select_from(Post).where(Post.duplicate_of_id.is_not(None))
        if channel is not None:
            stmt = stmt.where(Post.channel_name == channel)
        return int(await self._session.scalar(stmt) or 0)
