"""Перенос накопленных переходов из Redis в PostgreSQL."""

from __future__ import annotations

from core.config import Settings
from core.logger import get_logger
from db.uow import UnitOfWorkFactory
from services.tracker import DEFAULT_FLUSH_BATCH, ClickCounter
from worker.tasks.base import PeriodicTask, TaskResult

logger = get_logger(__name__)


class ClickFlushTask(PeriodicTask):
    """Записывает переходы, накопленные редирект-сервером.

    Задача существует ровно затем, чтобы убрать запись в базу из ответа на
    редирект. Очередь разбирается пачками до тех пор, пока она не опустеет
    или не исчерпается отведённое число пачек: за минуту между прогонами
    их может накопиться заметно больше одной, а откладывать остаток на
    следующий раз значит наращивать отставание.
    """

    name = "click_flush"

    #: Сколько пачек переносить за один прогон. Ограничение защищает от
    #: бесконечного цикла, если события поступают быстрее, чем пишутся.
    MAX_BATCHES = 20

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        counter: ClickCounter,
        settings: Settings,
        batch_size: int = DEFAULT_FLUSH_BATCH,
    ) -> None:
        self._uow_factory = uow_factory
        self._counter = counter
        self._batch_size = batch_size
        self.interval = float(settings.worker.click_flush_interval)

    async def run(self) -> TaskResult:
        """Переносит очередь переходов в базу."""
        if not self._counter.enabled:
            return TaskResult()

        written = 0
        batches = 0

        for _ in range(self.MAX_BATCHES):
            events = await self._counter.drain(self._batch_size)
            if not events:
                break

            batches += 1
            async with self._uow_factory() as uow:
                written += await uow.links.apply_clicks(events)
                await uow.commit()

        if not written:
            return TaskResult()

        pending = await self._counter.pending()
        result = TaskResult(
            processed=written,
            succeeded=written,
            details={"batches": batches, "pending": pending},
        )
        logger.info(
            "Перенесено переходов: %d за %d пачек, в очереди осталось %d",
            written, batches, pending,
        )
        return result
