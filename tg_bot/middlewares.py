"""Middleware-слой: подготовка зависимостей на каждое обновление."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import ParserConfig
from core.logger import get_logger
from db.repo import NewsRepo
from services.news_service import NewsService
from services.parser import TelegramWebParser

logger = get_logger(__name__)


class DependenciesMiddleware(BaseMiddleware):
    """Открывает сессию БД и собирает зависимости хендлера.

    Играет роль «композиционного корня» на время обработки одного апдейта:
    хендлеры получают готовый :class:`NewsService` и ничего не знают о том,
    как он собран. Сессия гарантированно закрывается, а транзакция
    откатывается при любом исключении.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        parser: TelegramWebParser,
        parser_config: ParserConfig,
    ) -> None:
        self._session_factory = session_factory
        self._parser = parser
        self._parser_config = parser_config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            repo = NewsRepo(session)
            data["repo"] = repo
            data["service"] = NewsService(repo, self._parser, self._parser_config)
            try:
                return await handler(event, data)
            except Exception:
                await session.rollback()
                raise
