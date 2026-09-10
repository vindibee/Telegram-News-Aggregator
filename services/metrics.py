"""Бизнес-метрики для панели администратора.

Сбор вынесен в сервис, а не размазан по хендлеру: те же числа нужны и
боту, и будущему отчёту в канал, и внешнему мониторингу, а повторять
десяток запросов в каждом месте — верный способ получить три разные
версии одной метрики.

Про «доход за месяц». В классическом смысле MRR — это сумма регулярных
подписных платежей, нормированная на месяц. Здесь подписка продаётся
разовыми периодами через звёзды и криптовалюту, автопродления нет, и
честно посчитать регулярную выручку не из чего. Поэтому показывается ровно
то, что есть: сумма успешных платежей за последние тридцать суток. Число
полезное, но при заметной доле годовых оплат оно скачет, и принимать его
за MRR нельзя.

Валюты не складываются. Звёзды и USDT — разные единицы, курс между ними в
базе не хранится, и «общая сумма» из них была бы просто неверной.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from core.logger import get_logger
from db.enums import SubscriptionStatus
from db.repositories.promo import PromocodeTotals
from db.repositories.referral import ReferralTotals
from db.repositories.user import UserCounters
from db.uow import UnitOfWork

logger = get_logger(__name__)

#: Окно расчёта «дохода за месяц».
REVENUE_WINDOW_DAYS: Final[int] = 30


@dataclass(frozen=True, slots=True)
class DashboardMetrics:
    """Снимок ключевых показателей на момент запроса."""

    #: Момент расчёта.
    taken_at: datetime
    users: UserCounters
    #: Распределение подписок по статусам.
    subscriptions: dict[SubscriptionStatus, int]
    #: Выручка за окно ``REVENUE_WINDOW_DAYS`` в разрезе валют.
    revenue_month: dict[str, Decimal]
    #: Выручка за всё время в разрезе валют.
    revenue_total: dict[str, Decimal]
    #: Сколько разных пользователей хоть раз заплатили.
    paying_users: int
    referrals: ReferralTotals
    promocodes: PromocodeTotals

    @property
    def active_subscriptions(self) -> int:
        """Оплаченные действующие подписки."""
        return self.subscriptions.get(SubscriptionStatus.ACTIVE, 0)

    @property
    def trial_users(self) -> int:
        """Пользователи на пробном периоде прямо сейчас."""
        return self.subscriptions.get(SubscriptionStatus.TRIALING, 0)

    @property
    def conversion(self) -> float:
        """Доля заплативших среди всех зарегистрированных, в процентах.

        Знаменатель — все пользователи, включая тех, кто пришёл минуту
        назад и ещё не успел ничего решить. Метрика поэтому занижена и
        годится для наблюдения за динамикой, а не как абсолютная оценка.
        """
        if self.users.total <= 0:
            return 0.0
        return self.paying_users / self.users.total * 100

    @property
    def trial_conversion(self) -> float:
        """Доля заплативших среди попробовавших бота, в процентах.

        Более честная воронка, чем :attr:`conversion`: знаменатель —
        только те, кто дошёл до пробного периода, то есть действительно
        познакомился с продуктом.
        """
        if self.users.trial_used <= 0:
            return 0.0
        return self.paying_users / self.users.trial_used * 100

    @property
    def blocked_share(self) -> float:
        """Доля заблокировавших бота, в процентах."""
        if self.users.total <= 0:
            return 0.0
        return self.users.blocked / self.users.total * 100


class MetricsService:
    """Собирает показатели панели администратора."""

    def __init__(self, uow: UnitOfWork) -> None:
        """
        :param uow: Единица работы с открытой транзакцией.
        """
        self._uow = uow

    async def collect(self, now: datetime) -> DashboardMetrics:
        """Считает все показатели панели.

        Запросы идут последовательно, а не параллельно: они делят одну
        сессию SQLAlchemy, а она не рассчитана на одновременное
        использование из нескольких задач. Панель открывают редко, и
        несколько агрегатов по индексам её не нагрузят.

        :param now: Момент расчёта (timezone-aware).
        :return: Снимок показателей.
        """
        since = now - timedelta(days=REVENUE_WINDOW_DAYS)

        users = await self._uow.users.counters(now)
        subscriptions = await self._uow.subscriptions.count_by_status()
        revenue_month = await self._uow.payments.revenue_by_currency(since=since)
        revenue_total = await self._uow.payments.revenue_by_currency()
        paying_users = await self._uow.payments.count_paying_users()
        referrals = await self._uow.referrals.totals()
        promocodes = await self._uow.promocodes.totals()

        metrics = DashboardMetrics(
            taken_at=now,
            users=users,
            subscriptions=subscriptions,
            revenue_month=revenue_month,
            revenue_total=revenue_total,
            paying_users=paying_users,
            referrals=referrals,
            promocodes=promocodes,
        )

        logger.info(
            "Метрики: пользователей %d, активных подписок %d, на триале %d, конверсия %.1f%%",
            metrics.users.total,
            metrics.active_subscriptions,
            metrics.trial_users,
            metrics.conversion,
        )
        return metrics


def format_money(amount: Decimal, currency: str) -> str:
    """Форматирует сумму для показа администратору.

    Звёзды Telegram целочисленны, у криптовалют дробная часть значима.
    Общего формата у них нет, поэтому разделение явное.

    :param amount: Сумма.
    :param currency: Код валюты.
    :return: Строка вида ``1 234 XTR`` или ``12.50 USDT``.
    """
    code = currency.upper()
    if code == "XTR":
        return f"{int(amount):,}".replace(",", " ") + " ⭐"
    return f"{amount:.2f} {code}"


def format_revenue(revenue: dict[str, Decimal]) -> str:
    """Собирает выручку по валютам в одну строку.

    :param revenue: Отображение «валюта → сумма».
    :return: Перечисление через запятую либо прочерк, если платежей нет.
    """
    parts = [
        format_money(amount, currency)
        for currency, amount in sorted(revenue.items())
        if amount > 0
    ]
    return ", ".join(parts) if parts else "—"


__all__ = [
    "REVENUE_WINDOW_DAYS",
    "DashboardMetrics",
    "MetricsService",
    "format_money",
    "format_revenue",
]
