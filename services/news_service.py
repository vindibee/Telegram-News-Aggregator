"""Прикладной сервис: связывает парсер, репозиторий и бизнес-правила.

Хендлеры не знают ни про HTTP, ни про SQL — они работают только с этим слоем.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.config import ParserConfig
from core.logger import get_logger
from db.models import NewsPost
from db.repo import NewsPostData, NewsRepo
from services.parser import ParsedPost, TelegramWebParser

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Итог обновления канала."""

    added: int
    posts: Sequence[NewsPost]


class NewsService:
    """Сценарии работы с новостями канала."""

    def __init__(self, repo: NewsRepo, parser: TelegramWebParser, config: ParserConfig) -> None:
        self._repo = repo
        self._parser = parser
        self._config = config

    async def get_posts(self, channel: str) -> Sequence[NewsPost]:
        """Последние сохранённые посты канала."""
        return await self._repo.get_recent_posts(channel, limit=self._config.max_posts)

    async def get_post(self, post_id: int) -> NewsPost | None:
        """Пост по идентификатору."""
        return await self._repo.get_post_by_id(post_id)

    async def refresh(self, channel: str) -> RefreshResult:
        """Парсит канал, сохраняет новые посты и возвращает актуальный список.

        :raises ParserError: Канал недоступен или сеть недоступна.
        :raises RepositoryError: Ошибка записи или чтения БД.
        """
        parsed = await self._parser.fetch_posts(channel)
        added = await self._repo.bulk_save_posts(
            [self._to_row(channel, post) for post in parsed]
        )
        logger.info("Канал @%s обновлён: получено %d, новых %d", channel, len(parsed), added)

        posts = await self._repo.get_recent_posts(channel, limit=self._config.max_posts)
        return RefreshResult(added=added, posts=posts)

    @staticmethod
    def _to_row(channel: str, post: ParsedPost) -> NewsPostData:
        return NewsPostData(
            channel_name=channel,
            message_id=post.message_id,
            post_time=post.post_time,
            content=post.text,
            media_urls=[item.as_dict() for item in post.media],
        )
