"""Политика анти-флуда.

Здесь принимается решение «пропустить, придержать или заглушить», а
middleware только переводит это решение на язык aiogram. Разделение даёт
две вещи: политику можно проверить без Telegram, а middleware остаётся
тонким адаптером.

Схема эскалации: превышение лимита само по себе наказанием не считается —
человек мог случайно нажать кнопку дважды. Но если нарушения повторяются,
пользователь получает временную заглушку, длительность которой растёт с
каждым разом. Это отсекает автоматический флуд, не мешая живым людям.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final

from core.logger import get_logger
from services.ratelimit.base import KeyGuard, RateLimiter, RateLimitRule

logger = get_logger(__name__)

#: Область ведра, считающего сами нарушения.
_VIOLATION_SCOPE: Final[str] = "violation"


class FloodAction(StrEnum):
    """Что делать с обновлением."""

    PASS = auto()
    THROTTLE = auto()
    MUTE = auto()


@dataclass(frozen=True, slots=True)
class FloodVerdict:
    """Решение политики по одному обновлению."""

    action: FloodAction
    retry_after: int = 0
    notify: bool = False

    @property
    def blocked(self) -> bool:
        """Нужно ли прервать обработку обновления."""
        return self.action is not FloodAction.PASS


@dataclass(frozen=True, slots=True)
class AntiFloodConfig:
    """Настройки эскалации наказаний."""

    #: Сколько нарушений в окне допустимо до заглушки.
    violations_before_mute: int = 5
    #: Окно подсчёта нарушений, секунды.
    violation_window: float = 60.0
    #: Длительности заглушки по номеру нарушения, секунды.
    mute_durations: tuple[int, ...] = (30, 120, 600)
    #: Не чаще одного предупреждения в этот интервал, секунды.
    warn_cooldown: float = 10.0
    #: Сколько помнить номер нарушения для эскалации, секунды.
    mute_level_ttl: float = 3600.0

    def __post_init__(self) -> None:
        if self.violations_before_mute < 1:
            raise ValueError("violations_before_mute должен быть не меньше 1.")
        if self.violation_window <= 0:
            raise ValueError("violation_window должно быть положительным.")
        if not self.mute_durations or any(value <= 0 for value in self.mute_durations):
            raise ValueError("mute_durations должен содержать положительные значения.")
        if self.warn_cooldown <= 0:
            raise ValueError("warn_cooldown должен быть положительным.")
        if self.mute_level_ttl <= 0:
            raise ValueError("mute_level_ttl должен быть положительным.")


class AntiFloodPolicy:
    """Решает судьбу обновления по частоте обращений пользователя."""

    def __init__(
        self,
        limiter: RateLimiter,
        guard: KeyGuard,
        config: AntiFloodConfig | None = None,
    ) -> None:
        self._limiter = limiter
        self._guard = guard
        self._config = config or AntiFloodConfig()
        self._violation_rule = RateLimitRule(
            limit=self._config.violations_before_mute,
            window=self._config.violation_window,
            scope=_VIOLATION_SCOPE,
        )

    async def check(self, user_id: int, rule: RateLimitRule) -> FloodVerdict:
        """Проверяет обновление по правилу и текущему состоянию наказаний.

        :param user_id: Пользователь Telegram.
        :param rule: Применяемое правило частоты.
        :return: Решение о пропуске, задержке или заглушке.
        """
        mute_left = await self._mute_remaining(user_id)
        if mute_left > 0:
            # Заглушённому не отвечаем на каждое сообщение: смысл заглушки в
            # том, чтобы перестать реагировать вовсе.
            return FloodVerdict(action=FloodAction.MUTE, retry_after=mute_left, notify=False)

        decision = await self._limiter.acquire(str(user_id), rule)
        if decision.allowed:
            return FloodVerdict(action=FloodAction.PASS)

        should_mute = await self._register_violation(user_id)
        if should_mute:
            duration = await self._apply_mute(user_id, rule)
            logger.warning(
                "Пользователь %s заглушён на %d с за систематический флуд", user_id, duration
            )
            return FloodVerdict(action=FloodAction.MUTE, retry_after=duration, notify=True)

        notify = await self._should_warn(user_id)
        logger.info(
            "Превышен лимит %s пользователем %s, повтор через %d с",
            rule.scope, user_id, decision.retry_after_seconds,
        )
        return FloodVerdict(
            action=FloodAction.THROTTLE,
            retry_after=decision.retry_after_seconds,
            notify=notify,
        )

    async def reset(self, user_id: int, rule: RateLimitRule) -> None:
        """Снимает наказания и обнуляет счётчики (действие администратора)."""
        await self._limiter.reset(str(user_id), rule)
        await self._limiter.reset(str(user_id), self._violation_rule)
        logger.info("Ограничения пользователя %s сброшены", user_id)

    async def _register_violation(self, user_id: int) -> bool:
        """Учитывает нарушение и сообщает, пора ли применять заглушку."""
        decision = await self._limiter.acquire(str(user_id), self._violation_rule)
        return not decision.allowed

    async def _apply_mute(self, user_id: int, rule: RateLimitRule) -> int:
        """Ставит заглушку с длительностью по номеру нарушения.

        Вместе с заглушкой сбрасываются оба ведра — основное и счётчик
        нарушений. Без этого наказание становилось бы бессрочным: окно
        лимита длиннее заглушки, поэтому первое же сообщение после её
        истечения снова превышало лимит и приводило к следующей, более
        длинной заглушке. Отбыв наказание, пользователь начинает с нуля.

        Номер нарушения при этом сохраняется отдельным счётчиком с большим
        сроком жизни, поэтому повторные срывы всё равно наказываются строже.
        """
        level = await self._guard.bump(f"mute-level:{user_id}", self._config.mute_level_ttl)
        index = min(level - 1, len(self._config.mute_durations) - 1)
        duration = self._config.mute_durations[index]

        await self._guard.acquire_once(f"mute:{user_id}", float(duration))
        await self._limiter.reset(str(user_id), rule)
        await self._limiter.reset(str(user_id), self._violation_rule)
        return duration

    async def _mute_remaining(self, user_id: int) -> int:
        """Сколько секунд осталось действовать заглушке."""
        remaining = await self._guard.ttl(f"mute:{user_id}")
        return math.ceil(remaining) if remaining > 0 else 0

    async def _should_warn(self, user_id: int) -> bool:
        """Разрешает предупреждение не чаще, чем раз в ``warn_cooldown``.

        Иначе бот сам начинает флудить в ответ на флуд.
        """
        token = await self._guard.acquire_once(f"warn:{user_id}", self._config.warn_cooldown)
        return token is not None
