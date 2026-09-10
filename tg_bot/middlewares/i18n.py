"""Middleware локализации: определяет язык и выдаёт локализатор хендлеру.

Порядок разрешения языка идёт от самого дешёвого источника к самому
дорогому:

1. **кэш** (Redis либо память процесса) — попадание закрывает вопрос без
   единого запроса к базе;
2. **строка пользователя**, если её уже загрузил
   :class:`~tg_bot.middlewares.user_context.UserContextMiddleware`;
3. **явный запрос в базу** по ``telegram_id`` — только при промахе кэша у
   апдейтов, которые до пользователя не доходят;
4. **подсказка клиента Telegram** — для тех, кого в базе ещё нет.

Middleware регистрируется *после* пользовательского контекста: так первый
же апдейт нового человека получает язык из уже созданной строки, а не
лишним запросом. Кэш при этом не бесполезен — он нужен фоновым рассылкам
и тем ответам, которые формируются раньше загрузки профиля.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TelegramUser

from core.logger import get_logger
from db.enums import Language
from db.models import User
from db.uow import UnitOfWork
from services.i18n import LanguageCache, TranslationManager, Translator

logger = get_logger(__name__)

#: Ключ локализатора в контексте хендлера.
I18N_KEY = "i18n"

#: Ключ выбранного языка в контексте хендлера.
LANGUAGE_KEY = "language"


class I18nMiddleware(BaseMiddleware):
    """Кладёт в контекст хендлера готовый :class:`Translator`."""

    def __init__(self, manager: TranslationManager, cache: LanguageCache) -> None:
        self._manager = manager
        self._cache = cache

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user: TelegramUser | None = data.get("event_from_user")
        language = await self._resolve(telegram_user, data)

        data[LANGUAGE_KEY] = language
        data[I18N_KEY] = Translator(self._manager, language)
        return await handler(event, data)

    async def _resolve(
        self,
        telegram_user: TelegramUser | None,
        data: dict[str, Any],
    ) -> Language:
        """Определяет язык для текущего апдейта.

        :param telegram_user: Автор обновления, если он есть.
        :param data: Контекст хендлера.
        :return: Язык интерфейса; при любой неудаче — язык по умолчанию.
        """
        if telegram_user is None:
            # Служебные обновления без автора: спрашивать язык не у кого.
            return self._manager.default_language

        cached = await self._cache.get(telegram_user.id)
        if cached is not None:
            return cached

        user: User | None = data.get("user")
        if user is not None:
            await self._cache.set(telegram_user.id, user.language)
            return user.language

        language = await self._from_database(telegram_user, data)
        await self._cache.set(telegram_user.id, language)
        return language

    async def _from_database(
        self,
        telegram_user: TelegramUser,
        data: dict[str, Any],
    ) -> Language:
        """Читает язык из базы, а при отсутствии строки — угадывает.

        Промах кэша у известного пользователя стоит одного запроса по
        уникальному индексу; для нового человека запрос вернёт пусто, и
        язык берётся из настроек его клиента.

        :param telegram_user: Автор обновления.
        :param data: Контекст хендлера.
        :return: Язык интерфейса.
        """
        uow: UnitOfWork | None = data.get("uow")
        if uow is None:
            logger.warning(
                "I18nMiddleware вызван без UnitOfWork — язык берётся из настроек клиента."
            )
            return Language.from_telegram(telegram_user.language_code)

        stored = await uow.users.get_by_telegram_id(telegram_user.id)
        if stored is not None:
            return stored.language

        return Language.from_telegram(telegram_user.language_code)
