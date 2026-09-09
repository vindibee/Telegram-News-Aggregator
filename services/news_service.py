"""Прикладной сервис: связывает парсер, репозиторий и бизнес-правила.

Хендлеры не знают ни про HTTP, ни про SQL — они работают только с этим слоем.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.config import ParserConfig
from core.logger import get_logger
from db.models import Post
from db.repositories import PostData
from db.uow import UnitOfWork
from services.fingerprint import build_fingerprint
from services.parser import ParsedPost, TelegramWebParser

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Итог обновления канала."""

    added: int
    posts: Sequence[Post]


class NewsService:
    """Сценарии работы с новостями канала."""

    def __init__(self, uow: UnitOfWork, parser: TelegramWebParser, config: ParserConfig) -> None:
        self._uow = uow
        self._parser = parser
        self._config = config

    async def get_posts(self, channel: str) -> Sequence[Post]:
        """Последние сохранённые посты канала."""
        return await self._uow.posts.get_recent(channel, limit=self._config.max_posts)

    async def get_post(self, post_id: int) -> Post | None:
        """Пост по идентификатору."""
        return await self._uow.posts.get(post_id)

    async def refresh(self, channel: str) -> RefreshResult:
        """Парсит канал, сохраняет новые посты и возвращает актуальный список.

        :raises ParserError: Канал недоступен или сеть недоступна.
        :raises RepositoryError: Ошибка записи или чтения БД.
        """
        parsed = await self._parser.fetch_posts(channel)
        added = await self._uow.posts.bulk_save(
            [self._to_row(channel, post) for post in parsed]
        )
        logger.info("Канал @%s обновлён: получено %d, новых %d", channel, len(parsed), added)

        posts = await self._uow.posts.get_recent(channel, limit=self._config.max_posts)
        return RefreshResult(added=added, posts=posts)

    @staticmethod
    def _to_row(channel: str, post: ParsedPost) -> PostData:
        """Преобразует распарсенный пост в строку для вставки.

        Отпечатки считаются здесь, на границе домена: к моменту записи в БД
        значения уже готовы, и конвейеру дедупликации не приходится
        перечитывать таблицу.
        """
        fingerprint = build_fingerprint(post.text)
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
        )
