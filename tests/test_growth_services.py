"""Реферальная программа и промокоды: начисление и защита от повторов.

Главное, что здесь проверяется, — однократность. Оба сценария опираются
не на проверки в коде, а на уникальные индексы, поэтому тесты бьют именно
в повторный вызов: двойной тап по ссылке, повторный ввод того же кода,
гонка двух одновременных активаций последнего оставшегося кода.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db.enums import (
    PromocodeKind,
    ReferralStatus,
    SubscriptionPlan,
    SubscriptionSource,
    SubscriptionStatus,
)
from db.models import Promocode, User
from db.uow import UnitOfWork
from services.promocodes import PromocodeService, PromoOutcome
from services.referrals import (
    ReferralOutcome,
    ReferralService,
    parse_referral_payload,
)
from tests.conftest import FROZEN_NOW

pytestmark = pytest.mark.db

BONUS_DAYS = 3


# --------------------------------------------------------------- диплинк
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("ref_ABC123", "ABC123"),
        ("REF_abc123", "ABC123"),
        ("  ref_ABC123  ", "ABC123"),
    ],
)
def test_parse_referral_payload_extracts_normalized_code(payload: str, expected: str) -> None:
    assert parse_referral_payload(payload) == expected, (
        f"Код из нагрузки {payload!r} должен нормализоваться в {expected!r}"
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "promo_ABC123",
        "ref_",
        "ref_" + "A" * 40,
        "ref_ABC-123",
        "ref_<script>",
    ],
)
def test_parse_referral_payload_rejects_foreign_and_malformed(payload: str | None) -> None:
    # Нагрузку формирует кто угодно: ссылку можно составить руками, и всё,
    # что не похоже на наш код, до базы доходить не должно.
    assert parse_referral_payload(payload) is None, (
        f"Нагрузка {payload!r} не должна распознаваться как реферальный код"
    )


# -------------------------------------------------------------- рефералы
async def test_referral_apply_code_grants_bonus_to_both_sides(
    uow: UnitOfWork,
    make_user,
) -> None:
    referrer = await make_user()
    invited = await make_user()

    service = ReferralService(uow, bonus_days=BONUS_DAYS)
    result = await service.apply_code(
        user=invited, code=referrer.referral_code, now=FROZEN_NOW
    )

    assert result.outcome is ReferralOutcome.GRANTED, "Новое приглашение должно засчитаться"
    assert result.days == BONUS_DAYS
    assert result.referrer is not None and result.referrer.id == referrer.id

    for side, label in ((invited, "приглашённого"), (referrer, "пригласившего")):
        subscription = await uow.subscriptions.get_live(side.id)
        assert subscription is not None, f"У {label} должна появиться подписка"
        assert subscription.source is SubscriptionSource.REFERRAL
        assert subscription.status is SubscriptionStatus.TRIALING, (
            "Бонусный доступ не оплачен и не должен считаться активной подпиской"
        )


async def test_referral_apply_code_links_referrer_and_marks_rewarded(
    uow: UnitOfWork,
    make_user,
) -> None:
    referrer = await make_user()
    invited = await make_user()

    await ReferralService(uow, bonus_days=BONUS_DAYS).apply_code(
        user=invited, code=referrer.referral_code, now=FROZEN_NOW
    )

    # Перечитываем через uow: объект invited принадлежит другой сессии
    # (его создала фабрика conftest) и изменений сервиса не видит.
    linked = await uow.users.get_by_id(invited.id)
    assert linked is not None
    assert linked.referred_by_id == referrer.id, "Связь «кто кого привёл» должна проставиться"

    referral = await uow.referrals.get_by_referred(invited.id)
    assert referral is not None, "Приглашение должно быть зафиксировано в журнале"
    assert referral.status is ReferralStatus.REWARDED
    assert referral.bonus_days == BONUS_DAYS
    assert referral.payment_id is None, "Мгновенный бонус не привязан к платежу"


async def test_referral_apply_code_twice_does_not_grant_bonus_again(
    uow: UnitOfWork,
    make_user,
) -> None:
    # Двойной тап по ссылке — самый частый способ получить бонус дважды.
    referrer = await make_user()
    invited = await make_user()
    service = ReferralService(uow, bonus_days=BONUS_DAYS)

    first = await service.apply_code(user=invited, code=referrer.referral_code, now=FROZEN_NOW)
    expires_after_first = (await uow.subscriptions.get_live(referrer.id)).expires_at

    second = await service.apply_code(user=invited, code=referrer.referral_code, now=FROZEN_NOW)

    assert first.granted, "Первый переход должен начислить бонус"
    assert second.outcome is ReferralOutcome.ALREADY_REFERRED, (
        "Повторный переход по той же ссылке не должен начислять повторно"
    )
    assert (await uow.subscriptions.get_live(referrer.id)).expires_at == expires_after_first, (
        "Срок подписки пригласившего не должен сдвигаться повторным переходом"
    )


async def test_referral_apply_code_rejects_self_invitation(
    uow: UnitOfWork,
    user: User,
) -> None:
    result = await ReferralService(uow, bonus_days=BONUS_DAYS).apply_code(
        user=user, code=user.referral_code, now=FROZEN_NOW
    )

    assert result.outcome is ReferralOutcome.SELF_REFERRAL
    assert await uow.subscriptions.get_live(user.id) is None, (
        "Переход по собственной ссылке не должен давать подписку"
    )


async def test_referral_apply_code_with_unknown_code_grants_nothing(
    uow: UnitOfWork,
    user: User,
) -> None:
    result = await ReferralService(uow, bonus_days=BONUS_DAYS).apply_code(
        user=user, code="NOSUCHCODE", now=FROZEN_NOW
    )

    assert result.outcome is ReferralOutcome.UNKNOWN_CODE
    untouched = await uow.users.get_by_id(user.id)
    assert untouched is not None
    assert untouched.referred_by_id is None, (
        "Несуществующий код не должен привязывать реферера"
    )


async def test_referral_second_code_cannot_replace_existing_referrer(
    uow: UnitOfWork,
    make_user,
) -> None:
    # Смена пригласившего задним числом ломает честность программы.
    first_referrer = await make_user()
    second_referrer = await make_user()
    invited = await make_user()
    service = ReferralService(uow, bonus_days=BONUS_DAYS)

    await service.apply_code(user=invited, code=first_referrer.referral_code, now=FROZEN_NOW)
    result = await service.apply_code(
        user=invited, code=second_referrer.referral_code, now=FROZEN_NOW
    )

    assert result.outcome is ReferralOutcome.ALREADY_REFERRED
    linked = await uow.users.get_by_id(invited.id)
    assert linked is not None
    assert linked.referred_by_id == first_referrer.id, "Реферер не должен подменяться"
    assert await uow.subscriptions.get_live(second_referrer.id) is None, (
        "Второй пригласивший не должен получить бонус"
    )


async def test_referral_bonus_extends_existing_subscription(
    uow: UnitOfWork,
    make_user,
    make_subscription,
) -> None:
    referrer = await make_user()
    invited = await make_user()
    existing = await make_subscription(
        referrer, started_at=FROZEN_NOW, status=SubscriptionStatus.ACTIVE
    )
    original_expiry = existing.expires_at

    await ReferralService(uow, bonus_days=BONUS_DAYS).apply_code(
        user=invited, code=referrer.referral_code, now=FROZEN_NOW
    )

    subscription = await uow.subscriptions.get_live(referrer.id)
    assert subscription is not None
    assert subscription.id == existing.id, "Вторая подписка создаваться не должна"
    assert subscription.expires_at > original_expiry, "Срок должен продлиться на бонус"
    assert subscription.status is SubscriptionStatus.ACTIVE, (
        "Бонус не должен понижать оплаченную подписку до пробной"
    )


def test_referral_service_rejects_non_positive_bonus(uow: UnitOfWork) -> None:
    with pytest.raises(ValueError):
        ReferralService(uow, bonus_days=0)


async def test_referral_build_link_contains_personal_code(
    uow: UnitOfWork,
    user: User,
) -> None:
    link = ReferralService(uow, bonus_days=BONUS_DAYS).build_link("MyBot", user)

    assert link == f"https://t.me/MyBot?start=ref_{user.referral_code}", (
        "Ссылка должна вести на бота с реферальной нагрузкой"
    )


# ------------------------------------------------------------- промокоды
async def _make_promo(uow: UnitOfWork, **overrides) -> Promocode:
    """Создаёт промокод на бонусные дни."""
    params = {
        "code": Promocode.generate_code(),
        "kind": PromocodeKind.BONUS_DAYS,
        "value": 30,
    }
    params.update(overrides)
    promocode = await uow.promocodes.create(**params)
    await uow.commit()
    return promocode


async def test_promocode_activate_grants_days_and_counts_activation(
    uow: UnitOfWork,
    user: User,
) -> None:
    promocode = await _make_promo(uow, value=30, max_activations=5)

    result = await PromocodeService(uow).activate(
        user=user, raw_code=promocode.code, now=FROZEN_NOW
    )

    assert result.outcome is PromoOutcome.ACTIVATED
    assert result.days == 30
    assert promocode.activations == 1, "Счётчик активаций должен вырасти ровно на одну"

    subscription = await uow.subscriptions.get_live(user.id)
    assert subscription is not None, "Промокод должен дать подписку"
    assert subscription.source is SubscriptionSource.PROMO


async def test_promocode_activate_accepts_messy_user_input(
    uow: UnitOfWork,
    user: User,
) -> None:
    # Код диктуют голосом и переписывают с картинки: регистр и дефисы
    # не должны мешать.
    promocode = await _make_promo(uow, code="SUMMER24")

    result = await PromocodeService(uow).activate(
        user=user, raw_code="  sum-mer-24 ", now=FROZEN_NOW
    )

    assert result.outcome is PromoOutcome.ACTIVATED, (
        "Ввод с пробелами, дефисами и в нижнем регистре должен приниматься"
    )
    assert promocode.activations == 1


async def test_promocode_second_activation_by_same_user_is_refused(
    uow: UnitOfWork,
    user: User,
) -> None:
    promocode = await _make_promo(uow, value=30, max_activations=5)
    service = PromocodeService(uow)

    await service.activate(user=user, raw_code=promocode.code, now=FROZEN_NOW)
    expires_after_first = (await uow.subscriptions.get_live(user.id)).expires_at

    second = await service.activate(user=user, raw_code=promocode.code, now=FROZEN_NOW)

    assert second.outcome is PromoOutcome.ALREADY_USED
    assert promocode.activations == 1, "Повтор не должен расходовать лимит активаций"
    assert (await uow.subscriptions.get_live(user.id)).expires_at == expires_after_first, (
        "Повторная активация не должна продлевать подписку"
    )


async def test_promocode_exhausted_by_self_reports_already_used(
    uow: UnitOfWork,
    user: User,
) -> None:
    # Код на одну активацию, израсходованный самим обратившимся. Сказать
    # «код закончился» здесь неверно: человек пойдёт искать другой код,
    # хотя этот он уже применил и дни получил.
    promocode = await _make_promo(uow, max_activations=1)
    service = PromocodeService(uow)

    await service.activate(user=user, raw_code=promocode.code, now=FROZEN_NOW)
    second = await service.activate(user=user, raw_code=promocode.code, now=FROZEN_NOW)

    assert second.outcome is PromoOutcome.ALREADY_USED, (
        "Свою же активацию нельзя выдавать за исчерпанный лимит"
    )


async def test_promocode_activate_refuses_when_limit_is_exhausted(
    uow: UnitOfWork,
    user: User,
    make_user,
) -> None:
    promocode = await _make_promo(uow, max_activations=1)
    other = await make_user()
    service = PromocodeService(uow)

    await service.activate(user=other, raw_code=promocode.code, now=FROZEN_NOW)
    result = await service.activate(user=user, raw_code=promocode.code, now=FROZEN_NOW)

    assert result.outcome is PromoOutcome.EXHAUSTED
    assert await uow.subscriptions.get_live(user.id) is None, (
        "Исчерпанный код не должен давать подписку"
    )


@pytest.mark.parametrize(
    ("valid_from", "valid_until"),
    [
        (FROZEN_NOW + timedelta(days=1), None),
        (None, FROZEN_NOW - timedelta(seconds=1)),
    ],
    ids=["ещё_не_начался", "уже_закончился"],
)
async def test_promocode_activate_refuses_outside_validity_window(
    uow: UnitOfWork,
    user: User,
    valid_from: datetime | None,
    valid_until: datetime | None,
) -> None:
    promocode = await _make_promo(uow, valid_from=valid_from, valid_until=valid_until)

    result = await PromocodeService(uow).activate(
        user=user, raw_code=promocode.code, now=FROZEN_NOW
    )

    assert result.outcome is PromoOutcome.EXPIRED
    assert promocode.activations == 0


async def test_promocode_activate_refuses_disabled_code(
    uow: UnitOfWork,
    user: User,
) -> None:
    promocode = await _make_promo(uow)
    promocode.is_active = False
    await uow.commit()

    result = await PromocodeService(uow).activate(
        user=user, raw_code=promocode.code, now=FROZEN_NOW
    )

    assert result.outcome is PromoOutcome.DISABLED


async def test_promocode_activate_refuses_unknown_code(uow: UnitOfWork, user: User) -> None:
    result = await PromocodeService(uow).activate(
        user=user, raw_code="NOSUCH99", now=FROZEN_NOW
    )

    assert result.outcome is PromoOutcome.UNKNOWN


async def test_promocode_activate_refuses_empty_input(uow: UnitOfWork, user: User) -> None:
    result = await PromocodeService(uow).activate(user=user, raw_code="   ", now=FROZEN_NOW)

    assert result.outcome is PromoOutcome.INVALID


async def test_promocode_discount_code_is_not_activated_standalone(
    uow: UnitOfWork,
    user: User,
) -> None:
    # Скидка выражается в процентах от оплаченного периода: вне оплаты
    # она не определена, поэтому такой код применяется только на кассе.
    promocode = await _make_promo(uow, kind=PromocodeKind.DISCOUNT_PERCENT, value=20)

    result = await PromocodeService(uow).activate(
        user=user, raw_code=promocode.code, now=FROZEN_NOW
    )

    assert result.outcome is PromoOutcome.CHECKOUT_ONLY
    assert promocode.activations == 0, "Отказ не должен расходовать лимит"
    assert await uow.subscriptions.get_live(user.id) is None


async def test_promocode_plan_restricted_code_is_not_activated_standalone(
    uow: UnitOfWork,
    user: User,
) -> None:
    promocode = await _make_promo(uow, plan=SubscriptionPlan.BUSINESS)

    result = await PromocodeService(uow).activate(
        user=user, raw_code=promocode.code, now=FROZEN_NOW
    )

    assert result.outcome is PromoOutcome.CHECKOUT_ONLY, (
        "Код, привязанный к тарифу, вне оплаты применять не к чему"
    )


async def test_promocode_activation_is_recorded_in_redemption_journal(
    uow: UnitOfWork,
    user: User,
) -> None:
    promocode = await _make_promo(uow, value=14)

    await PromocodeService(uow).activate(user=user, raw_code=promocode.code, now=FROZEN_NOW)

    assert await uow.promocodes.count_redemptions(promocode.id) == 1, (
        "Журнал активаций должен сходиться со счётчиком"
    )


async def test_promocode_bonus_extends_existing_subscription(
    uow: UnitOfWork,
    user: User,
    make_subscription,
) -> None:
    existing = await make_subscription(user, started_at=FROZEN_NOW)
    original_expiry = existing.expires_at
    promocode = await _make_promo(uow, value=10)

    await PromocodeService(uow).activate(user=user, raw_code=promocode.code, now=FROZEN_NOW)

    subscription = await uow.subscriptions.get_live(user.id)
    assert subscription is not None
    assert subscription.id == existing.id
    assert subscription.expires_at >= original_expiry + timedelta(days=9), (
        "Промокод на 10 суток должен продлить подписку примерно на столько же"
    )


# ----------------------------------------------------- бонус вне платежа
async def test_grant_bonus_days_rejects_non_positive_period(
    uow: UnitOfWork,
    user: User,
) -> None:
    with pytest.raises(ValueError):
        await uow.subscriptions.grant_bonus_days(
            user.id, days=0, source=SubscriptionSource.MANUAL, now=FROZEN_NOW
        )


async def test_grant_bonus_days_does_not_revive_expired_subscription_backwards(
    uow: UnitOfWork,
    user: User,
    make_subscription,
) -> None:
    # Давно истёкшая подписка не должна продлеваться от старой даты:
    # иначе бонус в три дня не дал бы ни часа доступа.
    long_ago = datetime.now(tz=timezone.utc) - timedelta(days=400)
    await make_subscription(
        user,
        started_at=long_ago,
        expires_at=long_ago + timedelta(days=30),
        status=SubscriptionStatus.TRIALING,
    )

    moment = datetime.now(tz=timezone.utc)
    subscription = await uow.subscriptions.grant_bonus_days(
        user.id, days=3, source=SubscriptionSource.MANUAL, now=moment
    )

    assert subscription is not None
    assert subscription.expires_at > moment, (
        "Бонус должен отсчитываться от текущего момента, а не от истёкшей даты"
    )
    assert subscription.expires_at < moment + timedelta(days=4), (
        "Три бонусных дня не должны превращаться в срок от старой даты"
    )
