"""Задача обслуживания счетов."""

from __future__ import annotations

from datetime import datetime, timezone

from core.config import Settings
from core.logger import get_logger
from db.uow import UnitOfWorkFactory
from worker.tasks.base import PeriodicTask, TaskResult

logger = get_logger(__name__)


class StaleInvoiceCleanupTask(PeriodicTask):
    """Помечает неоплаченные счета с истёкшим сроком.

    Нужна не только для порядка: пока счёт висит в ``pending``, он
    учитывается в лимите незавершённых счетов пользователя, и человек не
    может выставить новый.
    """

    name = "stale_invoice_cleanup"

    def __init__(self, uow_factory: UnitOfWorkFactory, settings: Settings) -> None:
        self._uow_factory = uow_factory
        self._batch_size = settings.worker.batch_size
        self.interval = float(settings.worker.invoice_cleanup_interval)

    async def run(self) -> TaskResult:
        """Просрочивает счета, срок действия которых вышел."""
        now = datetime.now(tz=timezone.utc)

        async with self._uow_factory() as uow:
            expired = await uow.payments.expire_stale_invoices(now=now, limit=self._batch_size)
            await uow.commit()

        if not expired:
            return TaskResult()

        result = TaskResult(processed=expired, succeeded=expired)
        logger.info("Просрочено неоплаченных счетов: %d", expired)
        return result
