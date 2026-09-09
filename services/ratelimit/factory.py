"""Сборка подсистемы ограничения частоты по конфигурации."""

from __future__ import annotations

from dataclasses import dataclass

from core.config import RateLimitConfig, RedisConfig
from core.logger import get_logger
from services.ratelimit.base import RateLimitBackend, RateLimitRule
from services.ratelimit.fallback import FallbackRateLimiter
from services.ratelimit.memory import InMemoryRateLimiter
from services.ratelimit.policy import AntiFloodConfig, AntiFloodPolicy

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitRules:
    """Правила частоты, применяемые по умолчанию."""

    message: RateLimitRule
    callback: RateLimitRule
    refresh: RateLimitRule


def build_rules(config: RateLimitConfig) -> RateLimitRules:
    """Создаёт правила из конфигурации.

    Сообщения и нажатия считаются раздельными вёдрами: переписка не должна
    исчерпывать лимит на кнопки и наоборот.
    """
    return RateLimitRules(
        message=RateLimitRule(
            limit=config.message_limit,
            window=config.message_window,
            burst=config.message_burst,
            scope="message",
        ),
        callback=RateLimitRule(
            limit=config.callback_limit,
            window=config.callback_window,
            burst=config.callback_burst,
            scope="callback",
        ),
        refresh=RateLimitRule(
            limit=config.refresh_limit,
            window=config.refresh_window,
            scope="refresh",
        ),
    )


def build_backend(redis_config: RedisConfig) -> RateLimitBackend:
    """Создаёт хранилище ограничителя.

    При настроенном Redis возвращается обёртка с локальным резервом: при
    недоступности Redis защита продолжит работать в пределах процесса, а не
    отключится целиком.

    :param redis_config: Параметры подключения к Redis.
    :return: Готовое хранилище.
    :raises RuntimeError: Redis настроен, но библиотека не установлена.
    """
    if not redis_config.enabled:
        logger.warning(
            "REDIS_URL не задан: ограничения работают в пределах процесса. "
            "Для нескольких реплик бота настройте Redis."
        )
        return InMemoryRateLimiter()

    try:
        from redis.asyncio import Redis

        from services.ratelimit.redis_limiter import RedisRateLimiter
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "REDIS_URL задан, но пакет redis не установлен: добавьте redis в зависимости."
        ) from exc

    client = Redis.from_url(redis_config.url, decode_responses=True)
    logger.info("Ограничитель частоты работает через Redis (префикс %s)", redis_config.prefix)
    return FallbackRateLimiter(RedisRateLimiter(client, prefix=redis_config.prefix))


def build_policy(backend: RateLimitBackend, config: RateLimitConfig) -> AntiFloodPolicy:
    """Создаёт политику анти-флуда поверх хранилища."""
    return AntiFloodPolicy(
        limiter=backend,
        guard=backend,
        config=AntiFloodConfig(
            violations_before_mute=config.violations_before_mute,
            violation_window=config.violation_window,
            mute_durations=config.mute_durations,
            warn_cooldown=config.warn_cooldown,
            mute_level_ttl=config.mute_level_ttl,
        ),
    )
