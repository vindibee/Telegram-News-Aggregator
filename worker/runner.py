"""Планировщик фоновых задач.

Обёртка над APScheduler решает три вещи, которых нет из коробки:

* ошибка внутри задачи не должна ронять процесс и мешать остальным
  задачам — каждый прогон изолирован;
* два прогона одной задачи не должны накладываться, если предыдущий
  затянулся (``max_instances=1``);
* при остановке нужно дождаться текущего прогона, а не оборвать его на
  середине транзакции.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from core.logger import get_logger
from worker.tasks.base import PeriodicTask

logger = get_logger(__name__)

#: Доля интервала, на которую случайно смещается запуск.
#: Разброс нужен, чтобы реплики воркера не били в базу синхронно, и он
#: обязан быть пропорционален интервалу: фиксированные несколько секунд
#: ничего не разносят при интервале в четверть часа и полностью ломают
#: расписание при интервале в доли секунды.
_JITTER_RATIO = 0.1

#: Верхняя граница разброса, чтобы задача не откладывалась надолго.
_MAX_JITTER_SECONDS = 30

#: Сколько ждать завершения текущих прогонов при остановке.
_SHUTDOWN_TIMEOUT = 30.0

#: Насколько задача может опоздать и всё же выполниться.
_MISFIRE_GRACE_SECONDS = 30


class TaskRunner:
    """Запускает периодические задачи и следит за их выполнением."""

    def __init__(self, tasks: Sequence[PeriodicTask]) -> None:
        if not tasks:
            raise ValueError("Список задач воркера пуст.")
        self._tasks = tuple(tasks)
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._stopping = asyncio.Event()
        self._running: set[asyncio.Task[None]] = set()

    def schedule(self, *, run_immediately: bool = True) -> None:
        """Регистрирует задачи в планировщике.

        :param run_immediately: Выполнить первый прогон сразу после старта,
            не дожидаясь интервала. Полезно после перезапуска: за время
            простоя могла накопиться работа.
        """
        now = datetime.now(tz=timezone.utc)
        for index, task in enumerate(self._tasks):
            self._scheduler.add_job(
                self._run_task,
                trigger=IntervalTrigger(seconds=task.interval, jitter=self._jitter_for(task.interval)),
                args=(task,),
                id=task.name,
                name=task.name,
                # Прогоны одной задачи не накладываются: иначе затянувшийся
                # проход и следующий за ним конкурировали бы за одни строки.
                max_instances=1,
                coalesce=True,
                misfire_grace_time=_MISFIRE_GRACE_SECONDS,
                # Небольшая лесенка на старте, чтобы задачи не ударили в БД
                # одновременно.
                next_run_time=now + timedelta(seconds=index) if run_immediately else None,
            )
            logger.info(
                "Задача %s запланирована с интервалом %.0f с", task.name, task.interval
            )

    @staticmethod
    def _jitter_for(interval: float) -> int | None:
        """Вычисляет разброс запуска для указанного интервала.

        :param interval: Интервал задачи в секундах.
        :return: Разброс в секундах либо ``None``, если он вырождается в ноль.
        """
        jitter = min(int(interval * _JITTER_RATIO), _MAX_JITTER_SECONDS)
        return jitter or None

    async def start(self) -> None:
        """Запускает планировщик."""
        self._scheduler.start()
        logger.info("Планировщик запущен, задач: %d", len(self._tasks))

    async def stop(self, timeout: float = _SHUTDOWN_TIMEOUT) -> None:
        """Останавливает планировщик, дождавшись текущих прогонов.

        Порядок шагов принципиален. Сначала планировщик ставится на паузу —
        новые прогоны не стартуют, — и только потом дожидаются текущие.
        Вызвать ``shutdown`` первым нельзя: исполнитель APScheduler отменяет
        выполняющиеся корутины независимо от флага ``wait``, и задача
        оборвалась бы посреди транзакции.

        :param timeout: Сколько ждать завершения текущих прогонов. По
            истечении они прерываются принудительно, иначе зависшая задача
            не дала бы процессу остановиться.
        """
        self._stopping.set()
        self._scheduler.pause()

        pending = tuple(self._running)
        if pending:
            logger.info("Ожидаю завершения %d выполняющихся задач…", len(pending))
            _, unfinished = await asyncio.wait(pending, timeout=timeout)
            if unfinished:
                logger.error(
                    "%d задач не завершились за %.0f с, прерываю принудительно",
                    len(unfinished), timeout,
                )
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)

        self._scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен.")

    async def run_once(self, task: PeriodicTask) -> None:
        """Выполняет одну задачу вне расписания (ручной запуск и тесты)."""
        await self._run_task(task)

    async def _run_task(self, task: PeriodicTask) -> None:
        """Выполняет прогон задачи с изоляцией ошибок и учётом остановки."""
        if self._stopping.is_set():
            logger.debug("Пропуск задачи %s: воркер останавливается", task.name)
            return

        current = asyncio.current_task()
        if current is not None:
            self._running.add(current)

        started = datetime.now(tz=timezone.utc)
        try:
            result = await task.run()
        except asyncio.CancelledError:
            logger.warning("Задача %s прервана", task.name)
            raise
        except Exception:
            # Любая ошибка задачи локализуется: планировщик и остальные
            # задачи продолжают работать, а трассировка попадает в лог.
            logger.exception("Задача %s завершилась ошибкой", task.name)
        else:
            elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
            if not result.is_empty:
                logger.info("Задача %s: %s за %.2f с", task.name, result.describe(), elapsed)
            else:
                logger.debug("Задача %s: нет работы (%.2f с)", task.name, elapsed)
        finally:
            if current is not None:
                self._running.discard(current)
