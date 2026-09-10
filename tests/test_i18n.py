"""Тесты локализации: каталоги, формы числа, кэш языка и middleware."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from db.enums import Language
from services.i18n import (
    InMemoryLanguageCache,
    TranslationError,
    TranslationManager,
    Translator,
    select_plural_form,
)
from tg_bot.middlewares.i18n import I18N_KEY, LANGUAGE_KEY, I18nMiddleware


@pytest.fixture(scope="session")
def manager() -> TranslationManager:
    """Каталоги проекта, загруженные с диска."""
    return TranslationManager.from_directory()


def _write_catalogs(directory: Path, catalogs: dict[str, dict[str, Any]]) -> None:
    """Раскладывает временные каталоги переводов по файлам."""
    for code, payload in catalogs.items():
        (directory / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
# Каталоги проекта
# --------------------------------------------------------------------------- #


def test_all_languages_have_the_same_keys(manager: TranslationManager) -> None:
    # Расхождение не ломает бота — сработает откат, — но часть интерфейса
    # окажется на другом языке. Такое ловится здесь, а не отзывом.
    reference = set(manager._catalogs[Language.RU])

    for language in Language:
        missing = reference - set(manager._catalogs[language])
        extra = set(manager._catalogs[language]) - reference
        assert not missing, f"В каталоге {language.value} не хватает: {sorted(missing)}"
        assert not extra, f"В каталоге {language.value} лишнее: {sorted(extra)}"


def test_every_language_is_loaded(manager: TranslationManager) -> None:
    assert set(manager.languages) == set(Language), "Должны загружаться все объявленные языки"


def test_format_placeholders_match_across_languages(manager: TranslationManager) -> None:
    # Пропущенный плейсхолдер в переводе означает, что пользователь увидит
    # текст без подставленного значения — например, без даты окончания.
    import re

    pattern = re.compile(r"\{(\w+)\}")
    reference = manager._catalogs[Language.RU]

    for language in Language:
        if language is Language.RU:
            continue
        for key, template in manager._catalogs[language].items():
            if not isinstance(template, str) or not isinstance(reference.get(key), str):
                continue
            assert set(pattern.findall(template)) == set(pattern.findall(reference[key])), (
                f"Разный набор подстановок в ключе {key!r} для {language.value}"
            )


@pytest.mark.parametrize("language", list(Language))
def test_known_keys_render_without_placeholders_left(
    manager: TranslationManager,
    language: Language,
) -> None:
    text = manager.get(language, "billing.info", plan="Pro", status="active", expires="01.01", left="5")

    assert "{" not in text, f"В тексте остались неподставленные значения: {text}"


# --------------------------------------------------------------------------- #
# Формы множественного числа
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "one"), (2, "few"), (4, "few"), (5, "many"), (11, "many"), (21, "one"), (22, "few"), (25, "many")],
)
def test_slavic_plural_rules_follow_cldr(count: int, expected: str) -> None:
    assert select_plural_form(Language.RU, count) == expected
    assert select_plural_form(Language.UK, count) == expected


@pytest.mark.parametrize(("count", "expected"), [(0, "other"), (1, "one"), (2, "other"), (21, "other")])
def test_english_plural_has_two_forms(count: int, expected: str) -> None:
    assert select_plural_form(Language.EN, count) == expected


def test_plural_renders_correct_form_for_each_language(manager: TranslationManager) -> None:
    assert manager.plural(Language.RU, "units.days", 1) == "1 день"
    assert manager.plural(Language.RU, "units.days", 3) == "3 дня"
    assert manager.plural(Language.RU, "units.days", 7) == "7 дней"
    assert manager.plural(Language.UK, "units.days", 3) == "3 дні"
    assert manager.plural(Language.EN, "units.days", 3) == "3 days"


# --------------------------------------------------------------------------- #
# Поведение при неполных каталогах
# --------------------------------------------------------------------------- #


def test_missing_key_falls_back_to_default_language(tmp_path: Path) -> None:
    _write_catalogs(
        tmp_path,
        {
            "ru": {"greeting": "Привет", "only_ru": "Только по-русски"},
            "en": {"greeting": "Hello"},
            "uk": {"greeting": "Привіт"},
        },
    )
    manager = TranslationManager.from_directory(tmp_path)

    assert manager.get(Language.EN, "only_ru") == "Только по-русски", (
        "При отсутствии перевода должен использоваться язык по умолчанию"
    )


def test_unknown_key_returns_the_key_itself(tmp_path: Path) -> None:
    # Падать посреди обработки апдейта из-за опечатки в ключе нельзя:
    # ключ в интерфейсе заметен и не мешает пользователю работать дальше.
    _write_catalogs(tmp_path, {code: {"greeting": "x"} for code in ("ru", "en", "uk")})
    manager = TranslationManager.from_directory(tmp_path)

    assert manager.get(Language.RU, "нет.такого") == "нет.такого"


def test_missing_parameter_returns_template_instead_of_crashing(tmp_path: Path) -> None:
    _write_catalogs(tmp_path, {code: {"hi": "Привет, {name}"} for code in ("ru", "en", "uk")})
    manager = TranslationManager.from_directory(tmp_path)

    assert manager.get(Language.RU, "hi") == "Привет, {name}", (
        "Нехватка параметра не должна ронять обработку апдейта"
    )


def test_missing_catalog_file_is_a_startup_error(tmp_path: Path) -> None:
    _write_catalogs(tmp_path, {"ru": {"greeting": "Привет"}})

    with pytest.raises(TranslationError):
        TranslationManager.from_directory(tmp_path)


def test_broken_json_is_a_startup_error(tmp_path: Path) -> None:
    _write_catalogs(tmp_path, {code: {"greeting": "x"} for code in ("ru", "en", "uk")})
    (tmp_path / "en.json").write_text("{ это не json", encoding="utf-8")

    with pytest.raises(TranslationError):
        TranslationManager.from_directory(tmp_path)


# --------------------------------------------------------------------------- #
# Локализатор
# --------------------------------------------------------------------------- #


def test_translator_binds_language_and_switch_makes_a_new_one(manager: TranslationManager) -> None:
    russian = Translator(manager, Language.RU)
    english = russian.switch(Language.EN)

    assert russian.language is Language.RU
    assert english.language is Language.EN, "switch не должен менять исходный локализатор"
    assert russian("common.busy") != english("common.busy")


# --------------------------------------------------------------------------- #
# Кэш языка
# --------------------------------------------------------------------------- #


async def test_memory_cache_stores_and_invalidates_language() -> None:
    cache = InMemoryLanguageCache()

    assert await cache.get(1) is None, "Пустой кэш должен давать промах"

    await cache.set(1, Language.UK)
    assert await cache.get(1) is Language.UK

    await cache.invalidate(1)
    assert await cache.get(1) is None


async def test_memory_cache_forgets_expired_entries() -> None:
    cache = InMemoryLanguageCache(ttl=0.01)
    await cache.set(1, Language.EN)

    import asyncio

    await asyncio.sleep(0.05)

    assert await cache.get(1) is None, "Протухшая запись должна считаться промахом"


def test_memory_cache_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError):
        InMemoryLanguageCache(ttl=0)


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


async def test_middleware_prefers_cache_over_everything(
    manager: TranslationManager,
    telegram_user,
    handler_stub: AsyncMock,
) -> None:
    cache = InMemoryLanguageCache()
    await cache.set(telegram_user.id, Language.UK)
    middleware = I18nMiddleware(manager, cache)
    data: dict[str, Any] = {"event_from_user": telegram_user}

    await middleware(handler_stub, object(), data)

    assert data[LANGUAGE_KEY] is Language.UK
    assert data[I18N_KEY].language is Language.UK


async def test_middleware_takes_language_from_loaded_user_and_warms_cache(
    manager: TranslationManager,
    telegram_user,
    handler_stub: AsyncMock,
) -> None:
    from tests.conftest import build_user

    cache = InMemoryLanguageCache()
    middleware = I18nMiddleware(manager, cache)
    user = build_user(telegram_id=telegram_user.id, language=Language.EN)
    data: dict[str, Any] = {"event_from_user": telegram_user, "user": user}

    await middleware(handler_stub, object(), data)

    assert data[LANGUAGE_KEY] is Language.EN
    assert await cache.get(telegram_user.id) is Language.EN, "Кэш должен прогреваться"


async def test_middleware_guesses_language_from_telegram_for_new_user(
    manager: TranslationManager,
    handler_stub: AsyncMock,
) -> None:
    from aiogram.types import User as TelegramUser

    cache = InMemoryLanguageCache()
    middleware = I18nMiddleware(manager, cache)
    newcomer = TelegramUser(id=555, is_bot=False, first_name="New", language_code="uk-UA")
    data: dict[str, Any] = {"event_from_user": newcomer}

    await middleware(handler_stub, object(), data)

    assert data[LANGUAGE_KEY] is Language.UK, "Подсказка клиента должна учитываться"


async def test_middleware_falls_back_to_default_for_service_updates(
    manager: TranslationManager,
    handler_stub: AsyncMock,
) -> None:
    middleware = I18nMiddleware(manager, InMemoryLanguageCache())
    data: dict[str, Any] = {}

    await middleware(handler_stub, object(), data)

    assert data[LANGUAGE_KEY] is manager.default_language
    handler_stub.assert_awaited_once()


@pytest.mark.db
async def test_middleware_reads_language_from_database_on_cache_miss(
    manager: TranslationManager,
    telegram_user,
    handler_stub: AsyncMock,
    uow,
    db_session,
    make_user,
) -> None:
    stored = await make_user(telegram_id=telegram_user.id, language=Language.EN)
    assert stored.id is not None

    middleware = I18nMiddleware(manager, InMemoryLanguageCache())
    data: dict[str, Any] = {"event_from_user": telegram_user, "uow": uow}

    await middleware(handler_stub, object(), data)

    assert data[LANGUAGE_KEY] is Language.EN, "Язык должен подняться из базы"
