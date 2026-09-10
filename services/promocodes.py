"""Активация промокодов пользователем.

Разделение обязанностей здесь такое же, как в реферальной программе:
сервис принимает решение, а однократность обеспечивает база. Уникальный
индекс по паре «код + пользователь» физически не даёт применить код
дважды, поэтому проверка «а не применял ли он уже» отдельным запросом не
нужна — она всё равно оставляла бы окно между чтением и записью.

Причины отказа различаются намеренно: «код закончился» и «код просрочен» —
разные ситуации, и человеку, который набрал код с рекламы, важно понимать,
опоздал он или ошибся.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from core.logger import get_logger
from db.enums import PromocodeKind, SubscriptionSource
from db.models import Promocode, User
from db.uow import UnitOfWork

logger = get_logger(__name__)


class PromoOutcome(StrEnum):
    """Чем закончилась попытка активировать промокод."""

    #: Дни начислены.
    ACTIVATED = "activated"
    #: Ввод не похож на код вообще.
    INVALID = "invalid"
    #: Такого кода нет.
    UNKNOWN = "unknown"
    #: Код выключен вручную.
    DISABLED = "disabled"
    #: Срок действия ещё не начался или уже истёк.
    EXPIRED = "expired"
    #: Лимит активаций исчерпан.
    EXHAUSTED = "exhausted"
    #: Пользователь уже применял этот код.
    ALREADY_USED = "already_used"
    #: Код действует только при оплате (скидка или привязка к тарифу).
    CHECKOUT_ONLY = "checkout_only"


@dataclass(frozen=True, slots=True)
class PromoResult:
    """Итог активации промокода."""

    outcome: PromoOutcome
    #: Сколько суток начислено.
    days: int = 0
    #: Сам код — нужен для сообщения и журнала.
    promocode: Promocode | None = None

    @property
    def activated(self) -> bool:
        """Была ли активация успешной."""
        return self.outcome is PromoOutcome.ACTIVATED


class PromocodeService:
    """Применяет промокоды к подписке пользователя."""

    def __init__(self, uow: UnitOfWork) -> None:
        """
        :param uow: Единица работы с открытой транзакцией.
        """
        self._uow = uow

    async def activate(self, *, user: User, raw_code: str, now: datetime) -> PromoResult:
        """Применяет промокод и начисляет бонусные сутки.

        Порядок проверок выбран так, чтобы пользователь получал точную
        причину отказа, а не общее «код недействителен»: сначала
        разбираются конкретные случаи, и лишь затем — остаток срока
        действия.

        :param user: Кто активирует код.
        :param raw_code: Пользовательский ввод как есть.
        :param now: Момент операции (timezone-aware).
        :return: Результат активации.
        """
        try:
            code = Promocode.normalize_code(raw_code)
        except ValueError as exc:
            logger.info("Некорректный ввод промокода %r: %s", raw_code, exc)
            return PromoResult(PromoOutcome.INVALID)

        # Блокировка строки нужна из-за счётчика активаций: без неё два
        # одновременных применения последнего кода прочитали бы одно и то
        # же значение и оба сочли бы лимит незаполненным.
        promocode = await self._uow.promocodes.get_by_code_for_update(code)
        if promocode is None:
            logger.info("Промокод %s не найден", code)
            return PromoResult(PromoOutcome.UNKNOWN)

        if promocode.kind is not PromocodeKind.BONUS_DAYS or promocode.plan is not None:
            # Скидка выражается в процентах от оплаченного периода, а
            # привязанный к тарифу код — в днях этого тарифа. Ни то, ни
            # другое вне оплаты не определено, поэтому такие коды
            # применяются на кассе, а не отдельной командой.
            logger.info("Промокод %s применяется только при оплате", code)
            return PromoResult(PromoOutcome.CHECKOUT_ONLY, promocode=promocode)

        if not promocode.is_active:
            return PromoResult(PromoOutcome.DISABLED, promocode=promocode)

        if promocode.is_exhausted:
            # Исчерпанный код мог разобрать кто угодно — в том числе
            # сам обратившийся. «Вы уже применяли этот код» и «код
            # закончился» — разные новости, и путать их не стоит:
            # во втором случае человек пойдёт искать другой код.
            own = await self._uow.promocodes.get_redemption(
                promocode_id=promocode.id, user_id=user.id
            )
            if own is not None:
                return PromoResult(PromoOutcome.ALREADY_USED, promocode=promocode)

            logger.info(
                "Промокод %s исчерпан: %d из %s",
                code, promocode.activations, promocode.max_activations,
            )
            return PromoResult(PromoOutcome.EXHAUSTED, promocode=promocode)

        if not promocode.is_redeemable(now):
            logger.info("Промокод %s вне срока действия", code)
            return PromoResult(PromoOutcome.EXPIRED, promocode=promocode)

        days = promocode.value
        redemption = await self._uow.promocodes.redeem(
            promocode=promocode, user_id=user.id, days_granted=days
        )
        if redemption is None:
            return PromoResult(PromoOutcome.ALREADY_USED, promocode=promocode)

        # Начисление строго после вставки активации и в той же
        # транзакции: строка активации и есть гарантия однократности.
        await self._uow.subscriptions.grant_bonus_days(
            user.id,
            days=days,
            source=SubscriptionSource.PROMO,
            now=now,
            payload={"promocode_id": promocode.id, "code": code},
        )
        await self._uow.flush()

        logger.info("Промокод %s: пользователю id=%s начислено %d сут.", code, user.id, days)
        return PromoResult(PromoOutcome.ACTIVATED, days=days, promocode=promocode)
