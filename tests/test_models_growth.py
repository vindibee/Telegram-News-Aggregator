"""Тесты моделей роста: каналы, рефералы, промокоды, трекинг кликов.

Проверяются два разных слоя защиты. Первый — методы моделей: нормализация
ввода, переходы состояний, расчёты. Второй — ограничения самой БД: они
последний рубеж, и если прикладной код однажды ошибётся, именно они не
дадут записать бессмыслицу.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from db.enums import ChannelKind, Language, PromocodeKind, ReferralStatus, SubscriptionPlan
from db.exceptions import InvalidStateTransitionError
from db.models import ClickLog, Promocode, PromocodeRedemption, Referral, TrackedLink, User, UserChannel
from tests.conftest import FROZEN_NOW, TELEGRAM_ID

SECRET = "test-secret"


def _channel(user_id: int, **overrides: object) -> UserChannel:
    """Строит канал пользователя с разумными значениями по умолчанию."""
    defaults: dict[str, object] = {
        "user_id": user_id,
        "kind": ChannelKind.SOURCE,
        "username": "habr_com",
        "title": "Хабр",
    }
    defaults.update(overrides)
    return UserChannel(**defaults)  # type: ignore[arg-type]


def _referral(**overrides: object) -> Referral:
    """Строит реферала в состоянии, эквивалентном только что вставленному.

    Значения ``default=`` проставляет INSERT, поэтому объект «из воздуха»
    приходит с ``None`` в статусе и счётчиках. Тесты конечного автомата
    работают с транзиентным объектом, и начальное состояние задаётся явно.
    """
    defaults: dict[str, object] = {
        "referrer_id": 1,
        "referred_id": 2,
        "code": "ABC123",
        "status": ReferralStatus.PENDING,
        "bonus_days": 0,
    }
    defaults.update(overrides)
    return Referral(**defaults)  # type: ignore[arg-type]


def _promocode(**overrides: object) -> Promocode:
    """Строит промокод на бонусные дни."""
    defaults: dict[str, object] = {
        "code": "WELCOME7",
        "kind": PromocodeKind.BONUS_DAYS,
        "value": 7,
        "activations": 0,
        "is_active": True,
    }
    defaults.update(overrides)
    return Promocode(**defaults)  # type: ignore[arg-type]


def _link(**overrides: object) -> TrackedLink:
    """Строит короткую ссылку."""
    defaults: dict[str, object] = {
        "token": "abcdefgh1234",
        "target_url": "https://example.com/landing",
        "clicks": 0,
        "unique_clicks": 0,
        "is_active": True,
    }
    defaults.update(overrides)
    return TrackedLink(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Язык интерфейса
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ru", Language.RU),
        ("en", Language.EN),
        ("en-US", Language.EN),
        ("EN-gb", Language.EN),
        ("de", Language.RU),
        ("", Language.RU),
        (None, Language.RU),
    ],
)
def test_language_from_telegram_maps_client_code_to_supported_language(
    code: str | None,
    expected: Language,
) -> None:
    assert Language.from_telegram(code) is expected, f"Код {code!r} разобран неверно"


def test_apply_language_reports_change_only_when_value_differs() -> None:
    user = User(telegram_id=1, referral_code="ABC123", language=Language.RU)

    assert user.apply_language(Language.EN) is True, "Смена языка должна возвращать True"
    assert user.language is Language.EN
    assert user.apply_language(Language.EN) is False, "Повторная установка не является изменением"


# --------------------------------------------------------------------------- #
# Каналы пользователя
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "habr_com",
        "@habr_com",
        "t.me/habr_com",
        "https://t.me/habr_com",
        "  https://t.me/habr_com/  ",
        "telegram.me/habr_com?single",
    ],
)
def test_normalize_username_reduces_any_input_form_to_bare_name(raw: str) -> None:
    # Без нормализации ограничение уникальности не сработает: "@habr_com"
    # и "t.me/habr_com" попали бы в базу как разные каналы.
    assert UserChannel.normalize_username(raw) == "habr_com", f"Не разобрано: {raw!r}"


@pytest.mark.parametrize("raw", ["", "@", "ab", "т_канал", "name with space", "1channel"])
def test_normalize_username_rejects_values_telegram_would_not_accept(raw: str) -> None:
    with pytest.raises(ValueError):
        UserChannel.normalize_username(raw)


def test_channel_display_name_falls_back_from_title_to_username_to_id() -> None:
    assert _channel(1, title="Хабр").display_name == "Хабр"
    assert _channel(1, title="").display_name == "@habr_com"
    assert _channel(1, title="", username=None, chat_id=-100500).display_name == "id-100500"


def test_channel_is_publishable_only_for_verified_active_target() -> None:
    target = _channel(
        1, kind=ChannelKind.TARGET, chat_id=-100500, bot_is_admin=True, is_active=True
    )
    assert target.is_publishable, "Проверенная активная цель должна быть готова к публикации"

    assert not _channel(1, kind=ChannelKind.SOURCE, chat_id=-1).is_publishable, (
        "Источник не является целью публикации"
    )
    target.bot_is_admin = False
    assert not target.is_publishable, "Без подтверждения прав публиковать нельзя"


def test_channel_mark_failed_truncates_long_error_and_deactivate_keeps_reason() -> None:
    channel = _channel(1)
    channel.mark_failed("x" * 900)

    assert len(channel.last_error) == 500, "Текст ошибки должен обрезаться"

    channel.deactivate("канал удалён")
    assert channel.is_active is False
    assert channel.last_error == "канал удалён"


def test_channel_mark_synced_clears_previous_error() -> None:
    channel = _channel(1)
    channel.mark_failed("временный сбой")

    channel.mark_synced(FROZEN_NOW)

    assert channel.last_error is None, "Успешная синхронизация должна снимать прошлую ошибку"
    assert channel.last_synced_at == FROZEN_NOW


@pytest.mark.db
async def test_channel_duplicate_username_for_same_role_is_rejected_by_database(
    db_session,
    user: User,
) -> None:
    db_session.add(_channel(user.id))
    await db_session.commit()

    db_session.add(_channel(user.id))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_channel_same_name_in_different_roles_is_allowed(db_session, user: User) -> None:
    # Один канал можно и читать, и публиковать в него — роль входит в ключ.
    db_session.add(_channel(user.id, kind=ChannelKind.SOURCE))
    db_session.add(_channel(user.id, kind=ChannelKind.TARGET, chat_id=-100500))

    await db_session.commit()

    assert True, "Оба канала должны сохраниться"


@pytest.mark.db
async def test_channel_target_without_chat_id_is_rejected_by_database(
    db_session,
    user: User,
) -> None:
    db_session.add(_channel(user.id, kind=ChannelKind.TARGET, chat_id=None))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_channel_without_any_identifier_is_rejected_by_database(
    db_session,
    user: User,
) -> None:
    db_session.add(_channel(user.id, username=None, chat_id=None))

    with pytest.raises(IntegrityError):
        await db_session.commit()


# --------------------------------------------------------------------------- #
# Рефералы
# --------------------------------------------------------------------------- #


def test_referral_qualify_then_reward_walks_the_expected_states() -> None:
    referral = _referral()

    assert referral.qualify(payment_id=10, moment=FROZEN_NOW) is True
    assert referral.status is ReferralStatus.QUALIFIED
    assert referral.payment_id == 10

    assert referral.reward(days=14, moment=FROZEN_NOW) is True
    assert referral.status is ReferralStatus.REWARDED
    assert referral.bonus_days == 14
    assert referral.is_final, "Вознаграждённый реферал — конечное состояние"


def test_referral_qualify_is_idempotent_for_repeated_payment_events() -> None:
    # Повторная доставка вебхука оплаты — норма, а не сбой.
    referral = _referral()
    referral.qualify(payment_id=10, moment=FROZEN_NOW)

    assert referral.qualify(payment_id=10, moment=FROZEN_NOW) is False
    assert referral.payment_id == 10, "Повтор не должен подменять платёж"


def test_referral_reward_without_qualification_grants_bonus_immediately() -> None:
    # Бонус выдаётся сразу при переходе по ссылке, без ожидания оплаты:
    # реферальная программа работает как канал привлечения. Путь через
    # qualify() при этом сохранён для политики «бонус после оплаты».
    referral = _referral()

    assert referral.reward(days=3, moment=FROZEN_NOW) is True
    assert referral.status is ReferralStatus.REWARDED
    assert referral.bonus_days == 3
    assert referral.payment_id is None, "Мгновенный бонус не привязан к платежу"
    assert referral.qualified_at is None, "Зачёта по оплате не было"


def test_referral_reward_is_idempotent_for_repeated_start() -> None:
    # Двойной тап по реферальной ссылке — обычное дело, и второй вызов
    # обязан вернуть False, а не поднять ошибку перехода.
    referral = _referral()
    referral.reward(days=3, moment=FROZEN_NOW)

    assert referral.reward(days=3, moment=FROZEN_NOW) is False
    assert referral.bonus_days == 3, "Повтор не должен удваивать бонус"


def test_referral_reward_after_rejection_is_forbidden() -> None:
    # Отклонённое приглашение — конечное состояние: вознаграждать нечего.
    referral = _referral()
    referral.reject(moment=FROZEN_NOW)

    with pytest.raises(InvalidStateTransitionError):
        referral.reward(days=3, moment=FROZEN_NOW)


def test_referral_reward_rejects_non_positive_bonus() -> None:
    referral = _referral()
    referral.qualify(payment_id=10, moment=FROZEN_NOW)

    with pytest.raises(ValueError):
        referral.reward(days=0, moment=FROZEN_NOW)


def test_referral_rewarded_cannot_be_rejected_afterwards() -> None:
    referral = _referral()
    referral.qualify(payment_id=10, moment=FROZEN_NOW)
    referral.reward(days=14, moment=FROZEN_NOW)

    with pytest.raises(InvalidStateTransitionError):
        referral.reject(moment=FROZEN_NOW)


@pytest.mark.db
async def test_referral_same_person_cannot_be_invited_twice(
    db_session,
    user: User,
    make_user,
) -> None:
    # Ключ идемпотентности всей программы: приглашённый учитывается один раз.
    first = await make_user(telegram_id=TELEGRAM_ID + 1)
    second = await make_user(telegram_id=TELEGRAM_ID + 2)

    db_session.add(Referral(referrer_id=first.id, referred_id=user.id, code="AAA111"))
    await db_session.commit()

    db_session.add(Referral(referrer_id=second.id, referred_id=user.id, code="BBB222"))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_referral_self_invitation_is_rejected_by_database(db_session, user: User) -> None:
    db_session.add(Referral(referrer_id=user.id, referred_id=user.id, code="AAA111"))

    with pytest.raises(IntegrityError):
        await db_session.commit()


# --------------------------------------------------------------------------- #
# Промокоды
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("welcome7", "WELCOME7"), (" wel-come7 ", "WELCOME7"), ("WEL COME7", "WELCOME7")],
)
def test_normalize_code_uppercases_and_strips_separators(raw: str, expected: str) -> None:
    assert Promocode.normalize_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "-", "X" * 33])
def test_normalize_code_rejects_empty_and_oversized_input(raw: str) -> None:
    with pytest.raises(ValueError):
        Promocode.normalize_code(raw)


def test_generate_code_produces_unambiguous_characters_only() -> None:
    codes = {Promocode.generate_code() for _ in range(50)}

    assert len(codes) > 45, "Коды должны быть практически всегда разными"
    assert all(set(code) <= set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for code in codes), (
        "В алфавите не должно быть визуально неоднозначных символов"
    )


def test_bonus_days_converts_percent_discount_into_days() -> None:
    percent = _promocode(kind=PromocodeKind.DISCOUNT_PERCENT, value=20)

    assert percent.bonus_days_for(30) == 6, "20 % от 30 дней — это 6 дней"
    assert _promocode(value=7).bonus_days_for(30) == 7, "Код на дни не зависит от периода"


def test_bonus_days_rejects_non_positive_base_period() -> None:
    with pytest.raises(ValueError):
        _promocode().bonus_days_for(0)


def test_is_redeemable_respects_window_plan_and_exhaustion() -> None:
    code = _promocode(
        valid_from=FROZEN_NOW,
        valid_until=FROZEN_NOW + timedelta(days=1),
        max_activations=2,
        plan=SubscriptionPlan.PRO,
    )

    assert code.is_redeemable(FROZEN_NOW, SubscriptionPlan.PRO), "Код в окне должен работать"
    assert not code.is_redeemable(FROZEN_NOW - timedelta(seconds=1)), "До начала — нельзя"
    assert not code.is_redeemable(FROZEN_NOW + timedelta(days=1)), "Верхняя граница исключающая"
    assert not code.is_redeemable(FROZEN_NOW, SubscriptionPlan.BUSINESS), "Чужой тариф — нельзя"

    code.register_activation()
    code.register_activation()
    assert code.is_exhausted, "Лимит должен исчерпаться"
    assert not code.is_redeemable(FROZEN_NOW, SubscriptionPlan.PRO)


def test_register_activation_refuses_to_exceed_the_limit() -> None:
    code = _promocode(max_activations=1)
    code.register_activation()

    with pytest.raises(ValueError):
        code.register_activation()


def test_inactive_code_is_never_redeemable() -> None:
    assert not _promocode(is_active=False).is_redeemable(FROZEN_NOW)


def test_state_machine_methods_require_a_persisted_initial_state() -> None:
    # Документируем поведение явно: значения ``default=`` проставляет
    # INSERT, поэтому вызывать переходы на объекте до сохранения нельзя.
    transient = Referral(referrer_id=1, referred_id=2, code="ABC123")

    assert transient.status is None, "До вставки статус ещё не проставлен"


@pytest.mark.db
async def test_promocode_code_is_unique_across_table(db_session) -> None:
    db_session.add(_promocode())
    await db_session.commit()

    db_session.add(_promocode(value=14))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_promocode_counter_cannot_exceed_limit_even_bypassing_model(db_session) -> None:
    # Последний рубеж: даже если прикладной код ошибётся и запишет счётчик
    # напрямую, база не даст превратить код в бесконечный.
    code = _promocode(max_activations=1)
    code.activations = 5
    db_session.add(code)

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_promocode_redemption_is_unique_per_user(db_session, user: User) -> None:
    code = _promocode()
    db_session.add(code)
    await db_session.commit()

    db_session.add(PromocodeRedemption(promocode_id=code.id, user_id=user.id, days_granted=7))
    await db_session.commit()

    db_session.add(PromocodeRedemption(promocode_id=code.id, user_id=user.id, days_granted=7))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_promocode_discount_above_hundred_percent_is_rejected(db_session) -> None:
    db_session.add(_promocode(kind=PromocodeKind.DISCOUNT_PERCENT, value=150))

    with pytest.raises(IntegrityError):
        await db_session.commit()


# --------------------------------------------------------------------------- #
# Трекинг переходов
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/file",
        "  ",
        "https://example.com/" + "x" * 2100,
    ],
)
def test_validate_target_url_rejects_unsafe_and_oversized_addresses(url: str) -> None:
    # Редирект на javascript: превратил бы короткую ссылку в готовый
    # вектор атаки на каждого, кто по ней перейдёт.
    with pytest.raises(ValueError):
        TrackedLink.validate_target_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://example.com/a", "http://example.com/a?b=1", "  https://example.com/a  "],
)
def test_validate_target_url_accepts_http_and_https(url: str) -> None:
    assert TrackedLink.validate_target_url(url).startswith(("http://", "https://"))


def test_generate_token_has_requested_length_and_is_unpredictable() -> None:
    tokens = {TrackedLink.generate_token() for _ in range(50)}

    assert len(tokens) == 50, "Токены обязаны быть уникальными"
    assert all(len(token) == 12 for token in tokens), "Длина токена должна соблюдаться"


def test_generate_token_rejects_short_length() -> None:
    with pytest.raises(ValueError):
        TrackedLink.generate_token(4)


def test_register_click_updates_totals_and_conversion() -> None:
    link = _link()

    link.register_click(unique=True)
    link.register_click(unique=False)

    assert link.clicks == 2
    assert link.unique_clicks == 1
    assert link.conversion_rate == pytest.approx(0.5)


def test_conversion_rate_is_zero_without_clicks() -> None:
    assert _link().conversion_rate == 0.0


def test_link_availability_accounts_for_flag_and_expiry() -> None:
    link = _link(expires_at=FROZEN_NOW + timedelta(hours=1))

    assert link.is_available(FROZEN_NOW), "Действующая ссылка должна работать"
    assert not link.is_available(FROZEN_NOW + timedelta(hours=2)), "Просроченная — нет"

    link.expires_at = None
    link.is_active = False
    assert not link.is_available(FROZEN_NOW), "Выключенная ссылка не работает и без срока"


def test_visitor_hash_is_stable_irreversible_and_secret_dependent() -> None:
    first = ClickLog.build_visitor_hash("203.0.113.7", "Mozilla/5.0", SECRET)
    same = ClickLog.build_visitor_hash("203.0.113.7", "Mozilla/5.0", SECRET)
    other_secret = ClickLog.build_visitor_hash("203.0.113.7", "Mozilla/5.0", "another")
    other_ip = ClickLog.build_visitor_hash("203.0.113.8", "Mozilla/5.0", SECRET)

    assert first == same, "Отпечаток должен быть детерминированным"
    assert first != other_secret, "Смена секрета обязана менять отпечаток"
    assert first != other_ip, "Разные адреса — разные отпечатки"
    assert len(first) == 64 and "203.0.113.7" not in first, "Адрес не должен быть восстановим"


@pytest.mark.parametrize(("ip", "secret"), [("", SECRET), ("  ", SECRET), ("1.2.3.4", "")])
def test_visitor_hash_requires_address_and_secret(ip: str, secret: str) -> None:
    with pytest.raises(ValueError):
        ClickLog.build_visitor_hash(ip, "UA", secret)


@pytest.mark.db
async def test_click_from_same_visitor_is_counted_once_per_link(db_session) -> None:
    # Уникальность в БД, а не счётчик в коде: обновление страницы не
    # должно раздувать статистику уникальных переходов.
    link = _link()
    db_session.add(link)
    await db_session.commit()

    visitor = ClickLog.build_visitor_hash("203.0.113.7", "Mozilla/5.0", SECRET)
    db_session.add(ClickLog(link_id=link.id, visitor_hash=visitor, is_unique=True))
    await db_session.commit()

    db_session.add(ClickLog(link_id=link.id, visitor_hash=visitor, is_unique=True))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_link_token_is_unique_across_table(db_session) -> None:
    db_session.add(_link())
    await db_session.commit()

    db_session.add(_link(target_url="https://example.com/other"))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_unique_clicks_cannot_exceed_total_clicks(db_session) -> None:
    db_session.add(_link(clicks=1, unique_clicks=5))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_user_language_defaults_to_russian_in_database(db_session, make_user) -> None:
    created = await make_user(telegram_id=TELEGRAM_ID + 77)

    stored = await db_session.get(User, created.id)
    assert stored is not None and stored.language is Language.RU, (
        "Язык по умолчанию должен проставляться на уровне БД"
    )


@pytest.mark.db
async def test_deleting_user_cascades_to_channels_but_keeps_click_logs(
    db_session,
    make_user,
) -> None:
    # Каналы принадлежат пользователю и уходят вместе с ним, а журнал
    # кликов — аналитика: она переживает удаление аккаунта.
    owner = await make_user(telegram_id=TELEGRAM_ID + 88)
    channel = _channel(owner.id)
    link = _link(owner_id=owner.id)
    db_session.add_all([channel, link])
    await db_session.commit()

    click = ClickLog(link_id=link.id, user_id=owner.id, visitor_hash="a" * 64)
    db_session.add(click)
    await db_session.commit()

    await db_session.delete(owner)
    await db_session.commit()

    assert await db_session.get(UserChannel, channel.id) is None, "Каналы должны удаляться каскадом"

    surviving = await db_session.get(ClickLog, click.id)
    assert surviving is not None, "Журнал кликов не должен исчезать вместе с пользователем"
    # SET NULL выполняет сама база, и объект в сессии об этом не знает:
    # без обновления мы бы проверяли устаревшую копию.
    await db_session.refresh(surviving)
    assert surviving.user_id is None, "Ссылка на удалённого пользователя должна обнуляться"
