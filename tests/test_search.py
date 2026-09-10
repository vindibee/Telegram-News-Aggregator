"""Тесты полнотекстового поиска: запросы, ранжирование, подсветка, страницы."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db.enums import PostStatus
from db.repositories.post import HIGHLIGHT_START, HIGHLIGHT_STOP
from services.search import (
    MAX_QUERY_LENGTH,
    SearchQueryError,
    SearchService,
    render_snippet,
)

pytestmark = pytest.mark.db

RUSSIAN = "Компания представила нейросеть, которая обучается быстрее предыдущих моделей."
ENGLISH = "New release of the framework improves performance and fixes crashes."
SPORT = "Сборная выиграла эстафету на этапе кубка мира по биатлону."
UNSAFE = 'Разбор кода <script>alert("xss")</script> и сравнение a < b & c > d в нейросети.'


async def _seed(make_post, db_session, texts: list[str], **overrides) -> list[int]:
    """Кладёт записи в архив со свежими датами."""
    now = datetime.now(tz=timezone.utc)
    ids: list[int] = []
    for index, text in enumerate(texts):
        post = await make_post(
            content=text,
            post_time=now - timedelta(minutes=index),
            **overrides,
        )
        ids.append(post.id)
    await db_session.commit()
    return ids


# --------------------------------------------------------------------------- #
# Морфология и синтаксис запроса
# --------------------------------------------------------------------------- #


async def test_russian_morphology_matches_other_word_forms(uow, db_session, make_post) -> None:
    # Конфигурация russian стеммит: «нейросети» и «нейросеть» — одна лемма.
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts).search("нейросети")

    assert len(page.results) == 1, "Другая словоформа должна находиться"


async def test_english_morphology_works_in_the_same_configuration(
    uow, db_session, make_post
) -> None:
    # Конфигурация russian обрабатывает латиницу английским стеммером,
    # поэтому отдельная колонка под английский не нужна.
    await _seed(make_post, db_session, [ENGLISH])

    page = await SearchService(uow.posts).search("releasing")

    assert len(page.results) == 1, "«releasing» должно находить «release»"


async def test_exact_phrase_narrows_the_search(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [SPORT, RUSSIAN])
    service = SearchService(uow.posts)

    found = await service.search('"кубка мира"')
    missing = await service.search('"мира кубка"')

    assert len(found.results) == 1
    assert missing.is_empty, "Порядок слов в точной фразе должен учитываться"


async def test_minus_excludes_documents(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN, SPORT])
    service = SearchService(uow.posts)

    page = await service.search("выиграла -биатлону")

    assert page.is_empty, "Исключающее слово должно убирать документ из выдачи"


async def test_stop_words_only_query_returns_nothing(uow, db_session, make_post) -> None:
    # websearch_to_tsquery на таком вводе даёт пустой запрос — это не
    # ошибка, а отсутствие значимых слов.
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts).search("и в на")

    assert page.is_empty


async def test_sql_injection_is_just_a_query(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts).search("'; DROP TABLE posts;--")

    assert page.is_empty
    assert await uow.posts.count() == 1, "Таблица должна остаться на месте"


# --------------------------------------------------------------------------- #
# Выдача
# --------------------------------------------------------------------------- #


async def test_duplicates_are_excluded_from_results(uow, db_session, make_post) -> None:
    # Дубликаты скрыты из ленты, и в поиске им делать нечего — иначе одна
    # новость занимала бы половину страницы.
    await _seed(make_post, db_session, [RUSSIAN])
    await _seed(make_post, db_session, [RUSSIAN], status=PostStatus.DUPLICATE)

    page = await SearchService(uow.posts).search("нейросеть")

    assert len(page.results) == 1


async def test_snippet_highlights_the_match(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts).search("нейросеть")

    assert "<b>" in page.results[0].snippet, "Совпадение должно подсвечиваться"
    assert "</b>" in page.results[0].snippet


async def test_snippet_escapes_html_from_the_post(uow, db_session, make_post) -> None:
    # Текст новости содержит угловые скобки и амперсанды: без
    # экранирования Telegram отверг бы сообщение целиком.
    await _seed(make_post, db_session, [UNSAFE])

    page = await SearchService(uow.posts).search("нейросети")
    snippet = page.results[0].snippet

    assert "<script>" not in snippet, "Разметка из текста обязана экранироваться"
    assert "&lt;script&gt;" in snippet or "&amp;" in snippet
    assert HIGHLIGHT_START not in snippet and HIGHLIGHT_STOP not in snippet


async def test_result_carries_link_to_the_source(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts).search("нейросеть")

    assert page.results[0].source_url.startswith("https://t.me/")


async def test_channel_filter_limits_the_scope(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN], channel_name="habr_com")
    await _seed(make_post, db_session, [RUSSIAN], channel_name="rbc_news")
    service = SearchService(uow.posts)

    everywhere = await service.search("нейросеть")
    narrowed = await service.search("нейросеть", channel="rbc_news")

    assert len(everywhere.results) == 2
    assert len(narrowed.results) == 1
    assert narrowed.results[0].channel_name == "rbc_news"


# --------------------------------------------------------------------------- #
# Постраничная выдача
# --------------------------------------------------------------------------- #


async def test_pages_do_not_overlap_and_report_next(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [f"{RUSSIAN} вариант {i}" for i in range(7)])
    service = SearchService(uow.posts, page_size=3)

    first = await service.search("нейросеть", page=0)
    second = await service.search("нейросеть", page=1)

    assert len(first.results) == 3
    assert first.has_next is True
    assert first.has_prev is False
    assert second.has_prev is True
    assert {item.post_id for item in first.results} & {
        item.post_id for item in second.results
    } == set(), "Страницы не должны пересекаться"


async def test_last_page_reports_no_next(uow, db_session, make_post) -> None:
    # Запрашивается на одну запись больше страницы: её отсутствие и есть
    # признак последней страницы, без отдельного COUNT.
    await _seed(make_post, db_session, [f"{RUSSIAN} вариант {i}" for i in range(4)])
    service = SearchService(uow.posts, page_size=3)

    last = await service.search("нейросеть", page=1)

    assert len(last.results) == 1
    assert last.has_next is False


async def test_page_beyond_results_is_empty_not_an_error(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts, page_size=3).search("нейросеть", page=5)

    assert page.is_empty
    assert page.has_next is False


async def test_negative_page_is_clamped(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts).search("нейросеть", page=-10)

    assert page.page == 0


# --------------------------------------------------------------------------- #
# Проверка запроса
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["", " ", "a"])
async def test_too_short_query_is_rejected(uow, raw: str) -> None:
    with pytest.raises(SearchQueryError) as info:
        await SearchService(uow.posts).search(raw)

    assert info.value.key == "search.errors.too_short"


async def test_too_long_query_is_rejected(uow) -> None:
    with pytest.raises(SearchQueryError) as info:
        await SearchService(uow.posts).search("я" * (MAX_QUERY_LENGTH + 1))

    assert info.value.key == "search.errors.too_long"


async def test_query_whitespace_is_collapsed(uow, db_session, make_post) -> None:
    await _seed(make_post, db_session, [RUSSIAN])

    page = await SearchService(uow.posts).search("  нейросеть   модель  ")

    assert page.query == "нейросеть модель", "Лишние пробелы должны схлопываться"


def test_page_size_must_be_positive(uow) -> None:
    with pytest.raises(ValueError):
        SearchService(uow.posts, page_size=0)


# --------------------------------------------------------------------------- #
# Отрисовка фрагмента
# --------------------------------------------------------------------------- #


def test_render_escapes_before_inserting_tags() -> None:
    # Обратный порядок означал бы, что наши же теги будут экранированы и
    # пользователь увидит их как текст.
    raw = f"a < b {HIGHLIGHT_START}нейросеть{HIGHLIGHT_STOP} & c"

    assert render_snippet(raw) == "a &lt; b <b>нейросеть</b> &amp; c"


def test_render_leaves_plain_text_untouched() -> None:
    assert render_snippet("обычный текст") == "обычный текст"


def test_markers_from_user_text_cannot_forge_highlighting() -> None:
    # Метки — управляющие символы, которых не бывает в тексте новости.
    # Даже если бы они там оказались, подмена дала бы только теги <b>.
    forged = render_snippet("<b>подделка</b>")

    assert forged == "&lt;b&gt;подделка&lt;/b&gt;"
