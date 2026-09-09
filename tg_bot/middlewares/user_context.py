"""Middleware регистрации пользователя.

Платежи и подписки ссылаются на ``users.id``, поэтому к моменту работы
любого хендлера строка пользователя должна существовать. Регистрация
вынесена в middleware, а не разбросана по хендлерам: иначе каждый новый
обработчик пришлось бы помнить об этом, и однажды кто-то забыл бы.

Здесь же отсекаются заблокированные пользователи — это самая ранняя точка,
где известно, кто обращается к боту.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TelegramUser

from core.logger import get_logger
from db.uow import UnitOfWork

logger = get_logger(__name__)


class UserContextMiddleware(BaseMiddleware):
    """Создаёт или обновляет пользователя и кладёт его в контекст хендлера.

    Регистрируется как внешний middleware **после**
    :class:`~tg_bot.middlewares.dependencies.DependenciesMiddleware`:
    ему нужна уже открытая единица работы.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user: TelegramUser | None = data.get("event_from_user")
        if telegram_user is None or telegram_user.is_bot:
            # Служебные обновления без автора регистрировать не за кем.
            return await handler(event, data)

        uow: UnitOfWork | None = data.get("uow")
        if uow is None:
            logger.error("UserContextMiddleware вызван без UnitOfWork — проверьте порядок middleware.")
            return await handler(event, data)

        result = await uow.users.get_or_create(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )

        if result.user.is_banned:
            logger.warning(
                "Обновление от заблокированного пользователя %s отброшено", telegram_user.id
            )
            # Транзакция всё равно будет зафиксирована middleware зависимостей:
            # отметка last_seen_at полезна и для заблокированных.
            return None

        data["user"] = result.user
        data["is_new_user"] = result.created
        return await handler(event, data)
