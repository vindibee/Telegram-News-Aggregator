"""Панель администратора: права доступа и бизнес-метрики.

Проверка прав важнее остального: ошибка здесь означает, что рассылку и
промокоды сможет запустить посторонний. Поэтому фильтр проверяется со всех
сторон — флаг в базе, список из окружения, отсутствие пользователя вовсе.

Метрики проверяются на арифметику воронки: конверсия считается по разным
знаменателям, и перепутать их — значит месяцами смотреть не на то число.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from core.config import AdminConfig, Settings
from db.enums import PaymentStatus, SubscriptionStatus
from db.models import User
from db.uow import UnitOfWork
from services.metrics import MetricsService, format_money, format_revenue
from tg_bot.filters import IsAdmin, IsNotAdmin
from tests.conftest import FROZEN_NOW

pytestmark = pytest.mark.db


def _settings_with_admins(settings: Settings, *ids: int) -> Settings:
    """Копия настроек с подменённым списком администраторов."""
    admin = AdminConfig(
        ids=ids,
        referral_bonus_days=settings.admin.referral_bonus_days,
        broadcast_rate=settings.admin.broadcast_rate,
        broadcast_workers=settings.admin.broadcast_workers,
        broadcast_page_size=settings.admin.broadcast_page_size,
    )
    return replace_admin(settings, admin)


def replace_admin(settings: Settings, admin: AdminConfig) -> Settings:
    """Возвращает настройки с другой админской секцией.

    ``Settings`` — frozen-датакласс со ``slots``, поэтому поле подменяется
    пересборкой, а не присваиванием.
    """
    import dataclasses

    return dataclasses.replace(settings, admin=admin)


@pytest.fixture
def make_paid(make_payment):
    """Создаёт успешный платёж.

    ``external_id`` обязателен: ограничение
    ``ck_payments_succeeded_has_external_id`` не даёт пометить платёж
    оплаченным без идентификатора у провайдера — иначе сверка с его
    выпиской была бы невозможна.
    """
    counter = iter(range(1, 10_000))

    async def _make(user, **overrides):
        overrides.setdefault("status", PaymentStatus.SUCCEEDED)
        overrides.setdefault("paid_at", FROZEN_NOW)
        overrides.setdefault("external_id", f"ext_{next(counter)}")
        return await make_payment(user, **overrides)

    return _make


# ------------------------------------------------------------------ права
async def test_is_admin_accepts_user_with_database_flag(
    settings: Settings,
    make_user,
) -> None:
    admin = await make_user(is_admin=True)

    assert await IsAdmin()(None, user=admin, settings=settings) is True, (
        "Флаг в базе — основной источник прав"
    )


async def test_is_admin_accepts_user_listed_in_environment(
    settings: Settings,
    make_user,
) -> None:
    # Стартовый список: первый флаг в базе кто-то должен выставить, а
    # сделать это через бота может только уже существующий администратор.
    candidate = await make_user(is_admin=False)
    configured = _settings_with_admins(settings, candidate.telegram_id)

    assert await IsAdmin()(None, user=candidate, settings=configured) is True


async def test_is_admin_rejects_ordinary_user(
    settings: Settings,
    make_user,
) -> None:
    ordinary = await make_user(is_admin=False)
    configured = _settings_with_admins(settings, ordinary.telegram_id + 1)

    assert await IsAdmin()(None, user=ordinary, settings=configured) is False, (
        "Посторонний не должен попадать в панель"
    )


async def test_is_admin_rejects_update_without_user(settings: Settings) -> None:
    # Служебные апдейты без автора: администратора в них нет.
    assert await IsAdmin()(None, user=None, settings=settings) is False


async def test_is_admin_rejects_when_settings_missing(make_user) -> None:
    ordinary = await make_user(is_admin=False)

    assert await IsAdmin()(None, user=ordinary, settings=None) is False, (
        "Без настроек остаётся только флаг в базе, и он не выставлен"
    )


async def test_is_not_admin_inverts_the_check(
    settings: Settings,
    make_user,
) -> None:
    admin = await make_user(is_admin=True)
    ordinary = await make_user(is_admin=False)

    assert await IsNotAdmin()(None, user=admin, settings=settings) is False
    assert await IsNotAdmin()(None, user=ordinary, settings=settings) is True


async def test_set_admin_grants_and_revokes_rights(
    uow: UnitOfWork,
    make_user,
) -> None:
    candidate = await make_user(is_admin=False)

    assert await uow.users.set_admin(candidate.id, is_admin=True) is True
    granted = await uow.users.get_by_id(candidate.id)
    assert granted is not None and granted.is_admin

    assert await uow.users.set_admin(candidate.id, is_admin=False) is True
    revoked = await uow.users.get_by_id(candidate.id)
    assert revoked is not None and not revoked.is_admin


async def test_set_admin_reports_missing_user(uow: UnitOfWork) -> None:
    assert await uow.users.set_admin(10**9, is_admin=True) is False, (
        "Несуществующему пользователю права выдать нельзя"
    )


async def test_list_admins_returns_only_privileged(
    uow: UnitOfWork,
    make_user,
) -> None:
    admin = await make_user(is_admin=True)
    await make_user(is_admin=False)

    admins = await uow.users.list_admins()

    assert [item.id for item in admins] == [admin.id]


# --------------------------------------------------------------- метрики
async def test_metrics_count_users_subscriptions_and_trials(
    uow: UnitOfWork,
    make_user,
    make_subscription,
) -> None:
    paid = await make_user(trial_activated_at=FROZEN_NOW - timedelta(days=10))
    await make_subscription(paid, started_at=FROZEN_NOW, status=SubscriptionStatus.ACTIVE)

    trialing = await make_user(trial_activated_at=FROZEN_NOW - timedelta(days=1))
    await make_subscription(trialing, started_at=FROZEN_NOW, status=SubscriptionStatus.TRIALING)

    await make_user()  # заглянул и ушёл

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.users.total == 3
    assert metrics.active_subscriptions == 1
    assert metrics.trial_users == 1
    assert metrics.users.trial_used == 2, "Считаются все, кто когда-либо запускал триал"


async def test_metrics_count_new_users_within_windows(
    uow: UnitOfWork,
    make_user,
    db_session,
) -> None:
    from sqlalchemy import update

    from db.models import User as UserModel

    fresh = await make_user()
    old = await make_user()
    # created_at проставляется базой, поэтому «старого» пользователя
    # приходится состарить явно.
    await db_session.execute(
        update(UserModel)
        .where(UserModel.id == old.id)
        .values(created_at=FROZEN_NOW - timedelta(days=30))
    )
    await db_session.commit()

    metrics = await MetricsService(uow).collect(FROZEN_NOW + timedelta(hours=1))

    assert metrics.users.new_today == 1, f"За сутки должен считаться только {fresh.id}"
    assert metrics.users.new_week == 1
    assert metrics.users.total == 2


async def test_metrics_revenue_is_split_by_currency(
    uow: UnitOfWork,
    make_user,
    make_paid,
) -> None:
    # Звёзды и USDT — разные единицы, курса между ними в базе нет.
    payer = await make_user()
    await make_paid(payer, amount=Decimal(150), currency="XTR")
    await make_paid(payer, amount=Decimal("12.50"), currency="USDT")

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.revenue_total == {"XTR": Decimal(150), "USDT": Decimal("12.50")}, (
        "Выручка должна оставаться разделённой по валютам"
    )


async def test_metrics_revenue_month_ignores_older_payments(
    uow: UnitOfWork,
    make_user,
    make_paid,
) -> None:
    payer = await make_user()
    await make_paid(payer, amount=Decimal(100))
    await make_paid(payer, paid_at=FROZEN_NOW - timedelta(days=45), amount=Decimal(900))

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.revenue_month == {"XTR": Decimal(100)}, "Окно дохода — тридцать суток"
    assert metrics.revenue_total == {"XTR": Decimal(1000)}


async def test_metrics_revenue_ignores_unpaid_invoices(
    uow: UnitOfWork,
    make_user,
    make_payment,
) -> None:
    payer = await make_user()
    await make_payment(payer, status=PaymentStatus.PENDING, amount=Decimal(500))

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.revenue_total == {}, "Выставленный, но не оплаченный счёт — не выручка"
    assert metrics.paying_users == 0


async def test_metrics_counts_paying_users_not_payments(
    uow: UnitOfWork,
    make_user,
    make_paid,
) -> None:
    # Один человек с тремя продлениями — по-прежнему один оплативший.
    payer = await make_user()
    for _ in range(3):
        await make_paid(payer)
    await make_user()

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.paying_users == 1
    assert metrics.conversion == pytest.approx(50.0), (
        "Один плательщик из двух пользователей — половина"
    )


async def test_metrics_trial_conversion_uses_triers_as_denominator(
    uow: UnitOfWork,
    make_user,
    make_paid,
) -> None:
    payer = await make_user(trial_activated_at=FROZEN_NOW - timedelta(days=5))
    await make_paid(payer)
    await make_user(trial_activated_at=FROZEN_NOW - timedelta(days=3))
    await make_user()  # до триала не дошёл

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.conversion == pytest.approx(100 / 3), "Знаменатель — все пользователи"
    assert metrics.trial_conversion == pytest.approx(50.0), (
        "Знаменатель воронки — только попробовавшие продукт"
    )


async def test_metrics_on_empty_database_do_not_divide_by_zero(uow: UnitOfWork) -> None:
    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.users.total == 0
    assert metrics.conversion == 0.0
    assert metrics.trial_conversion == 0.0
    assert metrics.blocked_share == 0.0


async def test_metrics_include_referral_and_promocode_totals(
    uow: UnitOfWork,
    user: User,
    make_user,
) -> None:
    from services.referrals import ReferralService

    invited = await make_user()
    await ReferralService(uow, bonus_days=3).apply_code(
        user=invited, code=user.referral_code, now=FROZEN_NOW
    )

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.referrals.total == 1
    assert metrics.referrals.rewarded == 1
    assert metrics.referrals.bonus_days == 3
    assert metrics.promocodes.codes == 0


async def test_metrics_blocked_share_reflects_blocked_users(
    uow: UnitOfWork,
    make_user,
) -> None:
    await make_user(is_bot_blocked=True)
    await make_user()
    await make_user()
    await make_user()

    metrics = await MetricsService(uow).collect(FROZEN_NOW)

    assert metrics.users.blocked == 1
    assert metrics.blocked_share == pytest.approx(25.0)


# ------------------------------------------------------------ оформление
def test_format_money_renders_stars_without_fraction() -> None:
    # Звёзды целочисленны, дробная часть у них смысла не имеет.
    assert format_money(Decimal(1234), "XTR") == "1 234 ⭐"


def test_format_money_keeps_two_decimals_for_crypto() -> None:
    assert format_money(Decimal("12.5"), "usdt") == "12.50 USDT"


def test_format_revenue_joins_currencies() -> None:
    rendered = format_revenue({"XTR": Decimal(100), "USDT": Decimal("5.00")})

    assert "100 ⭐" in rendered and "5.00 USDT" in rendered


def test_format_revenue_skips_zero_amounts() -> None:
    assert format_revenue({"XTR": Decimal(0)}) == "—", (
        "Валюта без поступлений не должна засорять панель"
    )


def test_format_revenue_without_payments_shows_dash() -> None:
    assert format_revenue({}) == "—"
