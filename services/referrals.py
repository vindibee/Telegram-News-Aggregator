"""Реферальная программа: привязка приглашённого и начисление бонусов.

Бонус выдаётся обеим сторонам сразу при переходе по ссылке. Это делает
программу каналом привлечения, а не наградой за продажу, и одновременно
открывает очевидную дыру: пустые аккаунты можно заводить пачками. Полностью
закрыть её на этом уровне нельзя — сдерживают те же отпечатки, что защищают
пробный период, а окончательно вопрос решает политика «бонус после первой
оплаты», для которой в модели уже есть состояние ``qualified``.

Однократность начисления держится не на проверках в коде, а на уникальном
индексе по ``referrals.referred_id``. Двойной тап по ссылке, повторная
доставка апдейта и две реплики бота дают один и тот же результат: вторая
вставка не состоится, и до начисления дело не дойдёт.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from core.logger import get_logger
from db.enums import SubscriptionSource
from db.models import User
from db.uow import UnitOfWork

logger = get_logger(__name__)

#: Префикс полезной нагрузки диплинка: ``https://t.me/bot?start=ref_ABC123``.
REFERRAL_PAYLOAD_PREFIX: Final[str] = "ref_"

#: Максимальная длина кода в ссылке — защита от мусорной нагрузки.
_MAX_CODE_LENGTH: Final[int] = 16


class ReferralOutcome(StrEnum):
    """Чем закончился переход по реферальной ссылке."""

    #: Бонус начислен обеим сторонам.
    GRANTED = "granted"
    #: Кода с такой строкой не существует.
    UNKNOWN_CODE = "unknown_code"
    #: Человек перешёл по собственной ссылке.
    SELF_REFERRAL = "self_referral"
    #: У пользователя уже есть пригласивший.
    ALREADY_REFERRED = "already_referred"


@dataclass(frozen=True, slots=True)
class ReferralResult:
    """Итог обработки реферальной ссылки."""

    outcome: ReferralOutcome
    #: Сколько суток получила каждая сторона.
    days: int = 0
    #: Кто пригласил — нужен, чтобы уведомить его о новом реферале.
    referrer: User | None = None

    @property
    def granted(self) -> bool:
        """Был ли бонус выдан этим переходом."""
        return self.outcome is ReferralOutcome.GRANTED


def parse_referral_payload(payload: str | None) -> str | None:
    """Достаёт реферальный код из полезной нагрузки ``/start``.

    :param payload: Аргумент команды ``/start`` (может отсутствовать).
    :return: Нормализованный код либо ``None``, если нагрузка не реферальная.
    """
    if not payload:
        return None

    value = payload.strip()
    if not value.lower().startswith(REFERRAL_PAYLOAD_PREFIX):
        return None

    code = value[len(REFERRAL_PAYLOAD_PREFIX) :].strip().upper()
    if not code or len(code) > _MAX_CODE_LENGTH or not code.isalnum():
        # Нагрузку формирует кто угодно: ссылку можно составить руками.
        # Всё, что не похоже на наш код, до базы доходить не должно.
        logger.info("Реферальная ссылка с некорректным кодом: %r", payload)
        return None
    return code


class ReferralService:
    """Обрабатывает переходы по реферальным ссылкам."""

    def __init__(self, uow: UnitOfWork, *, bonus_days: int) -> None:
        """
        :param uow: Единица работы с открытой транзакцией.
        :param bonus_days: Сколько суток получает каждая сторона.
        :raises ValueError: Некорректный размер бонуса.
        """
        if bonus_days <= 0:
            raise ValueError(f"Бонус должен быть положительным, получено: {bonus_days}")
        self._uow = uow
        self._bonus_days = bonus_days

    @property
    def bonus_days(self) -> int:
        """Размер бонуса за приглашение."""
        return self._bonus_days

    async def apply_code(self, *, user: User, code: str, now: datetime) -> ReferralResult:
        """Привязывает приглашённого и начисляет бонус обеим сторонам.

        Все проверки, которые можно провалить дёшево, выполняются до
        записи: поход в базу за начислением делается только тогда, когда
        приглашение действительно новое.

        :param user: Приглашённый (уже зарегистрирован в базе).
        :param code: Реферальный код в каноническом виде.
        :param now: Момент операции (timezone-aware).
        :return: Результат обработки.
        """
        if user.referred_by_id is not None:
            logger.info(
                "Пользователь id=%s уже приглашён (реферер id=%s), код %s игнорируется",
                user.id, user.referred_by_id, code,
            )
            return ReferralResult(ReferralOutcome.ALREADY_REFERRED)

        referrer = await self._uow.users.get_by_referral_code(code)
        if referrer is None:
            logger.info("Реферальный код %s не найден", code)
            return ReferralResult(ReferralOutcome.UNKNOWN_CODE)

        if referrer.id == user.id:
            logger.info("Пользователь id=%s перешёл по собственной ссылке", user.id)
            return ReferralResult(ReferralOutcome.SELF_REFERRAL)

        # Обе записи — и связь в users, и строка в referrals — защищены
        # условиями на стороне БД. Первая из них, которая не сработает,
        # означает гонку: кто-то уже обработал этот переход.
        linked = await self._uow.users.set_referrer(user.id, referrer.id)
        if not linked:
            return ReferralResult(ReferralOutcome.ALREADY_REFERRED)

        referral = await self._uow.referrals.claim(
            referrer_id=referrer.id, referred_id=user.id, code=code
        )
        if referral is None:
            return ReferralResult(ReferralOutcome.ALREADY_REFERRED)

        # Начисление идёт после вставки строки referrals и в той же
        # транзакции: именно эта строка отвечает за однократность.
        payload = {"referral_id": referral.id, "code": code}
        await self._uow.subscriptions.grant_bonus_days(
            user.id,
            days=self._bonus_days,
            source=SubscriptionSource.REFERRAL,
            now=now,
            payload={**payload, "role": "referred"},
        )
        await self._uow.subscriptions.grant_bonus_days(
            referrer.id,
            days=self._bonus_days,
            source=SubscriptionSource.REFERRAL,
            now=now,
            payload={**payload, "role": "referrer"},
        )
        referral.reward(self._bonus_days, now)
        await self._uow.flush()

        logger.info(
            "Реферальный бонус %d сут. начислен паре id=%s -> id=%s",
            self._bonus_days, referrer.id, user.id,
        )
        return ReferralResult(
            ReferralOutcome.GRANTED, days=self._bonus_days, referrer=referrer
        )

    def build_link(self, bot_username: str, user: User) -> str:
        """Собирает персональную ссылку-приглашение.

        :param bot_username: Имя бота без ``@``.
        :param user: Владелец ссылки.
        :return: Готовый диплинк.
        """
        return f"https://t.me/{bot_username}?start={REFERRAL_PAYLOAD_PREFIX}{user.referral_code}"
