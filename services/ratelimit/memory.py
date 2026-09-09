"""Ограничитель частоты в памяти процесса.

Подходит для локальной разработки, тестов и как аварийный резерв, когда
Redis недоступен. Для нескольких реплик бота непригоден: у каждой будет
собственный счётчик, и суммарный лимит окажется кратно выше заявленного.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Final

from core.logger import get_logger
from services.ratelimit.base import RateLimitBackend, RateLimitDecision, RateLimitRule

logger = get_logger(__name__)

#: Как часто очищать протухшие записи (в вызовах).
_PRUNE_EVERY: Final[int] = 512


@dataclass(slots=True)
class _Bucket:
    """Состояние ведра жетонов."""

    tokens: float
    updated_at: float
    expires_at: float


class InMemoryRateLimiter(RateLimitBackend):
    """Ограничитель и блокировки поверх обычных словарей.

    Используется ``time.monotonic``: системные часы можно перевести назад,
    и тогда ведро «зависло» бы до следующего совпадения времени.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._guards: dict[str, tuple[str, float]] = {}
        self._counters: dict[str, tuple[int, float]] = {}
        self._calls = 0

    async def acquire(self, key: str, rule: RateLimitRule, cost: float = 1.0) -> RateLimitDecision:
        """Списывает жетоны по алгоритму token bucket."""
        if cost <= 0:
            raise ValueError(f"Стоимость операции должна быть положительной, получено: {cost}")

        now = time.monotonic()
        self._maybe_prune(now)

        bucket_key = self._bucket_key(key, rule)
        bucket = self._buckets.get(bucket_key)

        if bucket is None:
            bucket = _Bucket(tokens=rule.capacity, updated_at=now, expires_at=now + rule.ttl)
            self._buckets[bucket_key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(rule.capacity, bucket.tokens + elapsed * rule.rate)
            bucket.updated_at = now

        bucket.expires_at = now + rule.ttl

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return RateLimitDecision(allowed=True, remaining=bucket.tokens, retry_after=0.0)

        retry_after = (cost - bucket.tokens) / rule.rate
        return RateLimitDecision(allowed=False, remaining=bucket.tokens, retry_after=retry_after)

    async def reset(self, key: str, rule: RateLimitRule) -> None:
        """Удаляет ведро, возвращая полный лимит."""
        self._buckets.pop(self._bucket_key(key, rule), None)

    async def acquire_once(self, key: str, ttl: float) -> str | None:
        """Занимает ключ, если он свободен или его срок истёк."""
        if ttl <= 0:
            raise ValueError(f"TTL блокировки должен быть положительным, получено: {ttl}")

        now = time.monotonic()
        held = self._guards.get(key)
        if held is not None and held[1] > now:
            return None

        token = secrets.token_hex(8)
        self._guards[key] = (token, now + ttl)
        return token

    async def release(self, key: str, token: str) -> None:
        """Снимает блокировку только с предъявлением верного токена."""
        held = self._guards.get(key)
        if held is not None and held[0] == token:
            del self._guards[key]

    async def ttl(self, key: str) -> float:
        """Остаток времени блокировки в секундах."""
        held = self._guards.get(key)
        if held is None:
            return 0.0
        return max(0.0, held[1] - time.monotonic())

    async def bump(self, key: str, ttl: float) -> int:
        """Увеличивает счётчик, сбрасывая его после истечения срока."""
        if ttl <= 0:
            raise ValueError(f"TTL счётчика должен быть положительным, получено: {ttl}")

        now = time.monotonic()
        current = self._counters.get(key)
        value = current[0] + 1 if current is not None and current[1] > now else 1
        self._counters[key] = (value, now + ttl)
        return value

    async def close(self) -> None:
        """Очищает состояние."""
        self._buckets.clear()
        self._guards.clear()
        self._counters.clear()

    @staticmethod
    def _bucket_key(key: str, rule: RateLimitRule) -> str:
        return f"{rule.scope}:{key}"

    def _maybe_prune(self, now: float) -> None:
        """Периодически удаляет протухшие записи.

        Без очистки словари растут вместе с числом пользователей и живут
        столько же, сколько процесс.
        """
        self._calls += 1
        if self._calls % _PRUNE_EVERY:
            return

        stale_buckets = [key for key, bucket in self._buckets.items() if bucket.expires_at <= now]
        for key in stale_buckets:
            del self._buckets[key]

        stale_guards = [key for key, (_, expires_at) in self._guards.items() if expires_at <= now]
        for key in stale_guards:
            del self._guards[key]

        stale_counters = [key for key, (_, expires_at) in self._counters.items() if expires_at <= now]
        for key in stale_counters:
            del self._counters[key]

        if stale_buckets or stale_guards:
            logger.debug(
                "Очищено %d вёдер и %d блокировок", len(stale_buckets), len(stale_guards)
            )
