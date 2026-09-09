"""Middleware-слой: подготовка зависимостей на каждое обновление."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from core.config import ParserConfig
from core.logger import get_logger
from db.uow import UnitOfWorkFactory
from services.billing import BillingService
from services.dedup import DedupConfig
from services.news_service import NewsService
from services.parser import TelegramWebParser

logger = get_logger(__name__)


class DependenciesMiddleware(BaseMiddleware):
    """Открывает транзакцию и собирает зависимости хендлера.

    Играет роль композиционного корня на время обработки одного апдейта:
    хендлеры получают готовый :class:`NewsService` и ничего не знают о том,
    как он собран.

    Границей транзакции служит обработка апдейта целиком: изменения
    фиксируются только после успешного возврата из хендлера, а любое
    исключение откатывает их. Так частично применённая операция не может
    попасть в базу.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        parser: TelegramWebParser,
        parser_config: ParserConfig,
        invoice_ttl: timedelta,
        dedup_config: DedupConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._parser = parser
        self._parser_config = parser_config
        self._invoice_ttl = invoice_ttl
        self._dedup_config = dedup_config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._uow_factory() as uow:
            data["uow"] = uow
            data["service"] = NewsService(uow, self._parser, self._parser_config, self._dedup_config)
            data["billing"] = BillingService(uow, invoice_ttl=self._invoice_ttl)
            result = await handler(event, data)
            await uow.commit()
            return result
