"""Репозиторий очереди отложенных публикаций."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from core.logger import get_logger
from db.enums import ScheduledPostStatus
from db.models import ScheduledPost
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)


class ScheduledPostRepository(BaseRepository[ScheduledPost]):
    """Доступ к очереди публикаций.

    Очередь разбирается через ``FOR UPDATE SKIP LOCKED``: несколько реплик
    воркера работают одновременно, и без пропуска заблокированных строк
    вторая реплика ждала бы первую вместо того, чтобы брать соседние
    записи.
    """

    model: ClassVar[type[ScheduledPost]] = ScheduledPost

    @handle_db_errors
    async def claim_due(self, *, now: datetime, limit: int = 50) -> Sequence[ScheduledPost]:
        """Забирает записи, которым пора публиковаться.

        Связанные пост и канал загружаются сразу: публикация обратится к
        обоим, а связи объявлены ``lazy="raise"`` — подгружать их по одной
        значило бы два лишних запроса на каждую запись очереди.

        Строки остаются в состоянии ``pending``: захват держится
        блокировкой до конца транзакции, а признаком обработки служит смена
        статуса после фактической отправки. Пометить их заранее нельзя —
        упавший воркер оставил бы публикации навсегда потерянными.

        :param now: Текущий момент (timezone-aware).
        :param limit: Сколько записей забрать за проход.
        :return: Заблокированные записи очереди.
        """
        stmt = (
            select(ScheduledPost)
            .where(
                ScheduledPost.status == ScheduledPostStatus.PENDING,
                ScheduledPost.publish_at <= now,
            )
            .order_by(ScheduledPost.publish_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .options(
                selectinload(ScheduledPost.post),
                selectinload(ScheduledPost.target_channel),
            )
            .execution_options(populate_existing=True)
        )
        claimed = (await self._session.execute(stmt)).scalars().all()

        if claimed:
            logger.info("Захвачено публикаций: %d", len(claimed))
        return claimed

    @handle_db_errors
    async def list_for_user(
        self,
        user_id: int,
        *,
        status: ScheduledPostStatus | None = None,
        limit: int = 50,
    ) -> Sequence[ScheduledPost]:
        """Возвращает публикации пользователя, сначала ближайшие."""
        stmt = select(ScheduledPost).where(ScheduledPost.user_id == user_id)
        if status is not None:
            stmt = stmt.where(ScheduledPost.status == status)
        stmt = stmt.order_by(ScheduledPost.publish_at).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def count_pending(self, user_id: int) -> int:
        """Считает публикации пользователя, ожидающие отправки."""
        stmt = (
            select(func.count())
            .select_from(ScheduledPost)
            .where(
                ScheduledPost.user_id == user_id,
                ScheduledPost.status == ScheduledPostStatus.PENDING,
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    @handle_db_errors
    async def cancel_for_channel(self, channel_id: int, reason: str) -> int:
        """Отменяет ожидающие публикации в указанный канал.

        Вызывается при отключении канала: очередь, ведущая в канал, куда
        бот больше не может писать, будет только копить неудачные попытки.

        :param channel_id: Идентификатор целевого канала.
        :param reason: Причина отмены для показа пользователю.
        :return: Сколько публикаций отменено.
        """
        stmt = (
            update(ScheduledPost)
            .where(
                ScheduledPost.target_channel_id == channel_id,
                ScheduledPost.status == ScheduledPostStatus.PENDING,
            )
            .values(
                status=ScheduledPostStatus.CANCELLED,
                last_error=reason[:500],
                updated_at=func.now(),
            )
        )
        result = await self._session.execute(stmt)
        cancelled = int(result.rowcount or 0)

        if cancelled:
            logger.info("Отменено публикаций в канал id=%s: %d", channel_id, cancelled)
        return cancelled

    @handle_db_errors
    async def cancel_for_user(self, user_id: int, reason: str) -> int:
        """Отменяет все ожидающие публикации пользователя.

        :param user_id: Владелец публикаций.
        :param reason: Причина отмены.
        :return: Сколько публикаций отменено.
        """
        stmt = (
            update(ScheduledPost)
            .where(
                ScheduledPost.user_id == user_id,
                ScheduledPost.status == ScheduledPostStatus.PENDING,
            )
            .values(
                status=ScheduledPostStatus.CANCELLED,
                last_error=reason[:500],
                updated_at=func.now(),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)
