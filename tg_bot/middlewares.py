from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from db.database import async_session_maker
from db.repo import NewsRepo

class DatabaseMiddleware(BaseMiddleware):
    """
    Middleware открывает сессию БД перед обработкой запроса,
    создает Репозиторий и передает его в хендлер.
    После обработки сессия автоматически закрывается.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        async with async_session_maker() as session:
            data['repo'] = NewsRepo(session)
            return await handler(event, data)