"""Ограничитель с деградацией на локальное хранилище.

Если Redis перестал отвечать, у нас два плохих варианта: пропускать всё
подряд или отказывать всем. Оба хуже третьего — временно перейти на
локальные счётчики: лимит перестанет быть общим для реплик, но защита от
флуда в пределах процесса продолжит работать.

Чтобы не платить таймаутом за каждый запрос при длительной недоступности
Redis, включён размыкатель цепи: после серии ошибок обращения к нему
прекращаются на короткую паузу.
"""

from __future__ import annotations

import time
from typing import Final

from redis.exceptions import RedisError

from core.logger import get_logger
from services.ratelimit.base import RateLimitBackend, RateLimitDecision, RateLimitRule
from services.ratelimit.memory import InMemoryRateLimiter

logger = get_logger(__name__)

#: Сколько подряд ошибок размыкают цепь.
_FAILURE_THRESHOLD: Final[int] = 3
#: На сколько секунд Redis исключается из обращения.
_OPEN_STATE_SECONDS: Final[float] = 15.0


class FallbackRateLimiter(RateLimitBackend):
    """Обёртка «Redis с локальным резервом»."""

    def __init__(
        self,
        primary: RateLimitBackend,
        fallback: InMemoryRateLimiter | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or InMemoryRateLimiter()
        self._failures = 0
        self._open_until = 0.0

    @property
    def degraded(self) -> bool:
        """Работает ли ограничитель сейчас на локальном резерве."""
        return time.monotonic() < self._open_until

    async def acquire(self, key: str, rule: RateLimitRule, cost: float = 1.0) -> RateLimitDecision:
        """Списывает жетоны через Redis, при отказе — локально."""
        if self.degraded:
            return await self._fallback.acquire(key, rule, cost)

        try:
            decision = await self._primary.acquire(key, rule, cost)
        except RedisError as exc:
            self._register_failure(exc)
            return await self._fallback.acquire(key, rule, cost)

        self._register_success()
        return decision

    async def reset(self, key: str, rule: RateLimitRule) -> None:
        """Сбрасывает ведро в обоих хранилищах."""
        await self._fallback.reset(key, rule)
        if self.degraded:
            return
        try:
            await self._primary.reset(key, rule)
        except RedisError as exc:
            self._register_failure(exc)

    async def acquire_once(self, key: str, ttl: float) -> str | None:
        """Берёт блокировку через Redis, при отказе — локально."""
        if self.degraded:
            return await self._fallback.acquire_once(key, ttl)

        try:
            token = await self._primary.acquire_once(key, ttl)
        except RedisError as exc:
            self._register_failure(exc)
            return await self._fallback.acquire_once(key, ttl)

        self._register_success()
        return token

    async def release(self, key: str, token: str) -> None:
        """Снимает блокировку в обоих хранилищах."""
        await self._fallback.release(key, token)
        if self.degraded:
            return
        try:
            await self._primary.release(key, token)
        except RedisError as exc:
            self._register_failure(exc)

    async def ttl(self, key: str) -> float:
        """Остаток блокировки; при отказе Redis — по локальным данным."""
        if self.degraded:
            return await self._fallback.ttl(key)
        try:
            value = await self._primary.ttl(key)
        except RedisError as exc:
            self._register_failure(exc)
            return await self._fallback.ttl(key)
        self._register_success()
        return value

    async def bump(self, key: str, ttl: float) -> int:
        """Увеличивает счётчик; при отказе Redis — локально."""
        if self.degraded:
            return await self._fallback.bump(key, ttl)
        try:
            value = await self._primary.bump(key, ttl)
        except RedisError as exc:
            self._register_failure(exc)
            return await self._fallback.bump(key, ttl)
        self._register_success()
        return value

    async def close(self) -> None:
        """Закрывает оба хранилища."""
        await self._fallback.close()
        await self._primary.close()

    def _register_failure(self, exc: BaseException) -> None:
        self._failures += 1
        if self._failures >= _FAILURE_THRESHOLD and not self.degraded:
            self._open_until = time.monotonic() + _OPEN_STATE_SECONDS
            logger.error(
                "Redis недоступен (%s), ограничитель переведён на локальный резерв на %.0f с",
                exc, _OPEN_STATE_SECONDS,
            )
        else:
            logger.warning("Ошибка обращения к Redis в ограничителе: %s", exc)

    def _register_success(self) -> None:
        if self._failures:
            logger.info("Redis снова доступен, ограничитель вернулся к общему состоянию")
        self._failures = 0
        self._open_until = 0.0
