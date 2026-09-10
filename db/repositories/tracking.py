"""Репозиторий коротких ссылок и журнала переходов."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.models import ClickLog, TrackedLink
from db.repositories.base import BaseRepository, handle_db_errors
from db.repositories.errors import ConflictError

logger = get_logger(__name__)

#: Сколько раз пытаться подобрать свободный токен.
_TOKEN_ATTEMPTS: Final[int] = 5


@dataclass(frozen=True, slots=True)
class ClickEvent:
    """Переход, ожидающий записи в журнал."""

    token: str
    clicked_at: datetime
    visitor_hash: str | None = None
    user_id: int | None = None
    referer: str | None = None


@dataclass(frozen=True, slots=True)
class LinkStats:
    """Показатели одной ссылки."""

    link_id: int
    token: str
    target_url: str
    clicks: int
    unique_clicks: int
    post_id: int | None

    @property
    def repeat_rate(self) -> float:
        """Доля повторных переходов среди всех.

        Прямого CTR у нас нет: для него нужен знаменатель — число показов,
        а сколько человек увидело пост в чужом канале, Telegram не
        сообщает. Отношение уникальных переходов ко всем — то, что
        действительно измеримо, и отвечает на вопрос «это интерес разных
        людей или один человек кликал много раз».
        """
        if self.clicks <= 0:
            return 0.0
        return 1 - self.unique_clicks / self.clicks


@dataclass(frozen=True, slots=True)
class OwnerTotals:
    """Сводка по всем ссылкам владельца."""

    links: int
    clicks: int
    unique_clicks: int


class TrackedLinkRepository(BaseRepository[TrackedLink]):
    """Доступ к коротким ссылкам и переходам по ним."""

    model: ClassVar[type[TrackedLink]] = TrackedLink

    # ----------------------------------------------------------------- чтение
    @handle_db_errors
    async def get_by_token(self, token: str) -> TrackedLink | None:
        """Возвращает ссылку по токену из адреса.

        :param token: Токен из короткой ссылки.
        :return: Ссылка либо ``None``.
        """
        stmt = select(TrackedLink).where(TrackedLink.token == token)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def top_for_owner(self, owner_id: int, *, limit: int = 10) -> list[LinkStats]:
        """Возвращает самые популярные ссылки владельца.

        :param owner_id: Владелец ссылок.
        :param limit: Сколько ссылок вернуть.
        :return: Ссылки, отсортированные по числу переходов.
        """
        stmt = (
            select(TrackedLink)
            .where(TrackedLink.owner_id == owner_id, TrackedLink.clicks > 0)
            .order_by(TrackedLink.clicks.desc(), TrackedLink.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            LinkStats(
                link_id=link.id,
                token=link.token,
                target_url=link.target_url,
                clicks=link.clicks,
                unique_clicks=link.unique_clicks,
                post_id=link.post_id,
            )
            for link in rows
        ]

    @handle_db_errors
    async def totals_for_owner(self, owner_id: int) -> OwnerTotals:
        """Считает сводку по всем ссылкам владельца."""
        stmt = select(
            func.count(TrackedLink.id),
            func.coalesce(func.sum(TrackedLink.clicks), 0),
            func.coalesce(func.sum(TrackedLink.unique_clicks), 0),
        ).where(TrackedLink.owner_id == owner_id)

        links, clicks, unique = (await self._session.execute(stmt)).one()
        return OwnerTotals(links=int(links), clicks=int(clicks), unique_clicks=int(unique))

    # ----------------------------------------------------------------- запись
    @handle_db_errors
    async def create(
        self,
        *,
        target_url: str,
        owner_id: int | None = None,
        post_id: int | None = None,
        expires_at: datetime | None = None,
    ) -> TrackedLink:
        """Создаёт короткую ссылку с уникальным токеном.

        Токен генерируется случайно, поэтому коллизия хоть и маловероятна,
        но возможна — и решает её база: при нарушении уникальности
        подбирается следующий. Проверять занятость запросом заранее
        бессмысленно, между проверкой и вставкой остаётся окно.

        :param target_url: Проверенный целевой адрес.
        :param owner_id: Владелец ссылки.
        :param post_id: Пост, из которого ведёт ссылка.
        :param expires_at: Когда ссылка перестаёт работать.
        :return: Созданная ссылка.
        :raises RepositoryError: Не удалось подобрать свободный токен.
        """
        last_conflict: ConflictError | None = None

        for attempt in range(1, _TOKEN_ATTEMPTS + 1):
            link = TrackedLink(
                token=TrackedLink.generate_token(),
                target_url=target_url,
                owner_id=owner_id,
                post_id=post_id,
                expires_at=expires_at,
                clicks=0,
                unique_clicks=0,
                is_active=True,
            )
            try:
                async with self._session.begin_nested():
                    self._session.add(link)
                    await self._session.flush()
            except Exception as exc:  # noqa: BLE001 - разбираем ниже по типу
                from sqlalchemy.exc import IntegrityError

                if not isinstance(exc, IntegrityError):
                    raise
                last_conflict = ConflictError("Коллизия токена короткой ссылки.")
                logger.warning("Коллизия токена короткой ссылки (попытка %d)", attempt)
                continue

            logger.info("Создана короткая ссылка %s -> %s", link.token, target_url[:80])
            return link

        logger.error("Не удалось подобрать токен за %d попыток", _TOKEN_ATTEMPTS)
        raise ConflictError("Не удалось создать короткую ссылку.") from last_conflict

    @handle_db_errors
    async def apply_clicks(self, events: Sequence[ClickEvent]) -> int:
        """Переносит накопленные переходы в базу.

        Журнал пишется пачкой, а счётчики обновляются одним запросом на
        ссылку: за время между сбросами набегают сотни событий, и
        обновлять строку на каждое значило бы столько же UPDATE.

        Уникальность посетителя определяет сама база: у журнала есть
        ограничение ``UNIQUE(link_id, visitor_hash)``, и ``ON CONFLICT DO
        NOTHING`` отвечает на вопрос «этот посетитель здесь впервые?» без
        отдельной проверки.

        :param events: Накопленные переходы.
        :return: Сколько переходов записано.
        """
        if not events:
            return 0

        tokens = {event.token for event in events}
        stmt = select(TrackedLink.id, TrackedLink.token).where(TrackedLink.token.in_(tokens))
        by_token = {token: link_id for link_id, token in (await self._session.execute(stmt))}

        rows = [
            {
                "link_id": by_token[event.token],
                "user_id": event.user_id,
                "clicked_at": event.clicked_at,
                "visitor_hash": event.visitor_hash,
                "referer": event.referer[:255] if event.referer else None,
            }
            for event in events
            if event.token in by_token
        ]
        if not rows:
            logger.warning("Все %d переходов ссылаются на несуществующие ссылки", len(events))
            return 0

        inserted = (
            await self._session.execute(
                insert(ClickLog)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["link_id", "visitor_hash"])
                .returning(ClickLog.link_id)
            )
        ).scalars().all()

        # Уникальные переходы — те, что прошли ограничение; всего переходов
        # — все события, включая повторные визиты того же посетителя.
        unique_per_link: dict[int, int] = {}
        for link_id in inserted:
            unique_per_link[link_id] = unique_per_link.get(link_id, 0) + 1

        total_per_link: dict[int, int] = {}
        for row in rows:
            total_per_link[row["link_id"]] = total_per_link.get(row["link_id"], 0) + 1

        for link_id, total in total_per_link.items():
            await self._session.execute(
                update(TrackedLink)
                .where(TrackedLink.id == link_id)
                .values(
                    clicks=TrackedLink.clicks + total,
                    unique_clicks=TrackedLink.unique_clicks + unique_per_link.get(link_id, 0),
                    updated_at=func.now(),
                )
            )

        logger.info(
            "Записано переходов: %d (уникальных %d) по %d ссылкам",
            len(rows), len(inserted), len(total_per_link),
        )
        return len(rows)

    @handle_db_errors
    async def deactivate(self, link_id: int) -> bool:
        """Выключает ссылку, не удаляя статистику.

        :param link_id: Идентификатор ссылки.
        :return: ``True``, если состояние изменилось.
        """
        stmt = (
            update(TrackedLink)
            .where(TrackedLink.id == link_id, TrackedLink.is_active.is_(True))
            .values(is_active=False, updated_at=func.now())
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)
