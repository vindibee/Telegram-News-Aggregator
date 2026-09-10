"""Флаги хендлеров.

Флаги — штатный способ aiogram 3 передать middleware настройки конкретного
хендлера. Благодаря им лимит объявляется рядом с обработчиком, которого
касается, а не в разросшемся словаре «имя хендлера → правило» где-то в
стороне.

Читать флаги умеет только *внутренний* (inner) middleware: внешний
выполняется до того, как диспетчер выбрал хендлер, и знать его флагов не
может.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from services.ratelimit.base import RateLimitRule

#: Имя флага с правилом ограничения частоты.
RATE_LIMIT_FLAG: Final[str] = "rate_limit"

#: Имя флага, отключающего защиту от повторных нажатий.
NO_SINGLE_FLIGHT_FLAG: Final[str] = "no_single_flight"

#: Имя флага, полностью отключающего троттлинг для хендлера.
SKIP_THROTTLING_FLAG: Final[str] = "skip_throttling"

#: Имя флага критического действия, защищаемого распределённой блокировкой.
CRITICAL_FLAG: Final[str] = "critical_action"


def rate_limit(
    limit: int,
    window: float,
    *,
    burst: int | None = None,
    scope: str = "default",
) -> dict[str, Any]:
    """Строит флаги с индивидуальным правилом частоты для хендлера.

    Использование::

        @router.callback_query(RefreshCB.filter(), **rate_limit(3, 60, scope="refresh"))
        async def refresh(...): ...

    :param limit: Количество операций за окно.
    :param window: Длина окна в секундах.
    :param burst: Ёмкость ведра (по умолчанию равна ``limit``).
    :param scope: Имя ведра — изолирует лимит от остальных.
    :return: Словарь с ключом ``flags`` для передачи в регистрацию хендлера.
    :raises ValueError: Некорректные параметры правила.
    """
    rule = RateLimitRule(limit=limit, window=window, burst=burst, scope=scope)
    return {"flags": {RATE_LIMIT_FLAG: rule}}


def merge(*parts: dict[str, Any]) -> dict[str, Any]:
    """Объединяет несколько наборов флагов в один.

    Нужна потому, что каждый помощник возвращает словарь с одним и тем же
    ключом ``flags``, и распаковать два таких словаря в один вызов нельзя —
    Python отвергнет повторяющийся именованный аргумент.

    Использование::

        @router.callback_query(
            PayMethodCB.filter(),
            **merge(rate_limit(5, 60, scope="invoice"), critical("invoice")),
        )
        async def send_invoice(...): ...

    :param parts: Наборы флагов от помощников этого модуля.
    :return: Единый словарь с ключом ``flags``.
    """
    merged: dict[str, Any] = {}
    for part in parts:
        merged.update(part.get("flags", {}))
    return {"flags": merged}


def skip_throttling() -> dict[str, Any]:
    """Отключает проверку частоты для хендлера.

    Нужен для служебных обработчиков — например, ответа на
    ``PreCheckoutQuery``, где задержка недопустима: Telegram ждёт ответ
    не дольше десяти секунд.
    """
    return {"flags": {SKIP_THROTTLING_FLAG: True}}


def no_single_flight() -> dict[str, Any]:
    """Разрешает параллельную обработку одинаковых нажатий.

    Применяется к дешёвым хендлерам-переключателям, где повторное нажатие
    безвредно, а блокировка только мешает.
    """
    return {"flags": {NO_SINGLE_FLIGHT_FLAG: True}}


@dataclass(frozen=True, slots=True)
class CriticalActionFlag:
    """Содержимое флага критического действия.

    Имя и пауза разделены намеренно: имя задаёт область блокировки (кто с
    кем её делит), а пауза — насколько долго не принимать повтор после
    успеха. Их часто хочется настроить независимо.
    """

    name: str
    cooldown: float | None = None


def critical(name: str, *, cooldown: float | None = None) -> dict[str, Any]:
    """Помечает хендлер как критическое действие.

    Критическим считается то, что нельзя выполнить дважды по ошибке:
    выставление счёта, активация пробного периода, запуск рассылки.
    Защита от двойного нажатия здесь строже обычной — блокировка держится
    и некоторое время после успешного завершения, потому что повторный
    тап приходит уже после того, как хендлер отработал.

    Использование::

        @router.callback_query(PlanCB.filter(), **critical("invoice"))
        async def create_invoice(...): ...

    :param name: Имя действия. Хендлеры с одним именем делят блокировку —
        два способа оплаты одного тарифа не должны запускаться разом.
    :param cooldown: Сколько секунд не принимать повтор после успеха;
        ``None`` — значение по умолчанию для всех критических действий.
    :return: Словарь с ключом ``flags`` для передачи в регистрацию хендлера.
    :raises ValueError: Пустое имя или неположительная пауза.
    """
    if not name.strip():
        raise ValueError("Имя критического действия не может быть пустым.")
    if cooldown is not None and cooldown <= 0:
        raise ValueError(f"Пауза должна быть положительной, получено: {cooldown}")

    return {"flags": {CRITICAL_FLAG: CriticalActionFlag(name=name.strip(), cooldown=cooldown)}}
