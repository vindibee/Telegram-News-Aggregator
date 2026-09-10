"""Каталог тарифов.

Цены живут в коде, а не в базе: они меняются реже схемы, участвуют в
проверке суммы платежа и должны быть одинаковыми во всех репликах в момент
выкатки. Когда понадобятся A/B-тесты цен и история изменений, каталог
переезжает в таблицу ``plans``, а этот модуль становится её кэшем.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from db.enums import SubscriptionPlan

#: Валюта Telegram Stars. Суммы в XTR всегда целые — дробных звёзд не бывает.
STARS_CURRENCY: Final[str] = "XTR"

#: Ограничения Bot API на сумму счёта в звёздах.
MIN_STARS_AMOUNT: Final[int] = 1
MAX_STARS_AMOUNT: Final[int] = 10_000

#: Криптовалюта, в которой выставляются счета CryptoBot.
CRYPTO_ASSET: Final[str] = "USDT"

#: Нижняя граница суммы криптосчёта: комиссия сети съедает меньшие.
MIN_CRYPTO_AMOUNT: Final[Decimal] = Decimal("0.10")


@dataclass(frozen=True, slots=True)
class PlanOption:
    """Вариант оплаты: тариф на конкретный срок."""

    id: str
    plan: SubscriptionPlan
    title: str
    description: str
    stars: int
    #: Цена в USDT. Хранится строкой и превращается в Decimal: запись
    #: цены числом с плавающей точкой теряет копейки уже на литерале.
    usdt: str
    period_days: int
    features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not MIN_STARS_AMOUNT <= self.stars <= MAX_STARS_AMOUNT:
            raise ValueError(
                f"Цена тарифа {self.id} должна быть в диапазоне "
                f"{MIN_STARS_AMOUNT}..{MAX_STARS_AMOUNT} звёзд, получено: {self.stars}"
            )
        if self.period_days < 1:
            raise ValueError(f"Период тарифа {self.id} должен быть не меньше суток.")
        if self.crypto_amount < MIN_CRYPTO_AMOUNT:
            raise ValueError(
                f"Цена тарифа {self.id} в {CRYPTO_ASSET} должна быть не меньше "
                f"{MIN_CRYPTO_AMOUNT}, получено: {self.usdt}"
            )

    @property
    def crypto_amount(self) -> Decimal:
        """Цена в криптовалюте как точное десятичное число."""
        return Decimal(self.usdt)

    @property
    def stars_per_month(self) -> float:
        """Приведённая стоимость месяца — для показа выгоды длинных тарифов."""
        return self.stars / (self.period_days / 30)


#: Доступные варианты оплаты. Идентификатор попадает в callback_data,
#: поэтому он короткий и не меняется после публикации.
PLAN_OPTIONS: Final[tuple[PlanOption, ...]] = (
    PlanOption(
        id="pro_1m",
        plan=SubscriptionPlan.PRO,
        title="Pro на месяц",
        description="Полный доступ к агрегатору новостей на 30 дней.",
        stars=150,
        usdt="2.50",
        period_days=30,
        features=("Все каналы", "Поиск по архиву", "Уведомления о свежих записях"),
    ),
    PlanOption(
        id="pro_12m",
        plan=SubscriptionPlan.PRO,
        title="Pro на год",
        description="Полный доступ к агрегатору новостей на 365 дней.",
        stars=1500,
        usdt="25.00",
        period_days=365,
        features=("Всё из месячного тарифа", "Два месяца в подарок"),
    ),
    PlanOption(
        id="biz_1m",
        plan=SubscriptionPlan.BUSINESS,
        title="Business на месяц",
        description="Командный доступ и выгрузка данных на 30 дней.",
        stars=500,
        usdt="8.00",
        period_days=30,
        features=("Всё из Pro", "Выгрузка в CSV", "Приоритетная поддержка"),
    ),
)

_BY_ID: Final[dict[str, PlanOption]] = {option.id: option for option in PLAN_OPTIONS}


def get_plan_option(option_id: str) -> PlanOption | None:
    """Возвращает вариант оплаты по идентификатору.

    Идентификатор приходит из callback_data, то есть от клиента, и не может
    считаться доверенным: неизвестное значение должно приводить к отказу,
    а не к обращению к произвольному тарифу.

    :param option_id: Идентификатор варианта.
    :return: Вариант оплаты либо ``None``.
    """
    return _BY_ID.get(option_id.strip())
