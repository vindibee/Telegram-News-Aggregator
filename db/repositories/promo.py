"""Репозиторий промокодов и их активаций."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.enums import PromocodeKind, SubscriptionPlan
from db.models import Promocode, PromocodeRedemption
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PromocodeTotals:
    """Сводка по промокодам — для панели администратора."""

    codes: int
    active_codes: int
    redemptions: int
    days_granted: int


class PromocodeRepository(BaseRepository[Promocode]):
    """Выдача и применение промокодов."""

    model = Promocode

    @handle_db_errors
    async def get_by_code(self, code: str) -> Promocode | None:
        """Находит промокод по нормализованному коду.

        :param code: Код в каноническом виде (см. ``Promocode.normalize_code``).
        :return: Промокод либо ``None``.
        """
        stmt = select(Promocode).where(Promocode.code == code)
        return await self._fetch_one(stmt)

    @handle_db_errors
    async def get_by_code_for_update(self, code: str) -> Promocode | None:
        """Находит промокод и блокирует строку до конца транзакции.

        Блокировка нужна из-за счётчика активаций: два одновременных
        применения последнего оставшегося кода иначе оба прочитали бы
        ``activations = max_activations - 1`` и оба записали бы увеличенное
        значение, выдав на одну активацию больше лимита.

        ``populate_existing`` обязателен: без него сессия вернула бы уже
        загруженный объект со старым счётчиком, и блокировка защищала бы
        строку, которую код всё равно не видит.
        """
        stmt = (
            select(Promocode)
            .where(Promocode.code == code)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self._fetch_one(stmt)

    @handle_db_errors
    async def redeem(
        self,
        *,
        promocode: Promocode,
        user_id: int,
        days_granted: int,
        payment_id: int | None = None,
    ) -> PromocodeRedemption | None:
        """Фиксирует применение кода пользователем ровно один раз.

        Как и в реферальной программе, решение принимает уникальный
        индекс — здесь по паре «код + пользователь». Пользователь может
        нажать кнопку дважды, и клиент Telegram может доставить одно и то
        же сообщение повторно; вторая вставка не состоится, счётчик
        активаций не сдвинется, а вызывающий код получит ``None``.

        Счётчик увеличивается только после успешной вставки — иначе
        повторное применение «съедало» бы лимит, ничего не выдавая.

        :param promocode: Промокод, заблокированный на запись.
        :param user_id: Кто применяет код.
        :param days_granted: Сколько суток начислено этим применением.
        :param payment_id: Платёж, если код применён к оплате.
        :return: Запись об активации либо ``None`` при повторном применении.
        :raises ValueError: Лимит активаций исчерпан.
        """
        stmt = (
            insert(PromocodeRedemption)
            .values(
                promocode_id=promocode.id,
                user_id=user_id,
                payment_id=payment_id,
                days_granted=days_granted,
            )
            .on_conflict_do_nothing(index_elements=["promocode_id", "user_id"])
            .returning(PromocodeRedemption)
        )
        redemption = (await self._session.execute(stmt)).scalar_one_or_none()

        if redemption is None:
            logger.info(
                "Пользователь id=%s уже применял промокод %s", user_id, promocode.code
            )
            return None

        promocode.register_activation()
        await self._session.flush()
        logger.info(
            "Промокод %s применён пользователем id=%s: %d сут.",
            promocode.code, user_id, days_granted,
        )
        return redemption

    @handle_db_errors
    async def get_redemption(
        self, *, promocode_id: int, user_id: int
    ) -> PromocodeRedemption | None:
        """Находит активацию кода конкретным пользователем.

        Нужна только для внятного отказа: когда лимит уже исчерпан, важно
        отличить «код разобрали другие» от «вы его уже применяли».

        :param promocode_id: Промокод.
        :param user_id: Пользователь.
        :return: Запись об активации либо ``None``.
        """
        stmt = select(PromocodeRedemption).where(
            PromocodeRedemption.promocode_id == promocode_id,
            PromocodeRedemption.user_id == user_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def create(
        self,
        *,
        code: str,
        kind: PromocodeKind,
        value: int,
        created_by_id: int | None = None,
        max_activations: int | None = None,
        plan: SubscriptionPlan | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        comment: str | None = None,
    ) -> Promocode:
        """Создаёт промокод.

        :param code: Код в каноническом виде.
        :param kind: Тип кода (бонусные дни либо скидка).
        :param value: Дни или проценты в зависимости от типа.
        :param created_by_id: Автор кода.
        :param max_activations: Лимит активаций; ``None`` — без лимита.
        :param plan: Ограничение по тарифу.
        :param valid_from: Начало действия.
        :param valid_until: Окончание действия.
        :param comment: Пометка для администратора.
        :return: Созданный промокод.
        """
        promocode = Promocode(
            code=code,
            kind=kind,
            value=value,
            created_by_id=created_by_id,
            max_activations=max_activations,
            plan=plan,
            valid_from=valid_from,
            valid_until=valid_until,
            comment=comment,
        )
        await self.add(promocode)
        logger.info(
            "Создан промокод %s (%s=%d, лимит %s)",
            code, kind, value, max_activations or "∞",
        )
        return promocode

    @handle_db_errors
    async def list_recent(self, limit: int = 20) -> Sequence[Promocode]:
        """Последние созданные коды."""
        stmt = select(Promocode).order_by(Promocode.created_at.desc()).limit(limit)
        return await self._fetch_all(stmt)

    @handle_db_errors
    async def count_redemptions(self, promocode_id: int) -> int:
        """Сколько раз код был применён по журналу активаций.

        Расходится со счётчиком ``activations`` только при ручной правке
        данных, поэтому годится как проверка целостности.
        """
        stmt = (
            select(func.count())
            .select_from(PromocodeRedemption)
            .where(PromocodeRedemption.promocode_id == promocode_id)
        )
        return int(await self._session.scalar(stmt) or 0)

    @handle_db_errors
    async def totals(self) -> PromocodeTotals:
        """Сводка по всем промокодам для панели администратора."""
        codes_stmt = select(
            func.count(),
            func.count().filter(Promocode.is_active),
        )
        codes, active_codes = (await self._session.execute(codes_stmt)).one()

        redemptions_stmt = select(
            func.count(),
            func.coalesce(func.sum(PromocodeRedemption.days_granted), 0),
        )
        redemptions, days = (await self._session.execute(redemptions_stmt)).one()

        return PromocodeTotals(
            codes=int(codes),
            active_codes=int(active_codes),
            redemptions=int(redemptions),
            days_granted=int(days),
        )
