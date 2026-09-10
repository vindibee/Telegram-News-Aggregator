"""Тесты трекинговых ссылок: подмена, редирект, учёт и отчёт."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.config import Settings
from db.models import ClickLog, TrackedLink, User
from db.repositories.tracking import ClickEvent
from services.tracker import AnalyticsService, ClickCounter, LinkConverter
from web import build_web_app
from web.redirect_app import setup_redirect_routes

BASE_URL = "https://track.example.com"


@pytest.fixture
def converter(uow) -> LinkConverter:
    """Конвертер ссылок поверх транзакции теста."""
    return LinkConverter(uow.links, BASE_URL)


@pytest.fixture
async def counter(redis_client) -> ClickCounter:
    """Буфер переходов на fakeredis."""
    return ClickCounter(redis_client, prefix="test")


# --------------------------------------------------------------------------- #
# Подмена ссылок в тексте
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_external_link_is_replaced(converter: LinkConverter, user: User) -> None:
    result = await converter.convert(
        "Читайте подробности: https://example.com/news/42", owner_id=user.id
    )

    assert result.replaced == 1
    assert "https://example.com/news/42" not in result.text
    assert f"{BASE_URL}/r/" in result.text


@pytest.mark.db
async def test_same_url_twice_gets_one_short_link(converter: LinkConverter, user: User) -> None:
    # Иначе статистика по адресу разошлась бы на две записи, а читатель
    # увидел бы разные ссылки на одно и то же.
    result = await converter.convert(
        "https://example.com/a и ещё раз https://example.com/a", owner_id=user.id
    )

    assert result.replaced == 1
    assert result.text.count(f"{BASE_URL}/r/") == 2


@pytest.mark.db
async def test_telegram_links_are_left_alone(converter: LinkConverter, user: User) -> None:
    # Ссылка на Telegram открывается внутри клиента; редирект через
    # внешний сервер только ломает переход.
    text = "Канал: https://t.me/habr_com и сайт https://example.com/x"

    result = await converter.convert(text, owner_id=user.id)

    assert "https://t.me/habr_com" in result.text
    assert result.replaced == 1


@pytest.mark.db
async def test_trailing_punctuation_is_not_part_of_the_url(
    converter: LinkConverter,
    user: User,
    uow,
) -> None:
    result = await converter.convert("Смотри https://example.com/a.", owner_id=user.id)

    assert result.text.endswith("."), "Точка предложения должна остаться в тексте"
    link = result.links[0]
    assert link.target_url == "https://example.com/a", "Точка не должна попасть в адрес"


@pytest.mark.db
async def test_unsafe_scheme_is_skipped(converter: LinkConverter, user: User) -> None:
    text = "Опасно: javascript:alert(1) и ftp://example.com/file"

    result = await converter.convert(text, owner_id=user.id)

    assert result.replaced == 0, "Подменяются только http и https"
    assert result.text == text


@pytest.mark.db
async def test_text_without_links_is_untouched(converter: LinkConverter, user: User) -> None:
    result = await converter.convert("Обычная новость без ссылок", owner_id=user.id)

    assert result.replaced == 0
    assert result.text == "Обычная новость без ссылок"


@pytest.mark.db
async def test_several_links_keep_their_positions(converter: LinkConverter, user: User) -> None:
    # Замены применяются с конца: иначе первая сдвинула бы позиции всех
    # последующих совпадений.
    result = await converter.convert(
        "раз https://a.example.com два https://b.example.com три", owner_id=user.id
    )

    assert result.replaced == 2
    assert result.text.startswith("раз ")
    assert result.text.endswith(" три")


def test_converter_requires_base_url(uow) -> None:
    with pytest.raises(ValueError):
        LinkConverter(uow.links, "")


# --------------------------------------------------------------------------- #
# Буфер переходов
# --------------------------------------------------------------------------- #


@pytest.mark.redis
async def test_recorded_click_is_drained_once(counter: ClickCounter) -> None:
    event = ClickEvent(token="abc", clicked_at=datetime.now(tz=timezone.utc), visitor_hash="h")

    assert await counter.record(event) is True
    first = await counter.drain()
    second = await counter.drain()

    assert [item.token for item in first] == ["abc"]
    assert second == [], "Повторный сброс не должен возвращать те же события"


@pytest.mark.redis
async def test_pending_reports_queue_length(counter: ClickCounter) -> None:
    now = datetime.now(tz=timezone.utc)
    for index in range(3):
        await counter.record(ClickEvent(token=f"t{index}", clicked_at=now))

    assert await counter.pending() == 3


@pytest.mark.redis
async def test_corrupted_event_is_skipped(counter: ClickCounter, redis_client) -> None:
    await redis_client.rpush("test:clicks:queue", "не json")
    await counter.record(
        ClickEvent(token="ok", clicked_at=datetime.now(tz=timezone.utc))
    )

    events = await counter.drain()

    assert [item.token for item in events] == ["ok"], "Битое событие не должно ронять сброс"


@pytest.mark.redis
async def test_target_cache_roundtrip(counter: ClickCounter) -> None:
    await counter.cache_target("abc", "https://example.com/x")

    assert await counter.cached_target("abc") == "https://example.com/x"

    await counter.forget_target("abc")
    assert await counter.cached_target("abc") is None


async def test_counter_without_redis_is_disabled() -> None:
    # Без Redis редирект-сервер просто не принимает события: переносить
    # будет нечего, но редирект продолжает работать.
    empty = ClickCounter(None)

    assert empty.enabled is False
    assert await empty.record(ClickEvent(token="x", clicked_at=datetime.now(tz=timezone.utc))) is False
    assert await empty.drain() == []
    assert await empty.pending() == 0


# --------------------------------------------------------------------------- #
# Перенос в базу
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_clicks_are_applied_with_unique_detection(uow, db_session, user: User) -> None:
    link = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
    await db_session.commit()

    now = datetime.now(tz=timezone.utc)
    written = await uow.links.apply_clicks(
        [
            ClickEvent(token=link.token, clicked_at=now, visitor_hash="visitor-1"),
            ClickEvent(token=link.token, clicked_at=now, visitor_hash="visitor-1"),
            ClickEvent(token=link.token, clicked_at=now, visitor_hash="visitor-2"),
        ]
    )
    await uow.session.refresh(link)

    assert written == 3
    assert link.clicks == 3, "Учитываются все переходы"
    assert link.unique_clicks == 2, "Уникальных посетителей двое"


@pytest.mark.db
async def test_clicks_for_unknown_token_are_dropped(uow, user: User) -> None:
    written = await uow.links.apply_clicks(
        [ClickEvent(token="нет-такого", clicked_at=datetime.now(tz=timezone.utc))]
    )

    assert written == 0


@pytest.mark.db
async def test_repeated_flush_does_not_double_count(uow, db_session, user: User) -> None:
    # Ограничение UNIQUE(link_id, visitor_hash) — та же защита, что и от
    # повторного сброса одной пачки.
    link = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
    await db_session.commit()

    event = ClickEvent(
        token=link.token, clicked_at=datetime.now(tz=timezone.utc), visitor_hash="visitor-1"
    )
    await uow.links.apply_clicks([event])
    await uow.links.apply_clicks([event])
    await uow.session.refresh(link)

    assert link.unique_clicks == 1, "Один посетитель не должен считаться дважды"


@pytest.mark.db
async def test_token_is_unique_per_link(uow, db_session, user: User) -> None:
    first = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
    second = await uow.links.create(target_url="https://example.com/b", owner_id=user.id)
    await db_session.commit()

    assert first.token != second.token


# --------------------------------------------------------------------------- #
# Редирект
# --------------------------------------------------------------------------- #


@pytest.fixture
def tracker_settings(settings: Settings) -> Settings:
    """Настройки с включённым трекингом."""
    return replace(settings, tracker=replace(settings.tracker, base_url=BASE_URL))


@pytest.fixture
async def client(
    tracker_settings: Settings,
    uow_factory,
    counter: ClickCounter,
) -> TestClient:
    """HTTP-клиент к редирект-серверу."""
    from unittest.mock import AsyncMock

    from services.i18n import TranslationManager

    app = build_web_app(
        settings=tracker_settings,
        uow_factory=uow_factory,
        notifier=AsyncMock(),
        translations=TranslationManager.from_directory(),
    )
    setup_redirect_routes(
        app, settings=tracker_settings, uow_factory=uow_factory, counter=counter
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


@pytest.mark.db
@pytest.mark.redis
async def test_redirect_sends_307_and_queues_the_click(
    client: TestClient,
    uow_factory,
    counter: ClickCounter,
    user: User,
    db_session,
) -> None:
    async with uow_factory() as uow:
        link = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
        token = link.token
        await uow.commit()

    response = await client.get(f"/r/{token}", allow_redirects=False)

    assert response.status == 307, "301 закэшировался бы и следующий переход не дошёл бы"
    assert response.headers["Location"] == "https://example.com/a"
    assert "no-store" in response.headers.get("Cache-Control", "")
    assert await counter.pending() == 1, "Переход должен попасть в очередь"


@pytest.mark.db
@pytest.mark.redis
async def test_second_request_is_served_from_cache(
    client: TestClient,
    uow_factory,
    counter: ClickCounter,
    user: User,
) -> None:
    async with uow_factory() as uow:
        link = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
        token = link.token
        await uow.commit()

    await client.get(f"/r/{token}", allow_redirects=False)

    assert await counter.cached_target(token) == "https://example.com/a", (
        "Первый переход должен прогреть кэш"
    )


@pytest.mark.db
@pytest.mark.redis
async def test_unknown_token_is_not_found(client: TestClient) -> None:
    response = await client.get("/r/несуществующий", allow_redirects=False)

    assert response.status == 404


@pytest.mark.db
@pytest.mark.redis
async def test_disabled_link_is_not_followed(
    client: TestClient,
    uow_factory,
    user: User,
) -> None:
    async with uow_factory() as uow:
        link = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
        token = link.token
        await uow.links.deactivate(link.id)
        await uow.commit()

    response = await client.get(f"/r/{token}", allow_redirects=False)

    assert response.status == 404


@pytest.mark.db
@pytest.mark.redis
async def test_expired_link_is_not_followed(
    client: TestClient,
    uow_factory,
    user: User,
) -> None:
    async with uow_factory() as uow:
        link = await uow.links.create(
            target_url="https://example.com/a",
            owner_id=user.id,
            expires_at=datetime.now(tz=timezone.utc) - timedelta(minutes=1),
        )
        token = link.token
        await uow.commit()

    response = await client.get(f"/r/{token}", allow_redirects=False)

    assert response.status == 404


@pytest.mark.db
@pytest.mark.redis
@pytest.mark.parametrize("token", ["x" * 64, "с-кириллицей", "with space", "sym#bol"])
async def test_garbage_token_never_reaches_the_database(client: TestClient, token: str) -> None:
    # Перебор не должен стоить запроса к базе.
    response = await client.get(f"/r/{token}", allow_redirects=False)

    assert response.status == 404


@pytest.mark.db
@pytest.mark.redis
async def test_urlsafe_token_with_separators_is_accepted(
    client: TestClient,
    uow_factory,
    user: User,
) -> None:
    # secrets.token_urlsafe выдаёт дефисы и подчёркивания: проверка,
    # требующая только букв и цифр, отвергала бы большинство ссылок.
    async with uow_factory() as uow:
        link = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
        link.token = "a-b_c123"
        await uow.commit()

    response = await client.get("/r/a-b_c123", allow_redirects=False)

    assert response.status == 307


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_report_sums_clicks_and_lists_top(uow, db_session, user: User) -> None:
    popular = await uow.links.create(target_url="https://example.com/hit", owner_id=user.id)
    quiet = await uow.links.create(target_url="https://example.com/quiet", owner_id=user.id)
    await db_session.commit()

    now = datetime.now(tz=timezone.utc)
    await uow.links.apply_clicks(
        [
            ClickEvent(token=popular.token, clicked_at=now, visitor_hash=f"v{i}")
            for i in range(5)
        ]
        + [ClickEvent(token=quiet.token, clicked_at=now, visitor_hash="v0")]
    )

    report = await AnalyticsService(uow.links).build_report(user.id)

    assert report.totals.links == 2
    assert report.totals.clicks == 6
    assert report.top[0].target_url.endswith("/hit"), "Первой идёт самая популярная"


@pytest.mark.db
async def test_report_without_clicks_is_empty(uow, user: User) -> None:
    report = await AnalyticsService(uow.links).build_report(user.id)

    assert report.is_empty


@pytest.mark.db
async def test_report_ignores_other_owners(uow, db_session, user: User, make_user) -> None:
    stranger = await make_user(telegram_id=user.telegram_id + 1)
    link = await uow.links.create(target_url="https://example.com/a", owner_id=stranger.id)
    await db_session.commit()
    await uow.links.apply_clicks(
        [ClickEvent(token=link.token, clicked_at=datetime.now(tz=timezone.utc), visitor_hash="v")]
    )

    report = await AnalyticsService(uow.links).build_report(user.id)

    assert report.is_empty, "Чужие ссылки не должны попадать в отчёт"


@pytest.mark.db
async def test_repeat_rate_reflects_returning_visitors(uow, db_session, user: User) -> None:
    link = await uow.links.create(target_url="https://example.com/a", owner_id=user.id)
    await db_session.commit()

    now = datetime.now(tz=timezone.utc)
    await uow.links.apply_clicks(
        [
            ClickEvent(token=link.token, clicked_at=now, visitor_hash="v1"),
            ClickEvent(token=link.token, clicked_at=now, visitor_hash="v1"),
        ]
    )

    report = await AnalyticsService(uow.links).build_report(user.id)

    assert report.repeat_rate == pytest.approx(0.5), (
        "Половина переходов — повторные визиты одного посетителя"
    )
