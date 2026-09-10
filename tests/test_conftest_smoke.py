"""Проверка самого тестового стенда.

Фикстуры — такой же код, как и всё остальное, и ошибка в них даёт либо
зелёные тесты, ничего не проверяющие, либо каскад непонятных падений.
Этот модуль проверяет каркас: изоляцию тестов друг от друга, подменённый
транспорт Telegram, Redis-бэкенд и заморозку времени.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery, SendMessage
from sqlalchemy import func, select

from db.models import User
from services.ratelimit.base import RateLimitRule
from services.ratelimit.policy import FloodAction
from tests.conftest import FROZEN_NOW, TELEGRAM_ID

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


async def test_mocked_bot_message_answer_records_request_without_network(bot, make_message) -> None:
    message = make_message("/start")

    await message.answer("Привет", parse_mode="HTML")

    request = bot.session.pop_request()
    assert isinstance(request, SendMessage), f"Ожидался SendMessage, получен {type(request).__name__}"
    assert request.text == "Привет", f"Текст ответа не совпал: {request.text!r}"
    assert request.chat_id == message.chat.id, "Ответ ушёл не в тот чат"


async def test_mocked_bot_callback_answer_returns_true_by_default(bot, make_callback) -> None:
    callback = make_callback("menu:plans")

    result = await callback.answer("Готово")

    assert result is True, "Ответ на callback должен возвращать True"
    assert bot.session.requests_of(AnswerCallbackQuery), "Запрос AnswerCallbackQuery не зафиксирован"


async def test_mocked_bot_prepared_error_raises_real_aiogram_exception(bot, make_message) -> None:
    bot.add_result_for(
        SendMessage,
        ok=False,
        error_code=403,
        description="Forbidden: bot was blocked by the user",
    )
    message = make_message("/start")

    with pytest.raises(TelegramForbiddenError):
        await message.answer("Привет")


async def test_make_successful_payment_builds_message_with_payment_payload(
    make_successful_payment,
) -> None:
    message = make_successful_payment("inv-1", charge_id="charge-42", total_amount=150)

    assert message.successful_payment is not None, "Сообщение должно содержать successful_payment"
    assert message.successful_payment.telegram_payment_charge_id == "charge-42"
    assert message.successful_payment.currency == "XTR"


async def test_fsm_context_stores_and_clears_state(fsm_context) -> None:
    await fsm_context.set_state("waiting_for_channel")
    await fsm_context.update_data(channel="habr_com")

    assert await fsm_context.get_state() == "waiting_for_channel", "Состояние не сохранилось"
    assert (await fsm_context.get_data())["channel"] == "habr_com", "Данные FSM не сохранились"

    await fsm_context.clear()
    assert await fsm_context.get_state() is None, "Состояние не очистилось"


# --------------------------------------------------------------------------- #
# Ограничение частоты
# --------------------------------------------------------------------------- #


async def test_in_memory_limiter_blocks_after_burst_is_spent(limiter) -> None:
    rule = RateLimitRule(limit=2, window=60.0, scope="smoke")

    first = await limiter.acquire("user", rule)
    second = await limiter.acquire("user", rule)
    third = await limiter.acquire("user", rule)

    assert first.allowed and second.allowed, "Первые две операции должны проходить"
    assert not third.allowed, "Третья операция обязана быть отклонена лимитом"
    assert third.retry_after_seconds > 0, "Отказ без времени повтора бесполезен клиенту"


async def test_flood_policy_escalates_to_mute_after_repeated_violations(
    flood_policy,
    message_rule,
) -> None:
    # Тестовое окружение: лимит 3 сообщения, заглушка после 2 нарушений.
    for _ in range(message_rule.limit):
        await flood_policy.check(TELEGRAM_ID, message_rule)

    verdicts = [await flood_policy.check(TELEGRAM_ID, message_rule) for _ in range(3)]

    assert verdicts[0].action is FloodAction.THROTTLE, "Первое превышение — предупреждение"
    assert any(v.action is FloodAction.MUTE for v in verdicts), "Повторный флуд должен привести к заглушке"


@pytest.mark.redis
async def test_redis_limiter_executes_token_bucket_script(redis_limiter) -> None:
    rule = RateLimitRule(limit=1, window=60.0, scope="smoke")

    allowed = await redis_limiter.acquire("user", rule)
    blocked = await redis_limiter.acquire("user", rule)

    assert allowed.allowed, "Первая операция должна проходить"
    assert not blocked.allowed, "Вторая операция обязана быть отклонена"


@pytest.mark.redis
async def test_redis_guard_releases_lock_only_with_matching_token(redis_limiter) -> None:
    token = await redis_limiter.acquire_once("smoke-key", 30.0)

    assert token is not None, "Свободный ключ должен захватываться"
    assert await redis_limiter.acquire_once("smoke-key", 30.0) is None, "Занятый ключ не должен выдаваться"

    await redis_limiter.release("smoke-key", "чужой-токен")
    assert await redis_limiter.ttl("smoke-key") > 0, "Чужой токен не должен снимать блокировку"

    await redis_limiter.release("smoke-key", token)
    assert await redis_limiter.ttl("smoke-key") == 0, "Свой токен обязан снимать блокировку"


# --------------------------------------------------------------------------- #
# Время
# --------------------------------------------------------------------------- #


async def test_frozen_time_stops_clock_and_allows_manual_tick(frozen_time) -> None:
    from datetime import datetime, timezone

    assert datetime.now(tz=timezone.utc) == FROZEN_NOW, "Часы должны стоять на FROZEN_NOW"

    frozen_time.tick(timedelta(hours=25))

    assert datetime.now(tz=timezone.utc) == FROZEN_NOW + timedelta(hours=25), "Сдвиг времени не применился"


# --------------------------------------------------------------------------- #
# База данных
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_make_user_persists_entity_and_assigns_id(db_session, make_user) -> None:
    created = await make_user(username="smoke")

    assert created.id is not None, "Идентификатор должен проставиться после записи"

    found = await db_session.get(User, created.id)
    assert found is not None and found.username == "smoke", "Пользователь не найден в базе"


@pytest.mark.db
async def test_tables_are_empty_at_the_start_of_each_test(db_session) -> None:
    # Предыдущий тест создал и зафиксировал пользователя. Если очистка
    # после теста работает, его строки здесь уже нет.
    total = await db_session.scalar(select(func.count()).select_from(User))

    assert total == 0, f"Данные предыдущего теста не очищены: осталось {total} строк"


@pytest.mark.db
async def test_uow_commit_is_visible_to_another_unit_of_work(uow, uow_factory) -> None:
    result = await uow.users.get_or_create(
        telegram_id=TELEGRAM_ID,
        username="uow",
        first_name="Тест",
        last_name=None,
        language_code="ru",
    )
    await uow.commit()

    assert result.created is True, "Пользователь должен быть создан"

    async with uow_factory() as another:
        again = await another.users.get_by_telegram_id(TELEGRAM_ID)
        assert again is not None, "Зафиксированные данные должны быть видны соседней единице работы"
