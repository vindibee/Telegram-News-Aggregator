"""Репозиторий подписок: безопасное продление и работа воркеров."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.enums import (
    LIVE_SUBSCRIPTION_STATUSES,
    SubscriptionEventKind,
    SubscriptionPlan,
    SubscriptionSource,
    SubscriptionStatus,
)
from db.locks import LockNamespace, acquire_xact_lock
from db.models import Subscription, SubscriptionEvent
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)

_LIVE_STATUSES = tuple(LIVE_SUBSCRIPTION_STATUSES)


@dataclass(frozen=True, slots=True)
class SubscriptionCreateResult:
    """Итог операции «получить или создать действующую подписку»."""

    subscription: Subscription
    created: bool


@dataclass(frozen=True, slots=True)
class GrantResult:
    """Итог начисления дней по платежу.

    ``applied=False`` означает, что этот платёж уже был учтён ранее, и
    повторное начисление не выполнялось.
    """

    applied: bool
    subscription: Subscription | None
    event_id: int | None = None

    @property
    def expires_at(self) -> datetime | None:
        """Дата окончания подписки после операции."""
        return self.subscription.expires_at if self.subscription else None


class SubscriptionRepository(BaseRepository[Subscription]):
    """Доступ к подпискам и журналу операций над ними."""

    model: ClassVar[type[Subscription]] = Subscription

    # ----------------------------------------------------------------- чтение
    @handle_db_errors
    async def get_live(self, user_id: int) -> Subscription | None:
        """Возвращает действующую подписку пользователя.

        Действующей считается подписка в статусе ``trialing``/``active``;
        частичный уникальный индекс гарантирует, что она не более одной.
        """
        stmt = select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status.in_(_LIVE_STATUSES),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def get_live_for_update(self, user_id: int) -> Subscription | None:
        """Читает действующую подписку, блокируя строку до конца транзакции.

        Обязательна перед изменением: без блокировки два параллельных
        платежа прочитают одну и ту же дату окончания и второй затрёт
        продление первого.
        """
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_(_LIVE_STATUSES),
            )
            .with_for_update()
            # См. пояснение в BaseRepository.get_for_update.
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def list_history(self, user_id: int, limit: int = 20) -> Sequence[Subscription]:
        """История подписок пользователя, сначала новые."""
        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def list_events(self, subscription_id: int, limit: int = 50) -> Sequence[SubscriptionEvent]:
        """Журнал операций по подписке, сначала новые."""
        stmt = (
            select(SubscriptionEvent)
            .where(SubscriptionEvent.subscription_id == subscription_id)
            .order_by(SubscriptionEvent.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    # ---------------------------------------------------------------- запись
    async def get_or_create_live(
        self,
        user_id: int,
        *,
        plan: SubscriptionPlan,
        source: SubscriptionSource,
        status: SubscriptionStatus,
        period_days: int,
        now: datetime,
    ) -> SubscriptionCreateResult:
        """Возвращает действующую подписку, создавая её при отсутствии.

        Пока подписки нет, блокировать ``FOR UPDATE`` нечего — строки не
        существует. Поэтому критическая секция закрывается advisory-локом
        по пользователю: два параллельных обработчика оплаты не создадут
        две подписки одновременно.

        :param user_id: Владелец подписки.
        :param plan: Тарифный план.
        :param source: Источник подписки.
        :param status: Начальный статус (``trialing`` или ``active``).
        :param period_days: Длительность периода в сутках.
        :param now: Момент операции (timezone-aware).
        :return: Подписка и признак того, что она создана этим вызовом.
        :raises ValueError: Некорректный период или неподходящий статус.
        """
        if period_days <= 0:
            raise ValueError(f"Период подписки должен быть положительным, получено: {period_days}")
        if status not in LIVE_SUBSCRIPTION_STATUSES:
            raise ValueError(f"Начальный статус подписки должен быть действующим, получено: {status}")

        await acquire_xact_lock(self._session, LockNamespace.USER_SUBSCRIPTION, user_id)

        existing = await self.get_live_for_update(user_id)
        if existing is not None:
            logger.debug("У пользователя id=%s уже есть действующая подписка id=%s", user_id, existing.id)
            return SubscriptionCreateResult(subscription=existing, created=False)

        subscription = Subscription(
            user_id=user_id,
            plan=plan,
            status=status,
            source=source,
            started_at=now,
            expires_at=now + timedelta(days=period_days),
        )
        await self.add(subscription)
        await self.record_event(
            subscription_id=subscription.id,
            user_id=user_id,
            kind=SubscriptionEventKind.CREATED,
            days_granted=period_days,
        )
        logger.info(
            "Создана подписка id=%s (user_id=%s, план %s, источник %s, до %s)",
            subscription.id, user_id, plan, source, subscription.expires_at,
        )
        return SubscriptionCreateResult(subscription=subscription, created=True)

    @handle_db_errors
    async def record_event(
        self,
        *,
        subscription_id: int,
        user_id: int,
        kind: SubscriptionEventKind,
        days_granted: int = 0,
        payment_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SubscriptionEvent:
        """Добавляет запись в журнал подписки.

        Для событий, связанных с платежом, предпочтительнее
        :meth:`apply_payment_grant` — он гарантирует однократность.
        """
        event = SubscriptionEvent(
            subscription_id=subscription_id,
            user_id=user_id,
            kind=kind,
            days_granted=days_granted,
            payment_id=payment_id,
            payload=payload or {},
        )
        self._session.add(event)
        await self._session.flush()
        return event

    @handle_db_errors
    async def apply_payment_grant(
        self,
        *,
        payment_id: int,
        subscription_id: int,
        user_id: int,
        days: int,
        plan: SubscriptionPlan | None = None,
        payload: dict[str, Any] | None = None,
        extend: bool = True,
    ) -> GrantResult:
        """Начисляет дни по платежу ровно один раз.

        Порядок шагов принципиален. Сначала выполняется вставка события с
        ``ON CONFLICT (payment_id) DO NOTHING``: уникальный индекс делает
        её точкой принятия решения. Если строка не вставилась, значит этот
        платёж уже учтён — и подписка не трогается вовсе. Проверка «а не
        начисляли ли мы уже» отдельным SELECT оставляла бы окно, в которое
        успевает повторная доставка вебхука.

        Продление считается выражением на стороне БД
        (``GREATEST(expires_at, now()) + interval``), а не в Python: так
        между чтением и записью не остаётся места для чужого обновления.

        :param payment_id: Платёж-основание (ключ идемпотентности).
        :param subscription_id: Продлеваемая подписка.
        :param user_id: Владелец подписки.
        :param days: Количество начисляемых суток (положительное).
        :param plan: Новый тариф, если оплата меняет план.
        :param payload: Произвольные данные для аудита.
        :param extend: Продлевать ли подписку. ``False`` нужен, когда
            подписка только что создана уже с оплаченным периодом: событие
            записать необходимо (иначе повторная доставка вебхука продлит
            срок), а продлевать нечего.
        :return: Результат с признаком применения.
        :raises ValueError: Некорректное число суток.
        """
        if days <= 0:
            raise ValueError(f"Количество суток должно быть положительным, получено: {days}")

        insert_event = (
            insert(SubscriptionEvent)
            .values(
                subscription_id=subscription_id,
                user_id=user_id,
                payment_id=payment_id,
                kind=SubscriptionEventKind.EXTENDED,
                days_granted=days,
                payload=payload or {},
            )
            .on_conflict_do_nothing(index_elements=["payment_id"])
            .returning(SubscriptionEvent.id)
        )
        event_id = (await self._session.execute(insert_event)).scalar_one_or_none()

        if event_id is None:
            logger.info(
                "Платёж id=%s уже учтён, повторное начисление не выполняется", payment_id
            )
            subscription = await self.get_by_id(subscription_id)
            return GrantResult(applied=False, subscription=subscription)

        if not extend:
            logger.info(
                "Платёж id=%s зафиксирован без продления: период уже включён в подписку id=%s",
                payment_id, subscription_id,
            )
            subscription = await self.get_by_id(subscription_id)
            return GrantResult(applied=True, subscription=subscription, event_id=event_id)

        values: dict[str, Any] = {
            # make_interval(years, months, weeks, days) — позиционная форма.
            "expires_at": func.greatest(Subscription.expires_at, func.now())
            + func.make_interval(0, 0, 0, days),
            "status": SubscriptionStatus.ACTIVE,
            # Продление открывает новый цикл уведомлений об окончании.
            "expiry_notified_at": None,
            "updated_at": func.now(),
        }
        if plan is not None:
            values["plan"] = plan

        stmt = (
            update(Subscription)
            .where(Subscription.id == subscription_id)
            .values(**values)
            .returning(Subscription)
            .execution_options(synchronize_session=False)
        )
        subscription = (await self._session.execute(stmt)).scalar_one_or_none()

        logger.info(
            "По платежу id=%s начислено %d суток подписке id=%s (до %s)",
            payment_id, days, subscription_id,
            subscription.expires_at if subscription else "неизвестно",
        )
        return GrantResult(applied=True, subscription=subscription, event_id=event_id)

    @handle_db_errors
    async def cancel(self, subscription_id: int, now: datetime) -> Subscription | None:
        """Отменяет автопродление, оставляя доступ до конца оплаченного срока."""
        stmt = (
            update(Subscription)
            .where(
                Subscription.id == subscription_id,
                Subscription.status.in_(_LIVE_STATUSES),
            )
            .values(
                status=SubscriptionStatus.CANCELLED,
                cancelled_at=now,
                auto_renew=False,
                updated_at=func.now(),
            )
            .returning(Subscription)
            .execution_options(synchronize_session=False)
        )
        subscription = (await self._session.execute(stmt)).scalar_one_or_none()
        if subscription is not None:
            await self.record_event(
                subscription_id=subscription_id,
                user_id=subscription.user_id,
                kind=SubscriptionEventKind.CANCELLED,
            )
            logger.info("Подписка id=%s отменена", subscription_id)
        return subscription

    # --------------------------------------------------------------- воркеры
    @handle_db_errors
    async def claim_expiring(
        self,
        *,
        now: datetime,
        horizon: timedelta,
        limit: int = 100,
    ) -> Sequence[Subscription]:
        """Забирает подписки, истекающие в пределах горизонта, под уведомление.

        Одним запросом делает и выборку, и пометку: ``FOR UPDATE SKIP
        LOCKED`` во вложенном запросе позволяет нескольким воркерам
        разбирать очередь параллельно, не мешая друг другу и не выдавая
        одну подписку дважды. Поле ``expiry_notified_at`` служит признаком
        «уже уведомлён», поэтому перезапуск воркера не порождает дублей.

        :param now: Текущий момент (timezone-aware).
        :param horizon: На сколько вперёд смотреть (например, 24 часа).
        :param limit: Максимальный размер порции.
        :return: Захваченные и уже помеченные подписки.
        :raises ValueError: Некорректные горизонт или размер порции.
        """
        if horizon <= timedelta(0):
            raise ValueError("Горизонт уведомления должен быть положительным.")
        if limit < 1:
            raise ValueError(f"Размер порции должен быть не меньше 1, получено: {limit}")

        candidates = (
            select(Subscription.id)
            .where(
                Subscription.status.in_(_LIVE_STATUSES),
                Subscription.expiry_notified_at.is_(None),
                Subscription.expires_at > now,
                Subscription.expires_at <= now + horizon,
            )
            .order_by(Subscription.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Subscription)
            .where(Subscription.id.in_(candidates))
            .values(expiry_notified_at=now, updated_at=func.now())
            .returning(Subscription)
            .execution_options(synchronize_session=False)
        )
        claimed = (await self._session.execute(stmt)).scalars().all()
        if claimed:
            logger.info("Захвачено %d подписок для уведомления об окончании", len(claimed))
        return claimed

    @handle_db_errors
    async def reset_expiry_notification(self, subscription_ids: Sequence[int]) -> int:
        """Снимает отметку об отправленном уведомлении.

        Нужна воркеру для компенсации: захват помечает подписку до отправки
        сообщения, поэтому при сбое доставки отметку необходимо вернуть —
        иначе пользователь никогда не узнает об окончании подписки.

        :param subscription_ids: Подписки, уведомление по которым не ушло.
        :return: Сколько отметок снято.
        """
        if not subscription_ids:
            return 0

        stmt = (
            update(Subscription)
            .where(
                Subscription.id.in_(tuple(subscription_ids)),
                Subscription.expiry_notified_at.is_not(None),
            )
            .values(expiry_notified_at=None, updated_at=func.now())
            .returning(Subscription.id)
            .execution_options(synchronize_session=False)
        )
        restored = (await self._session.execute(stmt)).scalars().all()
        if restored:
            logger.warning(
                "Отметка уведомления снята у %d подписок: доставка не состоялась", len(restored)
            )
        return len(restored)

    @handle_db_errors
    async def claim_expired(self, *, now: datetime, limit: int = 100) -> Sequence[Subscription]:
        """Переводит истёкшие подписки в статус ``expired`` и возвращает их.

        Так же, как :meth:`claim_expiring`, безопасна для параллельных
        воркеров: строка, уже взятая соседним процессом, пропускается.

        :param now: Текущий момент (timezone-aware).
        :param limit: Максимальный размер порции.
        :return: Подписки, у которых доступ следует отозвать.
        :raises ValueError: Некорректный размер порции.
        """
        if limit < 1:
            raise ValueError(f"Размер порции должен быть не меньше 1, получено: {limit}")

        candidates = (
            select(Subscription.id)
            .where(
                Subscription.status.in_(_LIVE_STATUSES),
                Subscription.expires_at <= now,
            )
            .order_by(Subscription.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Subscription)
            .where(Subscription.id.in_(candidates))
            .values(status=SubscriptionStatus.EXPIRED, updated_at=func.now())
            .returning(Subscription)
            .execution_options(synchronize_session=False)
        )
        expired = (await self._session.execute(stmt)).scalars().all()

        if expired:
            self._session.add_all(
                [
                    SubscriptionEvent(
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                        kind=SubscriptionEventKind.EXPIRED,
                    )
                    for subscription in expired
                ]
            )
            await self._session.flush()
            logger.info("Отозван доступ у %d истёкших подписок", len(expired))
        return expired

    @handle_db_errors
    async def count_by_status(self) -> dict[SubscriptionStatus, int]:
        """Распределение подписок по статусам — основа дашборда метрик."""
        stmt = select(Subscription.status, func.count()).group_by(Subscription.status)
        rows = (await self._session.execute(stmt)).all()
        return {status: int(amount) for status, amount in rows}
