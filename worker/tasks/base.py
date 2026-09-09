"""Контракт фоновой задачи."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Итог одного прогона задачи.

    Возвращается значением, а не пишется только в лог: планировщик
    использует эти числа для сводки, а тесты — для проверок.
    """

    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Не нашлось ли работы в этом прогоне."""
        return self.processed == 0

    def describe(self) -> str:
        """Краткое описание для лога."""
        if self.is_empty:
            return "нет работы"
        return f"обработано {self.processed}, успешно {self.succeeded}, с ошибками {self.failed}"


class PeriodicTask(ABC):
    """Периодическая фоновая задача.

    Задача обязана быть идемпотентной и безопасной при параллельном
    запуске: воркер может работать в нескольких репликах, а прогон —
    прерваться и повториться.
    """

    #: Имя для логов и идентификатора задания в планировщике.
    name: str = "unnamed"

    #: Интервал запуска по умолчанию, секунды.
    interval: float = 60.0

    @abstractmethod
    async def run(self) -> TaskResult:
        """Выполняет один прогон.

        :return: Сводка по обработанным записям.
        """
