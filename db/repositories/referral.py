"""Репозиторий реферальных начислений."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.enums import ReferralStatus
from db.models import Referral, User
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReferralStats:
    """Сводка по приглашениям одного пользователя."""

    total: int
    rewarded: int
    bonus_days: int

    @property
    def is_empty(self) -> bool:
        """Приглашал ли пользователь кого-нибудь вообще."""
        return self.total == 0


@dataclass(frozen=True, slots=True)
class ReferralTotals:
    """Сводка по программе целиком — для панели администратора."""

    total: int
    rewarded: int
    bonus_days: int


class ReferralRepository(BaseRepository[Referral]):
    """Учёт приглашений и выданных за них бонусов."""

    model = Referral

    @handle_db_errors
    async def claim(self, *, referrer_id: int, referred_id: int, code: str) -> Referral | None:
        """Регистрирует приглашение, если приглашённый ещё ничей.

        Точка принятия решения — уникальный индекс по ``referred_id``, а не
        предварительный ``SELECT``. Проверка «а нет ли уже такой записи»
        отдельным запросом оставляет окно, в которое пролезает второй
        одновременный ``/start`` с тем же кодом: оба увидели бы пустоту и
        оба начислили бы бонус. Здесь вторая вставка просто не состоится,
        и вызывающий код получит ``None``.

        :param referrer_id: Пригласивший.
        :param referred_id: Приглашённый.
        :param code: Код, по которому пришёл приглашённый.
        :return: Созданная запись либо ``None``, если приглашение уже было.
        :raises ValueError: Попытка пригласить самого себя.
        """
        if referrer_id == referred_id:
            # На уровне БД это тоже запрещено (CHECK no_self_referral), но
            # нарушение ограничения обошлось бы откатом всей транзакции.
            raise ValueError("Пользователь не может пригласить сам себя.")

        stmt = (
            insert(Referral)
            .values(
                referrer_id=referrer_id,
                referred_id=referred_id,
                code=code,
                status=ReferralStatus.PENDING,
            )
            .on_conflict_do_nothing(index_elements=["referred_id"])
            .returning(Referral)
        )
        referral = (await self._session.execute(stmt)).scalar_one_or_none()

        if referral is None:
            logger.info(
                "Пользователь id=%s уже был приглашён ранее, код %s не применяется",
                referred_id, code,
            )
            return None

        await self._session.flush()
        logger.info(
            "Зарегистрировано приглашение: id=%s пригласил id=%s по коду %s",
            referrer_id, referred_id, code,
        )
        return referral

    @handle_db_errors
    async def get_by_referred(self, referred_id: int) -> Referral | None:
        """Находит приглашение по приглашённому."""
        stmt = select(Referral).where(Referral.referred_id == referred_id)
        return await self._fetch_one(stmt)

    @handle_db_errors
    async def list_for_referrer(self, referrer_id: int, limit: int = 50) -> Sequence[Referral]:
        """Приглашения пользователя, сначала свежие."""
        stmt = (
            select(Referral)
            .where(Referral.referrer_id == referrer_id)
            .order_by(Referral.created_at.desc())
            .limit(limit)
        )
        return await self._fetch_all(stmt)

    @handle_db_errors
    async def stats_for(self, referrer_id: int) -> ReferralStats:
        """Считает приглашения и бонусы одного пользователя.

        Три числа берутся одним запросом: показываются они всегда вместе,
        и три обращения к базе ради одного экрана — лишняя латентность.
        """
        stmt = select(
            func.count(),
            func.count().filter(Referral.status == ReferralStatus.REWARDED),
            func.coalesce(func.sum(Referral.bonus_days), 0),
        ).where(Referral.referrer_id == referrer_id)

        total, rewarded, bonus_days = (await self._session.execute(stmt)).one()
        return ReferralStats(
            total=int(total), rewarded=int(rewarded), bonus_days=int(bonus_days)
        )

    @handle_db_errors
    async def totals(self, since: datetime | None = None) -> ReferralTotals:
        """Сводка по всей реферальной программе.

        :param since: Нижняя граница по дате приглашения.
        :return: Количество приглашений, вознаграждённых и выданных суток.
        """
        stmt = select(
            func.count(),
            func.count().filter(Referral.status == ReferralStatus.REWARDED),
            func.coalesce(func.sum(Referral.bonus_days), 0),
        )
        if since is not None:
            stmt = stmt.where(Referral.created_at >= since)

        total, rewarded, bonus_days = (await self._session.execute(stmt)).one()
        return ReferralTotals(
            total=int(total), rewarded=int(rewarded), bonus_days=int(bonus_days)
        )

    @handle_db_errors
    async def top_referrers(self, limit: int = 10) -> Sequence[tuple[User, int, int]]:
        """Самые результативные пригласившие.

        :param limit: Сколько строк вернуть.
        :return: Тройки «пользователь, приглашений, начислено суток».
        """
        stmt = (
            select(
                User,
                func.count(Referral.id).label("invited"),
                func.coalesce(func.sum(Referral.bonus_days), 0).label("days"),
            )
            .join(Referral, Referral.referrer_id == User.id)
            .group_by(User.id)
            .order_by(func.count(Referral.id).desc(), User.id)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(user, int(invited), int(days)) for user, invited, days in rows]
