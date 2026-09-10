"""Защитный контур: скользящее окно, жёсткий интервал и критические действия.

Проверяется не «middleware вызвался», а поведение на границах: что
происходит при одновременном нажатии, при нажатии сразу после успеха,
после исключения в хендлере и при отказе хранилища. Именно эти случаи
отличают работающую защиту от её видимости.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.ratelimit.base import RateLimitRule
from services.ratelimit.sliding import (
    InMemorySlidingWindow,
    RedisSlidingWindow,
    describe,
    validate_cost,
    window_ttl,
)
from tg_bot.flags import CRITICAL_FLAG, CriticalActionFlag, critical, merge, rate_limit
from tg_bot.middlewares.security import (
    CooldownMiddleware,
    CriticalActionMiddleware,
    SecurityConfig,
)


class _FlaggedHandler:
    """Подставной объект хендлера, несущий флаги.

    aiogram извлекает флаги из ``data["handler"].flags``, а не из
    ``data["flags"]``: словарь с ключом ``flags`` — это форма записи при
    регистрации хендлера, а не то, что доезжает до middleware.
    """

    def __init__(self, flags: dict[str, Any]) -> None:
        self.flags = flags


def with_flags(data: dict[str, Any], **flags: Any) -> dict[str, Any]:
    """Возвращает контекст middleware с указанными флагами хендлера."""
    return {**data, "handler": _FlaggedHandler(flags)}


# --------------------------------------------------------------------------- #
# Скользящее окно
# --------------------------------------------------------------------------- #

COOLDOWN_RULE = RateLimitRule(limit=1, window=0.5, scope="test-cooldown")
WINDOW_RULE = RateLimitRule(limit=3, window=1.0, scope="test-window")


@pytest.fixture
def window() -> InMemorySlidingWindow:
    """Скользящее окно в памяти процесса."""
    return InMemorySlidingWindow()


async def test_sliding_window_allows_first_request(window: InMemorySlidingWindow) -> None:
    decision = await window.acquire("user", COOLDOWN_RULE)

    assert decision.allowed, "Первое обращение должно проходить"
    assert decision.retry_after == 0.0


async def test_sliding_window_blocks_immediate_repeat(window: InMemorySlidingWindow) -> None:
    await window.acquire("user", COOLDOWN_RULE)
    decision = await window.acquire("user", COOLDOWN_RULE)

    assert not decision.allowed, "Повтор внутри интервала должен отбиваться"
    assert 0 < decision.retry_after <= COOLDOWN_RULE.window, (
        f"Ожидание должно укладываться в окно, получено {decision.retry_after}"
    )


async def test_sliding_window_allows_after_window_passes(
    window: InMemorySlidingWindow,
) -> None:
    await window.acquire("user", COOLDOWN_RULE)
    await asyncio.sleep(COOLDOWN_RULE.window + 0.05)

    decision = await window.acquire("user", COOLDOWN_RULE)

    assert decision.allowed, "После истечения окна обращение снова разрешено"


async def test_sliding_window_enforces_exact_limit(window: InMemorySlidingWindow) -> None:
    # Ровно N и ни одним больше — то, чего не даёт ведро с жетонами.
    results = [(await window.acquire("user", WINDOW_RULE)).allowed for _ in range(5)]

    assert results == [True, True, True, False, False], (
        f"Окно на {WINDOW_RULE.limit} обращения пропустило {sum(results)}"
    )


async def test_sliding_window_ignores_burst_setting(window: InMemorySlidingWindow) -> None:
    # burst — параметр ведра с жетонами; окно считает обращения и на него
    # не смотрит. Молча разрешать всплеск здесь было бы сюрпризом.
    bursty = RateLimitRule(limit=2, window=1.0, burst=50, scope="test-burst")

    results = [(await window.acquire("user", bursty)).allowed for _ in range(4)]

    assert results == [True, True, False, False]


async def test_sliding_window_separates_keys(window: InMemorySlidingWindow) -> None:
    await window.acquire("first", COOLDOWN_RULE)
    decision = await window.acquire("second", COOLDOWN_RULE)

    assert decision.allowed, "Обращение одного пользователя не должно мешать другому"


async def test_sliding_window_separates_scopes(window: InMemorySlidingWindow) -> None:
    other = RateLimitRule(limit=1, window=0.5, scope="other-scope")
    await window.acquire("user", COOLDOWN_RULE)

    decision = await window.acquire("user", other)

    assert decision.allowed, "Разные области не должны делить счётчик"


async def test_sliding_window_reset_clears_history(window: InMemorySlidingWindow) -> None:
    await window.acquire("user", COOLDOWN_RULE)
    await window.reset("user", COOLDOWN_RULE)

    decision = await window.acquire("user", COOLDOWN_RULE)

    assert decision.allowed, "После сброса окно должно быть пустым"


async def test_sliding_window_rejects_cost_above_limit(window: InMemorySlidingWindow) -> None:
    # Такая операция не поместится никогда: обещать «повторите позже» — ложь.
    decision = await window.acquire("user", WINDOW_RULE, cost=WINDOW_RULE.limit + 1)

    assert not decision.allowed
    assert decision.retry_after == 0.0, "Ждать бессмысленно, и это должно быть видно"


async def test_sliding_window_counts_multi_slot_cost(window: InMemorySlidingWindow) -> None:
    first = await window.acquire("user", WINDOW_RULE, cost=2)
    second = await window.acquire("user", WINDOW_RULE, cost=2)

    assert first.allowed, "Две отметки в окно на три помещаются"
    assert not second.allowed, "Ещё две — уже нет"


@pytest.mark.parametrize("cost", [0, -1, 0.5, 1.5])
def test_validate_cost_rejects_non_positive_and_fractional(cost: float) -> None:
    with pytest.raises(ValueError):
        validate_cost(cost)


def test_window_ttl_covers_window_with_slack() -> None:
    assert window_ttl(WINDOW_RULE) > WINDOW_RULE.window, (
        "Ключ должен жить дольше окна, иначе отметки исчезнут раньше времени"
    )


def test_describe_renders_rule_readably() -> None:
    assert describe(COOLDOWN_RULE) == "1 обращ. / 0.5 с (test-cooldown)"


def test_in_memory_window_rejects_invalid_capacity() -> None:
    with pytest.raises(ValueError):
        InMemorySlidingWindow(max_keys=0)


async def test_in_memory_window_evicts_when_full() -> None:
    tiny = InMemorySlidingWindow(max_keys=3)

    for index in range(10):
        await tiny.acquire(f"user-{index}", COOLDOWN_RULE)

    assert tiny.tracked_keys <= 3, (
        f"Словарь окон не должен расти без границы, накопилось {tiny.tracked_keys}"
    )


# --------------------------------------------------------------------------- #
# Скользящее окно на Redis
# --------------------------------------------------------------------------- #


@pytest.mark.redis
async def test_redis_window_enforces_limit(redis_client: Any) -> None:
    limiter = RedisSlidingWindow(redis_client, prefix="test-sw")

    results = [(await limiter.acquire("user", WINDOW_RULE)).allowed for _ in range(5)]

    assert results == [True, True, True, False, False]


@pytest.mark.redis
async def test_redis_window_counts_each_request_separately(redis_client: Any) -> None:
    # Регрессия: если всем обращениям дать одинаковый член множества,
    # ZADD перезапишет отметку вместо добавления новой, и окно насчитает
    # одно обращение вместо десяти — лимит перестанет работать вовсе.
    limiter = RedisSlidingWindow(redis_client, prefix="test-sw")
    wide = RateLimitRule(limit=100, window=10.0, scope="test-unique")

    for _ in range(10):
        await limiter.acquire("user", wide)

    decision = await limiter.acquire("user", wide)

    assert decision.remaining == pytest.approx(89.0), (
        f"Ожидался остаток 89 после 11 обращений, получено {decision.remaining}"
    )


@pytest.mark.redis
async def test_redis_window_reports_retry_after(redis_client: Any) -> None:
    limiter = RedisSlidingWindow(redis_client, prefix="test-sw")
    await limiter.acquire("user", COOLDOWN_RULE)

    decision = await limiter.acquire("user", COOLDOWN_RULE)

    assert not decision.allowed
    assert 0 < decision.retry_after <= COOLDOWN_RULE.window


@pytest.mark.redis
async def test_redis_window_reset_clears_history(redis_client: Any) -> None:
    limiter = RedisSlidingWindow(redis_client, prefix="test-sw")
    await limiter.acquire("user", COOLDOWN_RULE)

    await limiter.reset("user", COOLDOWN_RULE)

    assert (await limiter.acquire("user", COOLDOWN_RULE)).allowed


@pytest.mark.redis
async def test_redis_window_close_keeps_shared_client_usable(redis_client: Any) -> None:
    # Клиент общий с кэшем языка и очередью переходов: закрыть его здесь
    # значило бы оборвать соединение всем остальным.
    limiter = RedisSlidingWindow(redis_client, prefix="test-sw")

    await limiter.close()

    assert await redis_client.ping(), "Общий клиент должен остаться живым"


# --------------------------------------------------------------------------- #
# Жёсткий интервал
# --------------------------------------------------------------------------- #


@pytest.fixture
def cooldown(window: InMemorySlidingWindow) -> CooldownMiddleware:
    """Middleware жёсткого интервала на окне в памяти."""
    return CooldownMiddleware(window, interval=0.5)


def test_cooldown_rejects_invalid_interval(window: InMemorySlidingWindow) -> None:
    with pytest.raises(ValueError):
        CooldownMiddleware(window, interval=0)


async def test_cooldown_passes_first_message(
    cooldown: CooldownMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_message,
) -> None:
    message = make_message(text="привет")

    result = await cooldown(handler_stub, message, middleware_data)

    assert result == "handler-called"
    handler_stub.assert_awaited_once()


async def test_cooldown_blocks_immediate_second_message(
    cooldown: CooldownMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_message,
) -> None:
    message = make_message(text="привет")
    await cooldown(handler_stub, message, middleware_data)

    result = await cooldown(handler_stub, message, middleware_data)

    assert result is None, "Второе сообщение подряд должно быть отброшено"
    assert handler_stub.await_count == 1, "Хендлер не должен вызываться повторно"


async def test_cooldown_keeps_buttons_available_after_message(
    cooldown: CooldownMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_message,
    make_callback,
) -> None:
    # Сообщения и нажатия считаются раздельно: активная переписка не должна
    # лишать человека возможности нажать кнопку.
    await cooldown(handler_stub, make_message(text="привет"), middleware_data)

    result = await cooldown(handler_stub, make_callback(data="menu:channels"), middleware_data)

    assert result == "handler-called", "Нажатие не должно блокироваться сообщением"


async def test_cooldown_answers_callback_to_clear_spinner(
    cooldown: CooldownMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
    bot,
) -> None:
    # Неотвеченный callback оставляет «часики» на полминуты, и человек
    # решает, что бот завис.
    callback = make_callback(data="menu:channels")
    await cooldown(handler_stub, callback, middleware_data)
    bot.session.requests.clear()

    await cooldown(handler_stub, callback, middleware_data)

    from aiogram.methods import AnswerCallbackQuery

    assert bot.session.requests_of(AnswerCallbackQuery), (
        "На отклонённое нажатие обязателен ответ"
    )


async def test_cooldown_stays_silent_for_blocked_message(
    cooldown: CooldownMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_message,
    bot,
) -> None:
    # Отвечать на каждое частое сообщение значит самому стать флудером.
    message = make_message(text="привет")
    await cooldown(handler_stub, message, middleware_data)
    bot.session.requests.clear()

    await cooldown(handler_stub, message, middleware_data)

    from aiogram.methods import SendMessage

    assert not bot.session.requests_of(SendMessage), (
        "На частое сообщение бот отвечать не должен"
    )


async def test_cooldown_honours_skip_flag(
    cooldown: CooldownMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_message,
) -> None:
    # Служебные обработчики (ответ на PreCheckoutQuery) обязаны отвечать
    # немедленно: Telegram ждёт не дольше десяти секунд.
    message = make_message(text="привет")
    data = with_flags(middleware_data, skip_throttling=True)

    await cooldown(handler_stub, message, data)
    result = await cooldown(handler_stub, message, data)

    assert result == "handler-called", "Флаг пропуска должен снимать и жёсткий интервал"
    assert handler_stub.await_count == 2


async def test_cooldown_passes_update_without_user(
    cooldown: CooldownMiddleware,
    handler_stub: AsyncMock,
    make_message,
) -> None:
    result = await cooldown(handler_stub, make_message(text="привет"), {})

    assert result == "handler-called", "Ограничивать обновление без автора не по чему"


async def test_cooldown_fails_open_when_storage_breaks(
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_message,
) -> None:
    # Отказ хранилища не повод переставать обслуживать людей: бюджетный
    # лимит выше по цепочке продолжает действовать.
    broken = AsyncMock()
    broken.acquire.side_effect = RuntimeError("хранилище недоступно")
    middleware = CooldownMiddleware(broken, interval=0.5)

    result = await middleware(handler_stub, make_message(text="привет"), middleware_data)

    assert result == "handler-called", "При сбое проверки обращение должно проходить"


# --------------------------------------------------------------------------- #
# Критические действия
# --------------------------------------------------------------------------- #

FAST_CONFIG = SecurityConfig(cooldown=0.5, lock_ttl=5.0, action_cooldown=0.4)


@pytest.fixture
def guarded(limiter) -> CriticalActionMiddleware:
    """Middleware критических действий на хранилище в памяти."""
    return CriticalActionMiddleware(limiter, FAST_CONFIG)


def _critical_data(base: dict[str, Any], name: str = "invoice") -> dict[str, Any]:
    """Контекст с флагом критического действия."""
    return with_flags(base, **{CRITICAL_FLAG: CriticalActionFlag(name=name)})


async def test_critical_passes_unmarked_handler(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    result = await guarded(handler_stub, make_callback(data="menu:channels"), middleware_data)

    assert result == "handler-called", "Без флага защита вмешиваться не должна"


async def test_critical_runs_marked_handler(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    callback = make_callback(data="pay:stars:pro_1m")

    result = await guarded(handler_stub, callback, _critical_data(middleware_data))

    assert result == "handler-called"
    handler_stub.assert_awaited_once()


async def test_critical_blocks_concurrent_second_press(
    guarded: CriticalActionMiddleware,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    # Классическая гонка: два нажатия успевают войти в хендлер разом.
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(event: Any, data: dict[str, Any]) -> str:
        started.set()
        await release.wait()
        return "handler-called"

    callback = make_callback(data="pay:stars:pro_1m")
    data = _critical_data(middleware_data)

    first = asyncio.create_task(guarded(slow_handler, callback, data))
    await started.wait()

    second = await guarded(slow_handler, callback, data)
    release.set()

    assert second is None, "Второе нажатие во время обработки должно отбиваться"
    assert await first == "handler-called", "Первое должно доработать нормально"


async def test_critical_blocks_repeat_right_after_success(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    # Главное отличие от защиты одиночного запуска: повтор приходит уже
    # после того, как хендлер отработал и блокировка снята.
    callback = make_callback(data="pay:stars:pro_1m")
    data = _critical_data(middleware_data)

    await guarded(handler_stub, callback, data)
    result = await guarded(handler_stub, callback, data)

    assert result is None, "Повтор сразу после успеха должен отбиваться паузой"
    assert handler_stub.await_count == 1, "Второй счёт выставляться не должен"


async def test_critical_allows_repeat_after_cooldown_expires(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    callback = make_callback(data="pay:stars:pro_1m")
    data = _critical_data(middleware_data)

    await guarded(handler_stub, callback, data)
    await asyncio.sleep(FAST_CONFIG.action_cooldown + 0.1)
    result = await guarded(handler_stub, callback, data)

    assert result == "handler-called", "После паузы действие снова доступно"
    assert handler_stub.await_count == 2


async def test_critical_allows_immediate_retry_after_failure(
    guarded: CriticalActionMiddleware,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    # Неудачную попытку человек должен иметь возможность повторить сразу:
    # пауза после провала наказывала бы за чужой сбой.
    attempts = 0

    async def flaky(event: Any, data: dict[str, Any]) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("платёжный шлюз не ответил")
        return "handler-called"

    callback = make_callback(data="pay:stars:pro_1m")
    data = _critical_data(middleware_data)

    with pytest.raises(RuntimeError):
        await guarded(flaky, callback, data)

    result = await guarded(flaky, callback, data)

    assert result == "handler-called", "Повтор после ошибки должен проходить сразу"
    assert attempts == 2


async def test_critical_reraises_handler_exception(
    guarded: CriticalActionMiddleware,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    # Исключение обязано дойти до обработчика ошибок aiogram, иначе сбой
    # оплаты останется незамеченным.
    async def failing(event: Any, data: dict[str, Any]) -> str:
        raise ValueError("сбой")

    with pytest.raises(ValueError):
        await guarded(failing, make_callback(data="pay:x"), _critical_data(middleware_data))


async def test_critical_separates_different_actions(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    callback = make_callback(data="pay:stars:pro_1m")

    await guarded(handler_stub, callback, _critical_data(middleware_data, "invoice"))
    result = await guarded(handler_stub, callback, _critical_data(middleware_data, "trial"))

    assert result == "handler-called", "Разные действия не должны делить блокировку"


async def test_critical_separates_users(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    from aiogram.types import User as TelegramUser

    callback = make_callback(data="pay:stars:pro_1m")
    await guarded(handler_stub, callback, _critical_data(middleware_data))

    other = TelegramUser(id=999_111, is_bot=False, first_name="Другой")
    result = await guarded(
        handler_stub, callback, _critical_data({"event_from_user": other})
    )

    assert result == "handler-called", "Действие одного не должно блокировать другого"


async def test_critical_passes_update_without_user(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    make_callback,
) -> None:
    result = await guarded(
        handler_stub,
        make_callback(data="pay:x"),
        with_flags({}, **{CRITICAL_FLAG: CriticalActionFlag(name="invoice")}),
    )

    assert result == "handler-called"


async def test_critical_survives_malformed_flag(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    # Ошибка разработчика не должна ни ронять обработку, ни оставлять
    # критическое действие без защиты вовсе.
    callback = make_callback(data="pay:x")
    data = with_flags(middleware_data, **{CRITICAL_FLAG: "invoice"})

    first = await guarded(handler_stub, callback, data)
    second = await guarded(handler_stub, callback, data)

    assert first == "handler-called"
    assert second is None, "Даже при кривом флаге повтор должен отбиваться"


async def test_critical_fails_open_by_default(
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    # При недоступном хранилище отказ означал бы неработающую оплату,
    # тогда как повторный счёт отсекается уникальным ключом в базе.
    broken = AsyncMock()
    broken.ttl.side_effect = RuntimeError("хранилище недоступно")
    broken.acquire_once.side_effect = RuntimeError("хранилище недоступно")
    middleware = CriticalActionMiddleware(broken, FAST_CONFIG)

    result = await middleware(
        handler_stub, make_callback(data="pay:x"), _critical_data(middleware_data)
    )

    assert result == "handler-called", "По умолчанию сбой хранилища не блокирует оплату"


async def test_critical_fails_closed_when_configured(
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    broken = AsyncMock()
    broken.ttl.side_effect = RuntimeError("хранилище недоступно")
    broken.acquire_once.side_effect = RuntimeError("хранилище недоступно")
    strict = SecurityConfig(lock_ttl=5.0, action_cooldown=1.0, fail_closed=True)
    middleware = CriticalActionMiddleware(broken, strict)

    result = await middleware(
        handler_stub, make_callback(data="pay:x"), _critical_data(middleware_data)
    )

    assert result is None, "В строгом режиме сбой хранилища должен блокировать действие"
    handler_stub.assert_not_awaited()


async def test_critical_uses_per_handler_cooldown(
    limiter,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
) -> None:
    middleware = CriticalActionMiddleware(limiter, FAST_CONFIG)
    callback = make_callback(data="pay:x")
    data = with_flags(
        middleware_data, **{CRITICAL_FLAG: CriticalActionFlag(name="invoice", cooldown=0.2)}
    )

    await middleware(handler_stub, callback, data)
    await asyncio.sleep(0.3)
    result = await middleware(handler_stub, callback, data)

    assert result == "handler-called", "Пауза хендлера должна перекрывать общую настройку"



@pytest.fixture
def russian() -> Any:
    """Локализатор на русском — отказ должен быть на языке пользователя."""
    from db.enums import Language
    from services.i18n import TranslationManager, Translator

    return Translator(TranslationManager.from_directory(), Language.RU)


async def test_critical_explains_refusal_in_chat(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_message,
    russian: Any,
    bot,
) -> None:
    # Молчание здесь недопустимо: человек ждёт счёт или пробный период и
    # без ответа решит, что кнопка сломана. Заодно это объясняет, почему
    # считать «сколько сообщений отправил бот» бесполезно — отказ тоже
    # сообщение.
    from aiogram.methods import SendMessage
    from tg_bot.middlewares.i18n import I18N_KEY

    message = make_message(text="/trial")
    data = {**_critical_data(middleware_data, "trial"), I18N_KEY: russian}

    await guarded(handler_stub, message, data)
    bot.session.requests.clear()
    result = await guarded(handler_stub, message, data)

    sent = bot.session.requests_of(SendMessage)
    assert result is None, "Повтор должен быть отбит"
    assert sent, "Отказ обязан быть объяснён"
    assert "уже выполняется" in sent[0].text, (
        f"Ожидалось пояснение об идущей операции, получено: {sent[0].text!r}"
    )


async def test_critical_refusal_shows_alert_on_button(
    guarded: CriticalActionMiddleware,
    handler_stub: AsyncMock,
    middleware_data: dict[str, Any],
    make_callback,
    russian: Any,
    bot,
) -> None:
    from aiogram.methods import AnswerCallbackQuery
    from tg_bot.middlewares.i18n import I18N_KEY

    callback = make_callback(data="pay:stars:pro_1m")
    data = {**_critical_data(middleware_data), I18N_KEY: russian}

    await guarded(handler_stub, callback, data)
    bot.session.requests.clear()
    await guarded(handler_stub, callback, data)

    answers = bot.session.requests_of(AnswerCallbackQuery)
    assert answers, "«Часики» на кнопке нужно погасить в любом случае"
    assert answers[0].show_alert, (
        "У критического действия отказ показывается всплывающим окном: "
        "подпись под кнопкой человек часто не замечает"
    )



async def test_trial_offer_and_grant_use_separate_locks() -> None:
    """Показ предложения и выдача триала не должны делить блокировку.

    Регрессия. Когда оба хендлера были помечены одним именем, пауза после
    показа предложения блокировала приём номера телефона: человек жмёт
    «Поделиться номером» через секунду-две, то есть внутри паузы, и
    основной сценарий выдачи пробного периода переставал работать.

    Защита при этом нужна обоим: при отключённом запросе контакта триал
    выдаётся прямо во входном хендлере, без второго шага.
    """
    from tg_bot.handlers.trial import router

    handlers = list(router.message.handlers) + list(router.callback_query.handlers)
    names = {
        handler.callback.__name__: handler.flags[CRITICAL_FLAG].name
        for handler in handlers
        if CRITICAL_FLAG in handler.flags
    }

    assert names.get("process_contact"), "Выдача триала обязана быть защищена"
    assert names.get("cmd_trial"), "Вход в сценарий тоже защищается"
    assert names["cmd_trial"] != names["process_contact"], (
        f"Вход и выдача делят имя {names['cmd_trial']!r} — пауза после "
        "предложения заблокирует приём контакта"
    )
    assert names.get("start_trial") == names["cmd_trial"], (
        "Оба входа в сценарий должны делить одну блокировку между собой"
    )


async def test_invoice_handler_is_marked_critical() -> None:
    """Выставление счёта — критическое действие, повтор создаёт второй счёт."""
    from tg_bot.handlers.billing import router

    marked = {
        handler.callback.__name__
        for handler in router.callback_query.handlers
        if CRITICAL_FLAG in handler.flags
    }

    assert "send_invoice" in marked, (
        "Хендлер выставления счёта должен быть помечен critical()"
    )


# --------------------------------------------------------------------------- #
# Настройки и флаги
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cooldown", "lock_ttl", "action_cooldown"),
    [
        (0, 30.0, 5.0),
        (0.5, 0, 5.0),
        (0.5, 30.0, 0),
        (0.5, 5.0, 10.0),
    ],
    ids=["нулевой_интервал", "нулевая_блокировка", "нулевая_пауза", "пауза_длиннее_блокировки"],
)
def test_security_config_rejects_inconsistent_values(
    cooldown: float, lock_ttl: float, action_cooldown: float
) -> None:
    with pytest.raises(ValueError):
        SecurityConfig(cooldown=cooldown, lock_ttl=lock_ttl, action_cooldown=action_cooldown)


def test_security_config_defaults_are_consistent() -> None:
    config = SecurityConfig()

    assert config.action_cooldown <= config.lock_ttl
    assert not config.fail_closed, "По умолчанию выбирается доступность оплаты"


def test_merge_combines_flag_sets() -> None:
    # Каждый помощник возвращает свой словарь с ключом flags, и распаковать
    # два таких в один вызов Python не даст.
    combined = merge(rate_limit(5, 60, scope="invoice"), critical("invoice"))

    assert set(combined["flags"]) == {"rate_limit", CRITICAL_FLAG}


def test_merge_of_nothing_is_empty() -> None:
    assert merge() == {"flags": {}}


def test_critical_flag_carries_name_and_cooldown() -> None:
    flag = critical("invoice", cooldown=7)["flags"][CRITICAL_FLAG]

    assert flag.name == "invoice"
    assert flag.cooldown == 7


def test_critical_flag_trims_name() -> None:
    assert critical("  invoice  ")["flags"][CRITICAL_FLAG].name == "invoice"


@pytest.mark.parametrize(("name", "cooldown"), [("", None), ("   ", None), ("ok", 0), ("ok", -1)])
def test_critical_rejects_invalid_arguments(name: str, cooldown: float | None) -> None:
    with pytest.raises(ValueError):
        critical(name, cooldown=cooldown)
