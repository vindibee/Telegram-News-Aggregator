"""Ограничитель частоты поверх Redis.

Единственная реализация, пригодная для нескольких реплик бота: состояние
вёдер общее, поэтому лимит соблюдается суммарно, а не по каждому процессу.

Вся арифметика ведра выполняется одним Lua-скриптом. Это принципиально:
последовательность «прочитать — посчитать — записать» отдельными командами
не атомарна, и два одновременных запроса прочитали бы одно и то же
количество жетонов, списав каждый своё — лимит превышался бы ровно в тех
условиях, ради которых он и вводится.
"""

from __future__ import annotations

import secrets
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.logger import get_logger
from services.ratelimit.base import RateLimitBackend, RateLimitDecision, RateLimitRule

logger = get_logger(__name__)

#: Token bucket. Время берётся из самого Redis: часы реплик бота могут
#: расходиться, и клиентское время сделало бы лимит неравномерным.
_TOKEN_BUCKET_LUA: Final[str] = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local server_time = redis.call('TIME')
local now = tonumber(server_time[1]) + tonumber(server_time[2]) / 1000000

local stored = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(stored[1])
local ts = tonumber(stored[2])

if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then
    elapsed = 0
end
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    retry_after = (cost - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, ttl_ms)

return {allowed, tostring(tokens), tostring(retry_after)}
"""

#: Снятие блокировки с проверкой владельца.
_RELEASE_LUA: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisRateLimiter(RateLimitBackend):
    """Ограничитель и распределённые блокировки на Redis."""

    def __init__(self, client: Redis, prefix: str = "rl") -> None:
        self._client = client
        self._prefix = prefix.rstrip(":")
        self._token_bucket = client.register_script(_TOKEN_BUCKET_LUA)
        self._release_script = client.register_script(_RELEASE_LUA)

    async def acquire(self, key: str, rule: RateLimitRule, cost: float = 1.0) -> RateLimitDecision:
        """Списывает жетоны атомарно на стороне Redis.

        :raises redis.exceptions.RedisError: Redis недоступен. Обработку
            отказа берёт на себя :class:`~services.ratelimit.fallback.FallbackRateLimiter`.
        :raises ValueError: Некорректная стоимость операции.
        """
        if cost <= 0:
            raise ValueError(f"Стоимость операции должна быть положительной, получено: {cost}")

        raw: Any = await self._token_bucket(
            keys=[self._bucket_key(key, rule)],
            args=[rule.capacity, rule.rate, cost, int(rule.ttl * 1000)],
        )
        allowed = bool(int(raw[0]))
        remaining = float(raw[1])
        retry_after = float(raw[2])
        return RateLimitDecision(allowed=allowed, remaining=remaining, retry_after=retry_after)

    async def reset(self, key: str, rule: RateLimitRule) -> None:
        """Удаляет ведро, возвращая полный лимит."""
        await self._client.delete(self._bucket_key(key, rule))

    async def acquire_once(self, key: str, ttl: float) -> str | None:
        """Занимает ключ через ``SET NX PX``.

        Команда атомарна сама по себе, поэтому Lua здесь не нужен.
        """
        if ttl <= 0:
            raise ValueError(f"TTL блокировки должен быть положительным, получено: {ttl}")

        token = secrets.token_hex(8)
        was_set = await self._client.set(
            self._guard_key(key), token, nx=True, px=int(ttl * 1000)
        )
        return token if was_set else None

    async def release(self, key: str, token: str) -> None:
        """Снимает блокировку, только если токен совпадает.

        Сравнение и удаление выполняются одним скриптом: между отдельными
        GET и DEL блокировка могла бы истечь и достаться другому обработчику,
        которому мы бы её и сняли.
        """
        try:
            await self._release_script(keys=[self._guard_key(key)], args=[token])
        except RedisError as exc:
            # Не критично: блокировка исчезнет сама по истечении TTL.
            logger.warning("Не удалось снять блокировку %s: %s", key, exc)

    async def ttl(self, key: str) -> float:
        """Остаток времени блокировки по данным Redis."""
        remaining_ms = await self._client.pttl(self._guard_key(key))
        # -2 — ключа нет, -1 — ключ без срока жизни.
        return remaining_ms / 1000 if remaining_ms and remaining_ms > 0 else 0.0

    async def bump(self, key: str, ttl: float) -> int:
        """Увеличивает счётчик и продлевает срок его жизни.

        INCR и EXPIRE отправляются одним конвейером: два round-trip на
        каждое нарушение — лишняя задержка в горячем пути.
        """
        if ttl <= 0:
            raise ValueError(f"TTL счётчика должен быть положительным, получено: {ttl}")

        counter_key = f"{self._prefix}:counter:{key}"
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(counter_key)
            pipe.pexpire(counter_key, int(ttl * 1000))
            value, _ = await pipe.execute()
        return int(value)

    async def close(self) -> None:
        """Закрывает соединение с Redis."""
        await self._client.aclose()

    def _bucket_key(self, key: str, rule: RateLimitRule) -> str:
        return f"{self._prefix}:bucket:{rule.scope}:{key}"

    def _guard_key(self, key: str) -> str:
        return f"{self._prefix}:guard:{key}"
