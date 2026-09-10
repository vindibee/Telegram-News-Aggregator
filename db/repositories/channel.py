"""Репозиторий каналов пользователя: источники и цели публикации."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.enums import ChannelKind
from db.models import UserChannel
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelCreateResult:
    """Итог подключения канала."""

    channel: UserChannel
    created: bool

    @property
    def already_connected(self) -> bool:
        """Был ли канал подключён раньше."""
        return not self.created


class ChannelRepository(BaseRepository[UserChannel]):
    """Доступ к подключённым каналам.

    Подключение канала повторяется чаще, чем кажется: человек присылает
    ссылку дважды, возвращается к тому же экрану, жмёт кнопку повторно.
    Поэтому добавление идемпотентно и опирается на уникальность в БД, а не
    на предварительную проверку «а нет ли уже такого» — та оставляла бы
    окно гонки между SELECT и INSERT.
    """

    model: ClassVar[type[UserChannel]] = UserChannel

    # ----------------------------------------------------------------- чтение
    @handle_db_errors
    async def get_by_username(
        self,
        user_id: int,
        kind: ChannelKind,
        username: str,
    ) -> UserChannel | None:
        """Возвращает канал пользователя по публичному имени.

        :param user_id: Владелец канала.
        :param kind: Роль канала.
        :param username: Имя без «@» в каноническом виде.
        :return: Канал либо ``None``.
        """
        stmt = select(UserChannel).where(
            UserChannel.user_id == user_id,
            UserChannel.kind == kind,
            UserChannel.username == username,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def get_by_chat_id(
        self,
        user_id: int,
        kind: ChannelKind,
        chat_id: int,
    ) -> UserChannel | None:
        """Возвращает канал пользователя по идентификатору чата."""
        stmt = select(UserChannel).where(
            UserChannel.user_id == user_id,
            UserChannel.kind == kind,
            UserChannel.chat_id == chat_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def list_for_user(
        self,
        user_id: int,
        kind: ChannelKind | None = None,
        *,
        only_active: bool = False,
    ) -> Sequence[UserChannel]:
        """Возвращает каналы пользователя.

        :param user_id: Владелец.
        :param kind: Ограничение по роли; ``None`` — обе роли.
        :param only_active: Возвращать только включённые каналы.
        :return: Каналы, отсортированные по времени подключения.
        """
        stmt = select(UserChannel).where(UserChannel.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(UserChannel.kind == kind)
        if only_active:
            stmt = stmt.where(UserChannel.is_active.is_(True))
        stmt = stmt.order_by(UserChannel.created_at)
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def list_active_sources(self, *, limit: int = 500) -> Sequence[UserChannel]:
        """Возвращает активные источники всех пользователей.

        Запрос фонового парсера: он обходит источники независимо от того,
        кто их подключил. Лимит обязателен — по мере роста продукта эта
        выборка становится самой большой в приложении.

        :param limit: Максимальное число каналов за проход.
        :return: Активные каналы-источники.
        """
        stmt = (
            select(UserChannel)
            .where(
                UserChannel.kind == ChannelKind.SOURCE,
                UserChannel.is_active.is_(True),
            )
            # Первыми идут те, кого дольше всех не обновляли; NULL — это
            # ни разу не синхронизированные каналы, они важнее прочих.
            .order_by(UserChannel.last_synced_at.asc().nulls_first())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def count_active(self, user_id: int, kind: ChannelKind) -> int:
        """Считает включённые каналы пользователя в указанной роли.

        Нужен для проверки лимитов тарифа: сам лимит — правило продукта и
        живёт в конфигурации, а не в базе.
        """
        stmt = (
            select(func.count())
            .select_from(UserChannel)
            .where(
                UserChannel.user_id == user_id,
                UserChannel.kind == kind,
                UserChannel.is_active.is_(True),
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    # ----------------------------------------------------------------- запись
    @handle_db_errors
    async def connect(
        self,
        *,
        user_id: int,
        kind: ChannelKind,
        username: str | None = None,
        chat_id: int | None = None,
        title: str = "",
    ) -> ChannelCreateResult:
        """Подключает канал или возвращает уже подключённый.

        Повторное подключение не создаёт дубль и не считается ошибкой:
        решение принимает уникальный индекс через ``ON CONFLICT DO
        NOTHING``. Ранее отключённый канал при этом включается заново —
        человек, добавляющий его снова, ожидает именно этого.

        :param user_id: Владелец канала.
        :param kind: Роль канала.
        :param username: Публичное имя без «@».
        :param chat_id: Идентификатор чата (обязателен для целей публикации).
        :param title: Отображаемое название.
        :return: Канал и признак того, что он создан этим вызовом.
        :raises ValueError: Не передан ни один идентификатор канала.
        """
        if username is None and chat_id is None:
            raise ValueError("Нужен хотя бы один идентификатор канала: username или chat_id.")
        if kind is ChannelKind.TARGET and chat_id is None:
            raise ValueError("Для канала публикации обязателен chat_id.")

        # Конфликт возможен по любому из двух уникальных индексов, поэтому
        # ON CONFLICT указывает конкретный набор колонок — тот, по которому
        # канал вообще опознаётся.
        conflict_columns = ["user_id", "kind", "username" if username is not None else "chat_id"]

        stmt = (
            insert(UserChannel)
            .values(
                user_id=user_id,
                kind=kind,
                username=username,
                chat_id=chat_id,
                title=title,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=conflict_columns)
            .returning(UserChannel)
        )
        channel = (await self._session.execute(stmt)).scalar_one_or_none()

        if channel is not None:
            logger.info(
                "Подключён канал id=%s (user_id=%s, роль %s, %s)",
                channel.id, user_id, kind.value, channel.display_name,
            )
            return ChannelCreateResult(channel=channel, created=True)

        existing = (
            await self.get_by_username(user_id, kind, username)
            if username is not None
            else await self.get_by_chat_id(user_id, kind, chat_id)  # type: ignore[arg-type]
        )
        if existing is None:
            # Строка исчезла между вставкой и чтением: соседняя транзакция
            # откатилась. Повтор операции решает проблему.
            logger.error("Канал не найден после конфликта вставки: user_id=%s", user_id)
            raise LookupError("Канал недоступен сразу после конфликта вставки.")

        if not existing.is_active:
            existing.is_active = True
            existing.last_error = None
            await self._session.flush()
            logger.info("Ранее отключённый канал id=%s включён заново", existing.id)

        return ChannelCreateResult(channel=existing, created=False)

    @handle_db_errors
    async def disconnect(self, user_id: int, channel_id: int) -> bool:
        """Удаляет канал пользователя.

        Идентификатор владельца входит в условие намеренно: он приходит из
        callback_data, то есть от клиента, и без проверки чужой канал
        удалялся бы по подобранному номеру.

        :param user_id: Владелец канала.
        :param channel_id: Идентификатор канала.
        :return: ``True``, если канал был удалён.
        """
        from sqlalchemy import delete as sql_delete

        stmt = sql_delete(UserChannel).where(
            UserChannel.id == channel_id,
            UserChannel.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        removed = bool(result.rowcount)

        if removed:
            logger.info("Канал id=%s отключён пользователем %s", channel_id, user_id)
        else:
            logger.warning(
                "Попытка удалить чужой или несуществующий канал id=%s пользователем %s",
                channel_id, user_id,
            )
        return removed

    @handle_db_errors
    async def set_active(self, user_id: int, channel_id: int, *, active: bool) -> bool:
        """Включает или выключает канал, не удаляя его.

        :param user_id: Владелец канала.
        :param channel_id: Идентификатор канала.
        :param active: Новое состояние.
        :return: ``True``, если состояние изменилось.
        """
        stmt = (
            update(UserChannel)
            .where(
                UserChannel.id == channel_id,
                UserChannel.user_id == user_id,
                UserChannel.is_active.is_(not active),
            )
            .values(is_active=active, updated_at=func.now())
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)

    @handle_db_errors
    async def set_bot_admin(self, channel_id: int, *, is_admin: bool) -> None:
        """Сохраняет результат проверки прав бота в целевом канале.

        :param channel_id: Идентификатор канала.
        :param is_admin: Является ли бот администратором.
        """
        stmt = (
            update(UserChannel)
            .where(UserChannel.id == channel_id)
            .values(bot_is_admin=is_admin, updated_at=func.now())
        )
        await self._session.execute(stmt)
        logger.info("Права бота в канале id=%s: %s", channel_id, "администратор" if is_admin else "нет")

    @handle_db_errors
    async def mark_synced(self, channel_id: int, moment: datetime) -> None:
        """Отмечает успешное чтение источника и снимает прошлую ошибку.

        :param channel_id: Идентификатор канала.
        :param moment: Момент синхронизации (timezone-aware).
        """
        stmt = (
            update(UserChannel)
            .where(UserChannel.id == channel_id)
            .values(last_synced_at=moment, last_error=None, updated_at=func.now())
        )
        await self._session.execute(stmt)

    @handle_db_errors
    async def mark_failed(self, channel_id: int, reason: str) -> None:
        """Сохраняет причину сбоя обработки канала.

        :param channel_id: Идентификатор канала.
        :param reason: Текст ошибки.
        """
        stmt = (
            update(UserChannel)
            .where(UserChannel.id == channel_id)
            .values(last_error=reason.strip()[:500], updated_at=func.now())
        )
        await self._session.execute(stmt)
        logger.warning("Канал id=%s помечен ошибкой: %s", channel_id, reason[:120])
