"""Ограничение частоты «тяжёлых» операций (антиспам)."""

from __future__ import annotations

import time
from typing import Hashable


class CooldownStorage:
    """Кулдаун в памяти процесса с самоочисткой.

    Наивный ``dict`` без вытеснения рос бы бесконечно вместе с числом
    пользователей; здесь просроченные записи удаляются при каждом обращении,
    поэтому размер хранилища ограничен числом *активных* ключей.

    Хранилище процесс-локальное: при горизонтальном масштабировании бота его
    следует заменить на Redis, сохранив этот же интерфейс.
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = max(0.0, ttl)
        # monotonic, а не time(): не зависит от перевода системных часов.
        self._entries: dict[Hashable, float] = {}

    def remaining(self, key: Hashable) -> float:
        """Сколько секунд осталось до снятия ограничения (0.0 — можно выполнять)."""
        if self._ttl <= 0:
            return 0.0

        now = time.monotonic()
        self._prune(now)

        started_at = self._entries.get(key)
        if started_at is None:
            return 0.0
        return max(0.0, self._ttl - (now - started_at))

    def touch(self, key: Hashable) -> None:
        """Запускает отсчёт кулдауна для ключа."""
        if self._ttl > 0:
            self._entries[key] = time.monotonic()

    def _prune(self, now: float) -> None:
        expired = [key for key, started_at in self._entries.items() if now - started_at >= self._ttl]
        for key in expired:
            del self._entries[key]
