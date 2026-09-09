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

from typing import Any, Final

from services.ratelimit.base import RateLimitRule

#: Имя флага с правилом ограничения частоты.
RATE_LIMIT_FLAG: Final[str] = "rate_limit"

#: Имя флага, отключающего защиту от повторных нажатий.
NO_SINGLE_FLIGHT_FLAG: Final[str] = "no_single_flight"

#: Имя флага, полностью отключающего троттлинг для хендлера.
SKIP_THROTTLING_FLAG: Final[str] = "skip_throttling"


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
