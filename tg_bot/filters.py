"""Фильтры хендлеров.

Проверка прав вынесена в фильтр, а не написана первой строкой каждого
обработчика. Разница не косметическая: не прошедший фильтр апдейт вообще
не считается обработанным, поэтому админские команды остаются невидимыми
для посторонних — бот отвечает на них ровно так же, как на любой другой
незнакомый текст. Проверка внутри хендлера, наоборот, выдала бы сам факт
существования команды.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from core.config import Settings
from core.logger import get_logger
from db.models import User

logger = get_logger(__name__)


class IsAdmin(BaseFilter):
    """Пропускает только администраторов бота.

    Источников прав два, и оба нужны. Флаг ``users.is_admin`` в базе —
    рабочий: права выдаются и снимаются на ходу. Список ``ADMIN_IDS`` в
    окружении — стартовый: первый флаг в базе кто-то должен выставить, а
    сделать это через бота может только тот, у кого права уже есть.

    Список из окружения при этом не «сильнее» базы, а равноправен с ней:
    потерять доступ к собственной панели из-за неудачного ``UPDATE``
    неприятнее, чем держать пару идентификаторов в конфигурации.
    """

    async def __call__(
        self,
        event: TelegramObject,
        user: User | None = None,
        settings: Settings | None = None,
    ) -> bool:
        """Проверяет права отправителя.

        :param event: Обрабатываемое событие.
        :param user: Пользователь из контекста (может отсутствовать).
        :param settings: Настройки приложения.
        :return: Является ли отправитель администратором.
        """
        if user is None:
            # Служебные апдейты без автора: администратора в них нет.
            return False

        if user.is_admin:
            return True

        if settings is not None and settings.admin.is_admin(user.telegram_id):
            return True

        return False


class IsNotAdmin(BaseFilter):
    """Пропускает всех, кроме администраторов.

    Нужен там, где у администратора и обычного пользователя разные
    сценарии на одной команде.
    """

    def __init__(self) -> None:
        self._is_admin = IsAdmin()

    async def __call__(
        self,
        event: TelegramObject,
        user: User | None = None,
        settings: Settings | None = None,
    ) -> bool:
        """Инвертирует проверку прав."""
        return not await self._is_admin(event, user=user, settings=settings)


__all__ = ["IsAdmin", "IsNotAdmin"]
