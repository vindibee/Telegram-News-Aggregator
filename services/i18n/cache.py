"""Кэш выбранного языка пользователя.

Язык нужен раньше всего остального: он определяет текст любого ответа,
включая отказы, которые выдаются до обращения к базе — например, при
срабатывании анти-флуда. Ходить за ним в PostgreSQL на каждом апдейте
незачем: значение меняется от силы раз в жизни аккаунта.

Кэш устроен так же, как ограничитель частоты: при настроенном Redis
работает он, иначе — словарь в памяти процесса. Второй вариант годится
для одного экземпляра бота; при нескольких репликах каждая будет иметь
собственную копию, и смена языка в одной не сразу отразится в другой.

Недоступность Redis не должна ронять обработку: язык — не критичные
данные, и потеря кэша означает лишний запрос к базе, а не ошибку.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Final

from core.config import RedisConfig
from core.logger import get_logger
from db.enums import Language

logger = get_logger(__name__)

#: Сколько хранить язык в кэше. Сутки: значение почти неизменно, а
#: ограничение нужно лишь чтобы забытые ключи не копились вечно.
DEFAULT_TTL_SECONDS: Final[int] = 24 * 60 * 60


class LanguageCache(ABC):
    """Хранилище «пользователь → язык интерфейса»."""

    @abstractmethod
    async def get(self, telegram_id: int) -> Language | None:
        """Возвращает язык пользователя либо ``None``, если он неизвестен."""

    @abstractmethod
    async def set(self, telegram_id: int, language: Language) -> None:
        """Запоминает язык пользователя."""

    @abstractmethod
    async def invalidate(self, telegram_id: int) -> None:
        """Забывает язык пользователя."""

    @abstractmethod
    async def close(self) -> None:
        """Освобождает ресурсы."""


class InMemoryLanguageCache(LanguageCache):
    """Кэш в памяти процесса."""

    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        if ttl <= 0:
            raise ValueError(f"TTL кэша должен быть положительным, получено: {ttl}")
        self._ttl = ttl
        self._values: dict[int, tuple[Language, float]] = {}

    async def get(self, telegram_id: int) -> Language | None:
        """Возвращает язык, если запись ещё не протухла."""
        entry = self._values.get(telegram_id)
        if entry is None:
            return None
        language, expires_at = entry
        if expires_at <= time.monotonic():
            del self._values[telegram_id]
            return None
        return language

    async def set(self, telegram_id: int, language: Language) -> None:
        """Запоминает язык пользователя."""
        self._values[telegram_id] = (language, time.monotonic() + self._ttl)

    async def invalidate(self, telegram_id: int) -> None:
        """Забывает язык пользователя."""
        self._values.pop(telegram_id, None)

    async def close(self) -> None:
        """Очищает состояние."""
        self._values.clear()


class RedisLanguageCache(LanguageCache):
    """Кэш поверх Redis — общий для всех реплик бота."""

    def __init__(self, client: object, *, prefix: str = "lang", ttl: int = DEFAULT_TTL_SECONDS):
        if ttl <= 0:
            raise ValueError(f"TTL кэша должен быть положительным, получено: {ttl}")
        self._client = client
        self._prefix = prefix
        self._ttl = ttl

    def _key(self, telegram_id: int) -> str:
        return f"{self._prefix}:lang:{telegram_id}"

    async def get(self, telegram_id: int) -> Language | None:
        """Возвращает язык из Redis.

        Любая ошибка сети трактуется как промах кэша: вызывающий код
        просто сходит в базу, и пользователь ничего не заметит.
        """
        from redis.exceptions import RedisError

        try:
            raw = await self._client.get(self._key(telegram_id))  # type: ignore[attr-defined]
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при чтении языка %s: %s", telegram_id, exc)
            return None

        if raw is None:
            return None

        value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        try:
            return Language(value)
        except ValueError:
            # В кэше значение из прошлой версии приложения — например,
            # язык, который больше не поддерживается.
            logger.warning("В кэше неизвестный язык %r для %s", value, telegram_id)
            await self.invalidate(telegram_id)
            return None

    async def set(self, telegram_id: int, language: Language) -> None:
        """Записывает язык в Redis с ограниченным сроком жизни."""
        from redis.exceptions import RedisError

        try:
            await self._client.set(  # type: ignore[attr-defined]
                self._key(telegram_id), language.value, ex=self._ttl
            )
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при записи языка %s: %s", telegram_id, exc)

    async def invalidate(self, telegram_id: int) -> None:
        """Удаляет запись из Redis."""
        from redis.exceptions import RedisError

        try:
            await self._client.delete(self._key(telegram_id))  # type: ignore[attr-defined]
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при сбросе языка %s: %s", telegram_id, exc)

    async def close(self) -> None:
        """Закрывает клиент вместе с пулом соединений."""
        from redis.exceptions import RedisError

        try:
            await self._client.aclose()  # type: ignore[attr-defined]
            await self._client.connection_pool.disconnect()  # type: ignore[attr-defined]
        except (RedisError, OSError, AttributeError) as exc:  # pragma: no cover
            logger.warning("Не удалось закрыть клиент Redis: %s", exc)


def build_language_cache(config: RedisConfig) -> LanguageCache:
    """Создаёт кэш языка по конфигурации.

    :param config: Параметры подключения к Redis.
    :return: Кэш поверх Redis либо локальный.
    :raises RuntimeError: Redis настроен, но библиотека не установлена.
    """
    if not config.enabled:
        logger.warning(
            "REDIS_URL не задан: язык кэшируется в памяти процесса. "
            "При нескольких репликах смена языка отразится не сразу."
        )
        return InMemoryLanguageCache()

    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "REDIS_URL задан, но пакет redis не установлен: добавьте redis в зависимости."
        ) from exc

    client = Redis.from_url(config.url, decode_responses=True)
    logger.info("Язык пользователей кэшируется в Redis (префикс %s)", config.prefix)
    return RedisLanguageCache(client, prefix=config.prefix)
