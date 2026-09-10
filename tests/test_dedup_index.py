"""Тесты окна дедупликации в Redis и одиночной проверки текста."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from db.models import Post
from services.dedup import DedupConfig, DeduplicationService, MatchMethod
from services.dedup_index import (
    MAX_CACHED_TEXT,
    NullDedupIndex,
    RedisDedupIndex,
    build_dedup_index,
)
from services.fingerprint import build_fingerprint, normalize_text

ORIGINAL = (
    "Компания представила новую модель искусственного интеллекта, "
    "которая обгоняет предыдущую версию по всем тестам."
)
REPRINT = (
    "Компания представила новую модель искусственного интеллекта, "
    "которая обгоняет предыдущую версию по всем тестам. Подписывайтесь на канал!"
)
UNRELATED = (
    "Сборная страны по биатлону выиграла эстафету на этапе кубка мира в Австрии."
)
#: Длинный текст: одна изменённая формулировка в нём почти не сдвигает
#: simhash, и срезы продолжают совпадать. На короткой новости то же
#: изменение ломает все четыре среза — этим и объясняется второй канал
#: отбора, полнотекстовый.
LONG_ORIGINAL = ORIGINAL * 6


@pytest.fixture
async def index(redis_client) -> RedisDedupIndex:
    """Индекс поверх fakeredis."""
    return RedisDedupIndex(redis_client, prefix="test", ttl=48 * 3600)


# --------------------------------------------------------------------------- #
# Индекс
# --------------------------------------------------------------------------- #


@pytest.mark.redis
async def test_remembered_text_is_found_by_exact_hash(index: RedisDedupIndex) -> None:
    fingerprint = build_fingerprint(ORIGINAL)
    await index.remember(42, fingerprint, normalize_text(ORIGINAL))

    assert await index.find_exact(fingerprint.content_hash) == 42


@pytest.mark.redis
async def test_unknown_hash_is_a_miss(index: RedisDedupIndex) -> None:
    assert await index.find_exact("0" * 64) is None


@pytest.mark.redis
async def test_near_identical_text_is_found_among_candidates(index: RedisDedupIndex) -> None:
    # Срезы simhash — фильтр для почти идентичных текстов: правка в одно
    # слово их не меняет. Перепечатки с дописанным абзацем ловит уже
    # полнотекстовый канал отбора, а не этот.
    original = build_fingerprint(LONG_ORIGINAL)
    await index.remember(7, original, normalize_text(LONG_ORIGINAL))

    edited = build_fingerprint(LONG_ORIGINAL.replace("обгоняет", "опережает", 1))
    found = await index.find_candidates(edited.bands, limit=50)

    assert [item.post_id for item in found] == [7], "Почти идентичный текст должен находиться"
    assert found[0].simhash == original.simhash


@pytest.mark.redis
async def test_unrelated_text_shares_no_bands(index: RedisDedupIndex) -> None:
    await index.remember(7, build_fingerprint(ORIGINAL), normalize_text(ORIGINAL))

    found = await index.find_candidates(build_fingerprint(UNRELATED).bands, limit=50)

    assert found == [], "Разные новости не должны попадать в одни срезы"


@pytest.mark.redis
async def test_empty_text_is_not_indexed(index: RedisDedupIndex) -> None:
    fingerprint = build_fingerprint("   ")

    await index.remember(1, fingerprint, "")

    assert await index.find_exact(fingerprint.content_hash) is None


@pytest.mark.redis
async def test_long_text_is_truncated_before_storing(index: RedisDedupIndex) -> None:
    long_text = "новость " * 2000
    fingerprint = build_fingerprint(long_text)

    await index.remember(9, fingerprint, normalize_text(long_text))
    found = await index.find_candidates(fingerprint.bands, limit=10)

    assert found, "Запись должна попасть в индекс"
    assert len(found[0].text) <= MAX_CACHED_TEXT, "Длинный текст обязан обрезаться"


@pytest.mark.redis
async def test_keys_expire_within_the_window(index: RedisDedupIndex, redis_client) -> None:
    fingerprint = build_fingerprint(ORIGINAL)
    await index.remember(5, fingerprint, normalize_text(ORIGINAL))

    ttl = await redis_client.ttl(f"test:dd:h:{fingerprint.content_hash}")

    assert 0 < ttl <= 48 * 3600, f"Срок жизни ключа вне окна дедупликации: {ttl}"


# --------------------------------------------------------------------------- #
# Деградация при недоступном Redis
# --------------------------------------------------------------------------- #


async def test_broken_redis_reads_are_treated_as_a_miss() -> None:
    # Потеря кэша не должна означать «дубликатов нет»: вызывающий код
    # просто пойдёт в базу.
    client = AsyncMock()
    client.get.side_effect = RedisConnectionError("нет связи")
    broken = RedisDedupIndex(client, prefix="test")

    assert await broken.find_exact("abc") is None


async def test_broken_redis_writes_do_not_raise() -> None:
    from unittest.mock import MagicMock

    client = AsyncMock()
    # pipeline() у настоящего клиента синхронный, поэтому и мок синхронный:
    # иначе тест проверял бы поведение, которого в бою не бывает.
    client.pipeline = MagicMock(side_effect=RedisConnectionError("нет связи"))
    broken = RedisDedupIndex(client, prefix="test")

    await broken.remember(1, build_fingerprint(ORIGINAL), ORIGINAL)


async def test_null_index_is_always_a_miss() -> None:
    empty = NullDedupIndex()

    assert await empty.find_exact("abc") is None
    assert await empty.find_candidates([1, 2, 3, 4], limit=10) == []
    await empty.remember(1, build_fingerprint(ORIGINAL), ORIGINAL)


def test_index_requires_positive_ttl() -> None:
    with pytest.raises(ValueError):
        RedisDedupIndex(AsyncMock(), ttl=0)


def test_builder_returns_null_index_without_redis(settings) -> None:
    from dataclasses import replace

    index = build_dedup_index(replace(settings.redis, url=""), 48)

    assert isinstance(index, NullDedupIndex)


# --------------------------------------------------------------------------- #
# Одиночная проверка текста
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_is_duplicate_detects_verbatim_reprint(uow, db_session, make_post) -> None:
    await make_post(content=ORIGINAL, **_fingerprint_columns(ORIGINAL))
    await db_session.commit()

    service = DeduplicationService(uow.posts, DedupConfig())

    assert await service.is_duplicate(ORIGINAL) is True


@pytest.mark.db
async def test_is_duplicate_ignores_unrelated_news(uow, db_session, make_post) -> None:
    await make_post(content=ORIGINAL, **_fingerprint_columns(ORIGINAL))
    await db_session.commit()

    service = DeduplicationService(uow.posts, DedupConfig())

    assert await service.is_duplicate(UNRELATED) is False


@pytest.mark.db
async def test_find_duplicate_reports_id_and_percentage(uow, db_session, make_post) -> None:
    stored = await make_post(content=ORIGINAL, **_fingerprint_columns(ORIGINAL))
    await db_session.commit()

    service = DeduplicationService(uow.posts, DedupConfig())
    report = await service.find_duplicate(REPRINT)

    assert report is not None, "Перепечатка с дописанным хвостом должна находиться"
    assert report.post_id == stored.id
    assert 0 < report.similarity_percent <= 100
    assert report.method in {MatchMethod.EXACT, MatchMethod.SIMHASH}


@pytest.mark.db
async def test_exact_match_reports_hundred_percent(uow, db_session, make_post) -> None:
    stored = await make_post(content=ORIGINAL, **_fingerprint_columns(ORIGINAL))
    await db_session.commit()

    report = await DeduplicationService(uow.posts, DedupConfig()).find_duplicate(ORIGINAL)

    assert report is not None
    assert report.post_id == stored.id
    assert report.similarity_percent == 100.0
    assert report.method is MatchMethod.EXACT


@pytest.mark.db
async def test_higher_threshold_rejects_loose_similarity(uow, db_session, make_post) -> None:
    await make_post(content=ORIGINAL, **_fingerprint_columns(ORIGINAL))
    await db_session.commit()

    service = DeduplicationService(uow.posts, DedupConfig())

    assert await service.find_duplicate(REPRINT, threshold=0.999) is None, (
        "Порог должен управлять строгостью сравнения"
    )


@pytest.mark.db
async def test_empty_text_is_never_a_duplicate(uow) -> None:
    service = DeduplicationService(uow.posts, DedupConfig())

    assert await service.is_duplicate("   ") is False


@pytest.mark.db
async def test_disabled_service_reports_no_duplicates(uow, db_session, make_post) -> None:
    await make_post(content=ORIGINAL, **_fingerprint_columns(ORIGINAL))
    await db_session.commit()

    from dataclasses import replace

    service = DeduplicationService(uow.posts, replace(DedupConfig(), enabled=False))

    assert await service.is_duplicate(ORIGINAL) is False


@pytest.mark.redis
@pytest.mark.db
async def test_index_short_circuits_the_exact_lookup(uow, index: RedisDedupIndex) -> None:
    # База не трогается вовсе: запись существует только в индексе.
    service = DeduplicationService(uow.posts, DedupConfig(), index)
    await service.remember(555, ORIGINAL)

    report = await service.find_duplicate(ORIGINAL)

    assert report is not None
    assert report.post_id == 555, "Ответ должен прийти из индекса"
    assert report.method is MatchMethod.EXACT


def _fingerprint_columns(text: str) -> dict[str, object]:
    """Считает колонки отпечатка так же, как это делает сервис новостей."""
    fingerprint = build_fingerprint(text)
    bands = fingerprint.bands
    return {
        "content_hash": fingerprint.content_hash,
        "simhash": _signed(fingerprint.simhash),
        "simhash_band_0": bands[0],
        "simhash_band_1": bands[1],
        "simhash_band_2": bands[2],
        "simhash_band_3": bands[3],
        # Окно дедупликации отсчитывается от текущего момента, поэтому
        # запись должна быть свежей по настоящим часам, а не по FROZEN_NOW.
        "post_time": datetime.now(tz=timezone.utc) - timedelta(hours=1),
    }


def _signed(value: int) -> int:
    """Приводит simhash к знаковому BIGINT."""
    from services.fingerprint import to_signed64

    return to_signed64(value)
