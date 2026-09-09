"""Контракты подсистемы ограничения частоты запросов.

Разделены два независимых примитива, чтобы потребители зависели только от
того, что реально используют (принцип разделения интерфейсов):

* :class:`RateLimiter` — «сколько операций в единицу времени» (алгоритм
  «дырявое ведро с жетонами»);
* :class:`KeyGuard` — «эта операция уже выполняется / уже была» (короткие
  блокировки с TTL для защиты от двойных нажатий).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

#: Запас к времени жизни ключа, чтобы ведро успело полностью восстановиться.
_TTL_SLACK_SECONDS: Final[float] = 1.0


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """Правило ограничения частоты.

    Формулируется в понятных величинах — «сколько запросов за какое окно», —
    а внутрь алгоритма передаётся уже как скорость пополнения и ёмкость.

    :param limit: Количество операций за окно.
    :param window: Длина окна в секундах.
    :param burst: Ёмкость ведра — сколько операций можно выполнить подряд.
        По умолчанию равна ``limit``. Значение больше ``limit`` разрешает
        короткие всплески, не поднимая среднюю скорость.
    :param scope: Имя ведра. Разные области не мешают друг другу: лимит на
        сообщения не расходуется нажатиями на кнопки.
    """

    limit: int
    window: float
    burst: int | None = None
    scope: str = "default"

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"limit должен быть не меньше 1, получено: {self.limit}")
        if self.window <= 0:
            raise ValueError(f"window должно быть положительным, получено: {self.window}")
        if self.burst is not None and self.burst < 1:
            raise ValueError(f"burst должен быть не меньше 1, получено: {self.burst}")

    @property
    def capacity(self) -> float:
        """Ёмкость ведра в жетонах."""
        return float(self.burst if self.burst is not None else self.limit)

    @property
    def rate(self) -> float:
        """Скорость пополнения, жетонов в секунду."""
        return self.limit / self.window

    @property
    def ttl(self) -> float:
        """Время жизни ключа: полное восстановление ведра плюс запас."""
        return self.capacity / self.rate + _TTL_SLACK_SECONDS


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Решение ограничителя по одной операции."""

    allowed: bool
    remaining: float
    retry_after: float

    @property
    def retry_after_seconds(self) -> int:
        """Время до следующей попытки, округлённое вверх до секунды."""
        return max(1, math.ceil(self.retry_after)) if self.retry_after > 0 else 0


class RateLimiter(ABC):
    """Ограничитель частоты операций."""

    @abstractmethod
    async def acquire(self, key: str, rule: RateLimitRule, cost: float = 1.0) -> RateLimitDecision:
        """Пытается списать жетоны за операцию.

        :param key: Ключ ведра (обычно идентификатор пользователя).
        :param rule: Применяемое правило.
        :param cost: Стоимость операции в жетонах.
        :return: Решение с остатком и временем до следующей попытки.
        """

    @abstractmethod
    async def reset(self, key: str, rule: RateLimitRule) -> None:
        """Сбрасывает состояние ведра (например, после разбана)."""

    @abstractmethod
    async def close(self) -> None:
        """Освобождает ресурсы."""


class KeyGuard(ABC):
    """Короткие блокировки по ключу.

    Используются двояко: как «одиночный запуск» (взять на время обработки и
    отпустить в ``finally``) и как «сделать не чаще раза в N секунд» —
    во втором случае блокировка не освобождается и истекает сама.
    """

    @abstractmethod
    async def acquire_once(self, key: str, ttl: float) -> str | None:
        """Занимает ключ, если он свободен.

        :param key: Ключ блокировки.
        :param ttl: Время жизни в секундах.
        :return: Токен владения либо ``None``, если ключ уже занят.
        """

    @abstractmethod
    async def release(self, key: str, token: str) -> None:
        """Освобождает ключ, если владелец — предъявитель токена.

        Проверка токена обязательна: без неё запоздавший обработчик снял бы
        чужую блокировку, взятую после истечения его собственной.
        """

    @abstractmethod
    async def ttl(self, key: str) -> float:
        """Сколько секунд осталось до снятия блокировки.

        :return: Остаток в секундах; ``0.0``, если ключ свободен.
        """

    @abstractmethod
    async def bump(self, key: str, ttl: float) -> int:
        """Увеличивает счётчик по ключу и продлевает его жизнь.

        Нужен для эскалации наказаний: номер нарушения определяется
        значением счётчика, а сам счётчик забывается через ``ttl``.

        :return: Значение счётчика после увеличения (начиная с 1).
        """


class RateLimitBackend(RateLimiter, KeyGuard, ABC):
    """Хранилище, реализующее оба примитива.

    Существует потому, что бэкенды (Redis, память) естественно умеют и то и
    другое, а вот потребители по-прежнему объявляют зависимость от узкого
    интерфейса — :class:`RateLimiter` или :class:`KeyGuard`.
    """
