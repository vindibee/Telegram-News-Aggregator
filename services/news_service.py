"""Прикладной сервис: связывает парсер, дедупликацию и репозиторий.

Хендлеры не знают ни про HTTP, ни про SQL — они работают только с этим слоем.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.config import ParserConfig
from core.logger import get_logger
from db.enums import PostStatus
from db.models import Post
from db.repositories import PostData
from db.uow import UnitOfWork
from services.dedup import DedupCandidate, DedupConfig, DeduplicationService, DuplicateMatch
from services.fingerprint import build_fingerprint
from services.parser import ParsedPost, TelegramWebParser

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Итог обновления канала."""

    added: int
    duplicates: int
    posts: Sequence[Post]

    @property
    def unique_added(self) -> int:
        """Сколько добавлено записей, не признанных дубликатами."""
        return max(0, self.added - self.duplicates)


class NewsService:
    """Сценарии работы с новостями канала."""

    def __init__(
        self,
        uow: UnitOfWork,
        parser: TelegramWebParser,
        config: ParserConfig,
        dedup_config: DedupConfig | None = None,
    ) -> None:
        self._uow = uow
        self._parser = parser
        self._config = config
        self._dedup = DeduplicationService(uow.posts, dedup_config)

    async def get_posts(self, channel: str) -> Sequence[Post]:
        """Последние сохранённые записи канала."""
        return await self._uow.posts.get_recent(channel, limit=self._config.max_posts)

    async def get_post(self, post_id: int) -> Post | None:
        """Запись по идентификатору."""
        return await self._uow.posts.get(post_id)

    async def refresh(self, channel: str) -> RefreshResult:
        """Парсит канал, сохраняет новые записи и отсеивает повторы.

        Порядок шагов определяется одним ограничением: связать дубликат с
        оригиналом можно только по идентификатору, а у записи из той же
        пачки его до вставки нет. Поэтому сначала сохраняются все записи,
        и лишь затем проставляются связи.

        :raises ParserError: Канал недоступен или сеть недоступна.
        :raises RepositoryError: Ошибка записи или чтения БД.
        """
        parsed = await self._parser.fetch_posts(channel)
        candidates = [self._to_candidate(channel, post) for post in parsed]

        matches = await self._dedup.classify(candidates)
        rows = [self._to_row(channel, post, matches) for post in parsed]

        inserted = await self._uow.posts.bulk_save(rows)
        marked = await self._link_batch_duplicates(matches, inserted)

        logger.info(
            "Канал @%s обновлён: получено %d, новых %d, дубликатов %d",
            channel, len(parsed), len(inserted), len(matches),
        )

        posts = await self._uow.posts.get_recent(channel, limit=self._config.max_posts)
        return RefreshResult(added=len(inserted), duplicates=marked + self._known_links(matches), posts=posts)

    async def _link_batch_duplicates(
        self,
        matches: dict[tuple[str, int], DuplicateMatch],
        inserted: dict[tuple[str, int], int],
    ) -> int:
        """Проставляет связи для дубликатов внутри одной пачки.

        Записи, чей оригинал уже был в базе, получили ``duplicate_of_id``
        прямо при вставке. Здесь обрабатываются только те, чей оригинал
        добавлен этой же операцией и получил идентификатор только что.

        :return: Сколько связей проставлено.
        """
        links: dict[int, int] = {}
        for key, match in matches.items():
            if not match.is_in_batch:
                continue

            duplicate_id = inserted.get(key)
            canonical_id = inserted.get(match.canonical_key) if match.canonical_key else None
            if duplicate_id is None or canonical_id is None:
                # Запись уже существовала и не вставлялась заново — связь
                # либо проставлена ранее, либо не нужна.
                continue
            links[duplicate_id] = canonical_id

        return await self._uow.posts.mark_duplicates(links)

    @staticmethod
    def _known_links(matches: dict[tuple[str, int], DuplicateMatch]) -> int:
        """Сколько дубликатов связано с уже существовавшими записями."""
        return sum(1 for match in matches.values() if not match.is_in_batch)

    @staticmethod
    def _to_candidate(channel: str, post: ParsedPost) -> DedupCandidate:
        """Готовит запись к проверке на дублирование."""
        return DedupCandidate(
            key=(channel, post.message_id),
            text=post.text,
            post_time=post.post_time,
            fingerprint=build_fingerprint(post.text),
        )

    @staticmethod
    def _to_row(
        channel: str,
        post: ParsedPost,
        matches: dict[tuple[str, int], DuplicateMatch],
    ) -> PostData:
        """Преобразует распарсенный пост в строку для вставки.

        Отпечатки считаются здесь, на границе домена: к моменту записи в БД
        значения уже готовы, и конвейеру дедупликации не приходится
        перечитывать таблицу.
        """
        fingerprint = build_fingerprint(post.text)
        match = matches.get((channel, post.message_id))
        # Дубликат записи, уже лежащей в базе, связывается сразу при вставке;
        # для повтора внутри пачки идентификатор оригинала появится только
        # после неё, и связь проставляется вторым шагом.
        canonical_id = match.canonical_id if match is not None else None

        return PostData(
            channel_name=channel,
            message_id=post.message_id,
            post_time=post.post_time,
            content=post.text,
            media_urls=[item.as_dict() for item in post.media],
            content_hash=fingerprint.content_hash,
            simhash=fingerprint.simhash,
            simhash_band_0=fingerprint.bands[0],
            simhash_band_1=fingerprint.bands[1],
            simhash_band_2=fingerprint.bands[2],
            simhash_band_3=fingerprint.bands[3],
            duplicate_of_id=canonical_id,
            status=PostStatus.DUPLICATE if match is not None else PostStatus.NEW,
        )
