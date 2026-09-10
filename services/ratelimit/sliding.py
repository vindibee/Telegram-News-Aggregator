"""Скользящее окно — вторая стратегия ограничения частоты.

Отличие от ведра с жетонами не косметическое, и выбирать между ними стоит
осознанно.

Ведро восстанавливается непрерывно: потратив весь лимит, пользователь уже
через долю секунды получает один жетон и может сделать ещё одну операцию.
Это удобно для живого диалога, но означает, что строгое «не больше N за
окно» им не выражается — за минуту сквозь ведро на 20 жетонов проходит
заметно больше двадцати запросов.

Скользящее окно хранит отметки самих обращений и отвечает буквально на
вопрос «сколько их было за последние W секунд». Ровно N — и ни одним
больше, пока самое старое обращение не выпадет из окна. Это дороже по
памяти (одна запись на обращение против двух чисел на ключ), зато даёт
предсказуемую верхнюю границу — то, что нужно для жёстких интервалов
вроде «не чаще раза в полсекунды» и для дорогих операций.

Отметки хранятся в отсортированном множестве Redis, где ключ сортировки —
время обращения в миллисекундах. Вся работа делается одним Lua-скриптом:
последовательность «почистить старое — посчитать — добавить» отдельными
командами не атомарна, и два одновременных запроса насчитали бы одно и то
же количество, пропустив оба.
"""

from __future__ import annotations

import math
import secrets
import time
from collections import deque
from typing import Any, Final

from redis.asyncio import Redis

from core.logger import get_logger
from services.ratelimit.base import RateLimiter, RateLimitDecision, RateLimitRule

logger = get_logger(__name__)

#: Скользящее окно на отсортированном множестве.
#:
#: Время берётся из самого Redis: часы реплик бота расходятся, и отметка,
#: поставленная «убежавшей вперёд» репликой, выпала бы из окна позже, чем
#: должна.
#:
#: Каждому обращению нужен свой уникальный член множества — иначе ``ZADD``
#: перезаписал бы существующую отметку вместо добавления новой, и окно
#: считало бы один запрос вместо десяти.
_SLIDING_WINDOW_LUA: Final[str] = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local token = ARGV[5]

local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
local used = redis.call('ZCARD', key)

if used + cost <= limit then
    for i = 1, cost do
        redis.call('ZADD', key, now_ms, token .. ':' .. i)
    end
    redis.call('PEXPIRE', key, ttl_ms)
    return {1, limit - used - cost, 0}
end

-- Место освободится, когда из окна выпадет обращение, стоящее на позиции
-- cost с начала: именно оно мешает разместить запрошенное количество.
local blocking = redis.call('ZRANGE', key, cost - 1, cost - 1, 'WITHSCORES')
local retry_ms = 0
if blocking[2] ~= nil then
    retry_ms = window_ms - (now_ms - tonumber(blocking[2]))
    if retry_ms < 0 then
        retry_ms = 0
    end
end

redis.call('PEXPIRE', key, ttl_ms)
return {0, limit - used, retry_ms}
"""


def validate_cost(cost: float) -> int:
    """Приводит стоимость операции к числу отметок в окне.

    Скользящее окно считает сами обращения, а не делимый ресурс, поэтому
    дробная стоимость в нём не определена. Округлять молча нельзя:
    ``cost=0.5`` почти наверняка означает ошибку в вызывающем коде.

    :param cost: Стоимость операции.
    :return: Целое число отметок.
    :raises ValueError: Стоимость не положительна или дробная.
    """
    if cost <= 0:
        raise ValueError(f"Стоимость операции должна быть положительной, получено: {cost}")
    if abs(cost - round(cost)) > 1e-9:
        raise ValueError(
            f"Скользящее окно считает обращения поштучно, дробная стоимость недопустима: {cost}"
        )
    return int(round(cost))


class RedisSlidingWindow(RateLimiter):
    """Скользящее окно поверх Redis.

    Пригодно для нескольких реплик бота: множество отметок общее, поэтому
    лимит соблюдается суммарно, а не отдельно в каждом процессе.
    """

    def __init__(self, client: Redis, prefix: str = "sw") -> None:
        self._client = client
        self._prefix = prefix.rstrip(":")
        self._script = client.register_script(_SLIDING_WINDOW_LUA)

    async def acquire(self, key: str, rule: RateLimitRule, cost: float = 1.0) -> RateLimitDecision:
        """Регистрирует обращение, если окно не переполнено.

        :param key: Ключ окна (обычно идентификатор пользователя).
        :param rule: Применяемое правило.
        :param cost: Сколько отметок занимает операция.
        :return: Решение с остатком и временем до следующей попытки.
        :raises ValueError: Некорректная стоимость.
        :raises redis.exceptions.RedisError: Redis недоступен.
        """
        slots = validate_cost(cost)

        if slots > rule.limit:
            # Операция не поместится в окно никогда, сколько ни жди.
            # Возвращать «повторите через столько-то» здесь было бы ложью.
            logger.error(
                "Операция стоимостью %d не помещается в окно на %d обращений (%s)",
                slots, rule.limit, rule.scope,
            )
            return RateLimitDecision(allowed=False, remaining=0.0, retry_after=0.0)

        raw: Any = await self._script(
            keys=[self._window_key(key, rule)],
            args=[
                rule.limit,
                int(rule.window * 1000),
                int(window_ttl(rule) * 1000),
                slots,
                secrets.token_hex(8),
            ],
        )
        return RateLimitDecision(
            allowed=bool(int(raw[0])),
            remaining=float(raw[1]),
            retry_after=float(raw[2]) / 1000,
        )

    async def reset(self, key: str, rule: RateLimitRule) -> None:
        """Очищает окно, возвращая полный лимит."""
        await self._client.delete(self._window_key(key, rule))

    async def close(self) -> None:
        """Ничего не освобождает: клиент Redis общий и живёт дольше.

        Закрыть его здесь значило бы оборвать соединение у всех
        остальных потребителей — ограничителя частоты, кэша языка,
        очереди переходов.
        """
        return None

    def _window_key(self, key: str, rule: RateLimitRule) -> str:
        """Строит ключ окна с учётом области действия правила."""
        return f"{self._prefix}:{rule.scope}:{key}"


class InMemorySlidingWindow(RateLimiter):
    """Скользящее окно в памяти процесса.

    Резерв на случай недоступности Redis и рабочий вариант для одной
    реплики. Отметки хранятся в двусторонней очереди: старые снимаются с
    головы, новые добавляются в хвост — обе операции за константное время.

    Время берётся монотонное: перевод системных часов назад (синхронизация
    NTP, смена времени на сервере) иначе заморозил бы окно.
    """

    def __init__(self, max_keys: int = 100_000) -> None:
        if max_keys < 1:
            raise ValueError(f"Лимит ключей должен быть положительным, получено: {max_keys}")
        self._windows: dict[str, deque[float]] = {}
        self._max_keys = max_keys

    async def acquire(self, key: str, rule: RateLimitRule, cost: float = 1.0) -> RateLimitDecision:
        """Регистрирует обращение, если окно не переполнено."""
        slots = validate_cost(cost)

        if slots > rule.limit:
            return RateLimitDecision(allowed=False, remaining=0.0, retry_after=0.0)

        now = time.monotonic()
        window_key = f"{rule.scope}:{key}"
        marks = self._windows.get(window_key)

        if marks is None:
            marks = deque()
            self._evict_if_needed()
            self._windows[window_key] = marks

        cutoff = now - rule.window
        while marks and marks[0] <= cutoff:
            marks.popleft()

        if len(marks) + slots <= rule.limit:
            marks.extend([now] * slots)
            return RateLimitDecision(
                allowed=True, remaining=float(rule.limit - len(marks)), retry_after=0.0
            )

        # Мешает обращение на позиции slots-1: пока оно не выпадет,
        # запрошенное количество отметок не разместить.
        blocking = marks[slots - 1] if len(marks) >= slots else marks[0]
        retry_after = max(0.0, rule.window - (now - blocking))
        return RateLimitDecision(
            allowed=False, remaining=float(rule.limit - len(marks)), retry_after=retry_after
        )

    async def reset(self, key: str, rule: RateLimitRule) -> None:
        """Очищает окно."""
        self._windows.pop(f"{rule.scope}:{key}", None)

    async def close(self) -> None:
        """Отпускает память под окнами."""
        self._windows.clear()

    def _evict_if_needed(self) -> None:
        """Не даёт словарю окон расти без границы.

        Пустые окна удаляются первыми: они остаются от пользователей,
        которые давно ничего не делали. Если и после этого места нет,
        выбрасывается самое старое по времени добавления — словари Python
        сохраняют порядок вставки, поэтому это первый ключ.
        """
        if len(self._windows) < self._max_keys:
            return

        stale = [name for name, marks in self._windows.items() if not marks]
        for name in stale:
            del self._windows[name]

        while len(self._windows) >= self._max_keys:
            self._windows.pop(next(iter(self._windows)))
            logger.warning("Достигнут предел окон в памяти, вытесняю самое старое")

    @property
    def tracked_keys(self) -> int:
        """Сколько окон хранится сейчас — для диагностики."""
        return len(self._windows)


def window_ttl(rule: RateLimitRule) -> float:
    """Сколько держать ключ окна после последнего обращения.

    Ровно длина окна плюс запас: раньше удалять нельзя — отметки ещё
    влияют на решение, позже незачем — они уже все просрочены.
    """
    return rule.window + 1.0


def describe(rule: RateLimitRule) -> str:
    """Человекочитаемое описание правила — для логов и отладки."""
    per = rule.window
    if math.isclose(per, round(per)):
        per_text = f"{int(round(per))} с"
    else:
        per_text = f"{per:.1f} с"
    return f"{rule.limit} обращ. / {per_text} ({rule.scope})"


__all__ = [
    "InMemorySlidingWindow",
    "RedisSlidingWindow",
    "describe",
    "validate_cost",
    "window_ttl",
]
