"""Задачи мониторинга подписок.

Две зеркальные операции: предупредить об окончании и отозвать доступ у
истёкших. Обе разбирают очередь через ``FOR UPDATE SKIP LOCKED``, поэтому
несколько реплик воркера не мешают друг другу и не обрабатывают одну
подписку дважды.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Final

from core.config import Settings
from core.logger import get_logger
from db.models import Subscription
from db.uow import UnitOfWorkFactory
from services.i18n import TranslationManager, Translator
from services.notifier import TelegramNotifier
from worker.tasks.base import PeriodicTask, TaskResult

logger = get_logger(__name__)



class ExpiryNotificationTask(PeriodicTask):
    """Предупреждает об окончании подписки за сутки.

    Отметка об отправке ставится вместе с захватом, одним запросом — иначе
    два воркера успели бы уведомить одного пользователя дважды. Но раз
    отметка ставится *до* доставки, при сбое отправки её необходимо снять,
    иначе уведомление потеряется навсегда: следующий прогон такую подписку
    уже не увидит.
    """

    name = "subscription_expiry_notice"

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        notifier: TelegramNotifier,
        settings: Settings,
        translations: TranslationManager | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._notifier = notifier
        self._settings = settings
        # Рассылка идёт каждому на его языке: получатель не выбирал
        # момент уведомления и тем более не ждёт его по-русски.
        self._translations = translations or TranslationManager.from_directory()
        self._horizon = timedelta(hours=settings.worker.expiry_notice_hours)
        self._batch_size = settings.worker.batch_size
        self.interval = float(settings.worker.expiry_check_interval)

    async def run(self) -> TaskResult:
        """Забирает истекающие подписки и рассылает предупреждения."""
        now = datetime.now(tz=timezone.utc)

        async with self._uow_factory() as uow:
            claimed = await uow.subscriptions.claim_expiring(
                now=now, horizon=self._horizon, limit=self._batch_size
            )
            recipients = [
                (subscription, await uow.users.get(subscription.user_id))
                for subscription in claimed
            ]
            # Фиксируем захват сразу: пока сообщения уходят, соседняя реплика
            # не должна видеть эти подписки свободными.
            await uow.commit()

        if not recipients:
            return TaskResult()

        undelivered: list[int] = []
        blocked: list[int] = []
        delivered = 0

        for subscription, user in recipients:
            if user is None:
                logger.error(
                    "Подписка id=%s ссылается на несуществующего пользователя", subscription.id
                )
                undelivered.append(subscription.id)
                continue

            result = await self._notifier.send(
                user.telegram_id,
                self._build_text(subscription, Translator(self._translations, user.language)),
            )
            if result.delivered:
                delivered += 1
            elif result.blocked:
                # Бот заблокирован: доставлять нечего и повторять незачем,
                # отметку об уведомлении оставляем.
                blocked.append(user.id)
            else:
                undelivered.append(subscription.id)

        await self._compensate(undelivered, blocked)

        result = TaskResult(
            processed=len(recipients),
            succeeded=delivered,
            failed=len(undelivered),
            details={"blocked": len(blocked)},
        )
        logger.info("Уведомления об окончании подписки: %s", result.describe())
        return result

    def _build_text(self, subscription: Subscription, i18n: Translator) -> str:
        expires = subscription.expires_at.astimezone(
            self._settings.display_timezone
        ).strftime("%d.%m.%Y %H:%M")
        return i18n(
            "worker.expiry_notice",
            plan=escape(subscription.plan.value),
            expires=escape(expires),
        )

    async def _compensate(self, undelivered: list[int], blocked: list[int]) -> None:
        """Возвращает отметку недоставленным и помечает заблокировавших бота."""
        if not undelivered and not blocked:
            return

        async with self._uow_factory() as uow:
            if undelivered:
                await uow.subscriptions.reset_expiry_notification(undelivered)
            for user_id in blocked:
                await uow.users.mark_bot_blocked(user_id)
            await uow.commit()


class SubscriptionExpirationTask(PeriodicTask):
    """Отзывает доступ у подписок с истёкшим сроком.

    В отличие от уведомления, смена статуса — самостоятельная ценность:
    если сообщение не дошло, доступ всё равно должен быть закрыт, поэтому
    компенсации здесь нет.
    """

    name = "subscription_expiration"

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        notifier: TelegramNotifier,
        settings: Settings,
        translations: TranslationManager | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._notifier = notifier
        self._settings = settings
        self._translations = translations or TranslationManager.from_directory()
        self._batch_size = settings.worker.batch_size
        self.interval = float(settings.worker.expiration_check_interval)

    async def run(self) -> TaskResult:
        """Переводит истёкшие подписки в ``expired`` и сообщает об этом."""
        now = datetime.now(tz=timezone.utc)

        async with self._uow_factory() as uow:
            expired = await uow.subscriptions.claim_expired(now=now, limit=self._batch_size)
            recipients = [
                (subscription, await uow.users.get(subscription.user_id))
                for subscription in expired
            ]
            await uow.commit()

        if not recipients:
            return TaskResult()

        delivered = 0
        failed = 0
        blocked: list[int] = []

        for subscription, user in recipients:
            if user is None:
                failed += 1
                continue

            result = await self._notifier.send(
                user.telegram_id,
                self._build_text(subscription, Translator(self._translations, user.language)),
            )
            if result.delivered:
                delivered += 1
            elif result.blocked:
                blocked.append(user.id)
            else:
                failed += 1

        if blocked:
            async with self._uow_factory() as uow:
                for user_id in blocked:
                    await uow.users.mark_bot_blocked(user_id)
                await uow.commit()

        result = TaskResult(
            processed=len(recipients),
            succeeded=delivered,
            failed=failed,
            details={"blocked": len(blocked)},
        )
        logger.info("Отзыв доступа по истёкшим подпискам: %s", result.describe())
        return result

    def _build_text(self, subscription: Subscription, i18n: Translator) -> str:
        expires = subscription.expires_at.astimezone(
            self._settings.display_timezone
        ).strftime("%d.%m.%Y %H:%M")
        return i18n(
            "worker.expired",
            plan=escape(subscription.plan.value),
            expires=escape(expires),
        )
