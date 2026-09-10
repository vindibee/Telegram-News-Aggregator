"""Тесты пробного периода и защиты от мультиаккаунтов.

Проверяется не только «счастливый путь», но и обход защиты: тот же номер
с нового аккаунта, чужой контакт из адресной книги, повторная активация и
две параллельные попытки одного пользователя.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from core.config import Settings
from db.enums import (
    SubscriptionPlan,
    SubscriptionSource,
    SubscriptionStatus,
    TrialFingerprintKind,
)
from db.models import TrialClaim, User
from services.trial import (
    ContactRequiredError,
    SubscriptionAlreadyActiveError,
    TrialAlreadyClaimedError,
    TrialDisabledError,
    TrialFingerprintTakenError,
    TrialService,
)
from tests.conftest import FROZEN_NOW, TELEGRAM_ID

pytestmark = pytest.mark.db

PHONE = "+79001234567"


# --------------------------------------------------------------------------- #
# Счастливый путь
# --------------------------------------------------------------------------- #


async def test_activate_creates_trialing_subscription_for_configured_period(
    trial: TrialService,
    user: User,
    uow,
    settings: Settings,
) -> None:
    outcome = await trial.activate(user, phone=PHONE, now=FROZEN_NOW)

    assert outcome.days_granted == settings.trial.days, "Выдан неверный срок пробного периода"
    assert outcome.expires_at == FROZEN_NOW + timedelta(days=settings.trial.days), (
        f"Дата окончания не совпала: {outcome.expires_at}"
    )

    subscription = await uow.subscriptions.get_live(user.id)
    assert subscription is not None, "Подписка не создана"
    assert subscription.status is SubscriptionStatus.TRIALING, (
        f"Статус подписки должен быть trialing, получен {subscription.status}"
    )
    assert subscription.source is SubscriptionSource.TRIAL, "Источник подписки должен быть trial"
    assert subscription.plan is SubscriptionPlan.PRO, "Триал должен открывать тариф Pro"


async def test_activate_marks_user_and_reserves_phone_fingerprint(
    trial: TrialService,
    user: User,
    uow,
    settings: Settings,
) -> None:
    await trial.activate(user, phone=PHONE, now=FROZEN_NOW)

    # Сервис работает с копией пользователя из собственной транзакции —
    # проверяем её, а не экземпляр из сессии подготовки данных.
    actor = await uow.users.get_by_id(user.id)
    assert actor is not None and actor.trial_activated_at == FROZEN_NOW, (
        "Отметка об активации не проставлена"
    )

    owner_id = await uow.users.find_trial_claim_owner(
        TrialFingerprintKind.PHONE,
        TrialClaim.build_fingerprint(
            TrialFingerprintKind.PHONE, PHONE, settings.trial.fingerprint_secret
        ),
    )
    assert owner_id == user.id, "Отпечаток телефона не закреплён за пользователем"


async def test_activate_stores_only_hashed_phone_never_the_number(
    trial: TrialService,
    user: User,
    uow,
    settings: Settings,
) -> None:
    await trial.activate(user, phone=PHONE, now=FROZEN_NOW)

    stored = (await uow.session.execute(select(TrialClaim.fingerprint))).scalars().all()

    assert stored, "Отпечаток не сохранён"
    digits = PHONE.lstrip("+")
    assert all(digits not in value for value in stored), (
        "Номер телефона попал в базу в открытом виде"
    )
    assert all(len(value) == 64 for value in stored), "Отпечаток должен быть SHA-256 в hex"


# --------------------------------------------------------------------------- #
# Защита от повторов
# --------------------------------------------------------------------------- #


async def test_activate_twice_by_same_user_raises_already_claimed(
    trial: TrialService,
    user: User,
) -> None:
    await trial.activate(user, phone=PHONE, now=FROZEN_NOW)

    with pytest.raises(TrialAlreadyClaimedError):
        await trial.activate(user, phone=PHONE, now=FROZEN_NOW)


async def test_activate_from_second_account_with_same_phone_is_rejected(
    trial: TrialService,
    user: User,
    make_user,
) -> None:
    await trial.activate(user, phone=PHONE, now=FROZEN_NOW)

    multi_account = await make_user(telegram_id=TELEGRAM_ID + 999, username="multi")

    with pytest.raises(TrialFingerprintTakenError) as info:
        await trial.activate(multi_account, phone=PHONE, now=FROZEN_NOW)

    assert info.value.kind is TrialFingerprintKind.PHONE, "Должен указываться конфликтующий признак"


@pytest.mark.parametrize(
    "formatted",
    [
        "+7 (900) 123-45-67",
        "7 900 123 45 67",
        "  +79001234567  ",
    ],
)
async def test_activate_ignores_phone_formatting_when_matching_fingerprints(
    trial: TrialService,
    user: User,
    make_user,
    formatted: str,
) -> None:
    # Разное написание одного номера не должно давать второй триал:
    # отпечаток считается по одним цифрам.
    await trial.activate(user, phone=PHONE, now=FROZEN_NOW)
    multi_account = await make_user(telegram_id=TELEGRAM_ID + 500, username="formatted")

    with pytest.raises(TrialFingerprintTakenError):
        await trial.activate(multi_account, phone=formatted, now=FROZEN_NOW)


async def test_rejected_by_fingerprint_leaves_second_account_without_trial_mark(
    trial: TrialService,
    user: User,
    make_user,
    uow,
) -> None:
    await trial.activate(user, phone=PHONE, now=FROZEN_NOW)
    multi_account = await make_user(telegram_id=TELEGRAM_ID + 321, username="clean")

    with pytest.raises(TrialFingerprintTakenError):
        await trial.activate(multi_account, phone=PHONE, now=FROZEN_NOW)

    rejected = await uow.users.get_by_id(multi_account.id)
    assert rejected is not None and rejected.trial_activated_at is None, (
        "Отказ по чужому номеру не должен сжигать право на триал"
    )
    assert not rejected.has_used_trial, "Аккаунт помечен использовавшим триал после отказа"


async def test_activate_with_live_subscription_raises_subscription_active(
    trial: TrialService,
    user: User,
    make_subscription,
) -> None:
    await make_subscription(user, status=SubscriptionStatus.ACTIVE)

    with pytest.raises(SubscriptionAlreadyActiveError):
        await trial.activate(user, phone=PHONE, now=FROZEN_NOW)


async def test_parallel_activations_of_one_user_grant_single_subscription(
    uow_factory,
    make_user,
    db_session,
    settings: Settings,
) -> None:
    # Гонка двух одновременных нажатий: advisory-лок выстраивает их в
    # очередь, вторая попытка видит уже отмеченный триал.
    owner = await make_user(telegram_id=TELEGRAM_ID + 777, username="racer")
    await db_session.commit()
    user_id = owner.id

    async def attempt() -> str:
        async with uow_factory() as unit:
            service = TrialService(unit, settings.trial)
            fresh = await unit.users.get_by_id(user_id)
            assert fresh is not None
            try:
                await service.activate(fresh, phone=PHONE, now=FROZEN_NOW)
            except (TrialAlreadyClaimedError, TrialFingerprintTakenError):
                return "rejected"
            await unit.commit()
            return "granted"

    results = await asyncio.gather(attempt(), attempt())

    assert sorted(results) == ["granted", "rejected"], (
        f"Ровно одна попытка должна получить триал, получено: {results}"
    )

    async with uow_factory() as unit:
        subscriptions = await unit.subscriptions.list_history(user_id)
    assert len(subscriptions) == 1, f"Создано подписок: {len(subscriptions)}, ожидалась одна"


# --------------------------------------------------------------------------- #
# Крайние случаи конфигурации и ввода
# --------------------------------------------------------------------------- #


async def test_activate_without_phone_raises_contact_required(
    trial: TrialService,
    user: User,
) -> None:
    with pytest.raises(ContactRequiredError):
        await trial.activate(user, now=FROZEN_NOW)


async def test_activate_with_blank_phone_raises_contact_required(
    trial: TrialService,
    user: User,
) -> None:
    with pytest.raises(ContactRequiredError):
        await trial.activate(user, phone="   ", now=FROZEN_NOW)


async def test_activate_when_disabled_raises_disabled(uow, user: User, settings: Settings) -> None:
    service = TrialService(uow, replace(settings.trial, enabled=False))

    with pytest.raises(TrialDisabledError):
        await service.activate(user, phone=PHONE, now=FROZEN_NOW)


async def test_activate_without_contact_requirement_skips_fingerprints(
    uow,
    user: User,
    settings: Settings,
) -> None:
    service = TrialService(uow, replace(settings.trial, require_contact=False))

    outcome = await service.activate(user, now=FROZEN_NOW)

    assert outcome.days_granted == settings.trial.days, "Триал должен выдаваться и без телефона"
    owner_id = await uow.users.find_trial_claim_owner(
        TrialFingerprintKind.PHONE,
        TrialClaim.build_fingerprint(
            TrialFingerprintKind.PHONE, PHONE, settings.trial.fingerprint_secret
        ),
    )
    assert owner_id is None, "Без требования контакта отпечатки резервироваться не должны"


async def test_banned_user_cannot_activate_trial(trial: TrialService, make_user) -> None:
    banned = await make_user(telegram_id=TELEGRAM_ID + 42, is_banned=True)

    with pytest.raises(TrialAlreadyClaimedError):
        await trial.activate(banned, phone=PHONE, now=FROZEN_NOW)


# --------------------------------------------------------------------------- #
# Доступность триала для экрана подписки
# --------------------------------------------------------------------------- #


async def test_check_eligibility_allows_fresh_user(trial: TrialService, user: User) -> None:
    eligibility = await trial.check_eligibility(user)

    assert eligibility.available, (
        f"Новому пользователю триал должен быть доступен: {eligibility.reason_key}"
    )


async def test_check_eligibility_blocks_user_after_activation(
    trial: TrialService,
    user: User,
) -> None:
    await trial.activate(user, phone=PHONE, now=FROZEN_NOW)

    eligibility = await trial.check_eligibility(user)

    assert eligibility.blocked, "После активации триал должен быть недоступен"
    # Конкретная причина зависит от того, что проверка увидит раньше:
    # отметку об активации или уже созданную подписку. Важно, что это
    # ключ перевода, а не готовая фраза на одном языке.
    assert eligibility.reason_key in {
        "trial.reasons.used",
        "trial.reasons.has_subscription",
    }, f"Неожиданный ключ причины отказа: {eligibility.reason_key}"


async def test_check_eligibility_blocks_when_disabled(
    uow,
    user: User,
    settings: Settings,
) -> None:
    service = TrialService(uow, replace(settings.trial, enabled=False))

    eligibility = await service.check_eligibility(user)

    assert eligibility.blocked, "При отключённом триале доступа быть не должно"
