"""Поиск по архиву новостей.

Слой между репозиторием и интерфейсом: проверяет запрос, режет выдачу на
страницы и превращает разметку подсветки в безопасный HTML.

**Общего числа найденного здесь нет намеренно.** Чтобы показать «страница
2 из 47», нужен ``COUNT(*)`` по всем совпадениям, а он у полнотекстового
поиска стоит примерно столько же, сколько сама выдача: индекс даёт
кандидатов, но посчитать их можно только пройдя по ним. Ради подписи,
которую никто не читает, удваивать стоимость каждого запроса не стоит.
Вместо этого запрашивается на одну запись больше, чем нужно странице:
если она пришла — есть следующая страница, и кнопка «Вперёд» показывается.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Final

from core.logger import get_logger
from db.repositories.post import HIGHLIGHT_START, HIGHLIGHT_STOP, PostRepository, SearchHit

logger = get_logger(__name__)

#: Сколько результатов показывать на странице.
DEFAULT_PAGE_SIZE: Final[int] = 5

#: Границы длины запроса. Слишком короткий даёт бессмысленную выдачу, а
#: слишком длинный — только нагрузку: значимых слов в нём всё равно
#: единицы.
MIN_QUERY_LENGTH: Final[int] = 2
MAX_QUERY_LENGTH: Final[int] = 128

#: Верхняя граница номера страницы. Глубокая пагинация полнотекстового
#: поиска бессмысленна: за сотой страницей релевантности уже нет, а
#: ``OFFSET`` продолжает расти линейно.
MAX_PAGE: Final[int] = 20


class SearchQueryError(ValueError):
    """Запрос не годится для поиска."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Одна найденная запись, готовая к показу."""

    post_id: int
    channel_name: str
    source_url: str
    snippet: str
    post_time_iso: str


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Страница результатов поиска."""

    query: str
    page: int
    results: tuple[SearchResult, ...]
    has_next: bool

    @property
    def has_prev(self) -> bool:
        """Есть ли предыдущая страница."""
        return self.page > 0

    @property
    def is_empty(self) -> bool:
        """Пустая ли выдача."""
        return not self.results


class SearchService:
    """Поиск по сохранённым новостям."""

    def __init__(self, repo: PostRepository, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        if page_size < 1:
            raise ValueError(f"Размер страницы должен быть положительным, получено: {page_size}")
        self._repo = repo
        self._page_size = page_size

    @property
    def page_size(self) -> int:
        """Сколько результатов на странице."""
        return self._page_size

    async def search(
        self,
        query_text: str,
        *,
        page: int = 0,
        channel: str | None = None,
    ) -> SearchPage:
        """Ищет записи и возвращает запрошенную страницу.

        :param query_text: Поисковый запрос пользователя.
        :param page: Номер страницы, считая с нуля.
        :param channel: Ограничение по каналу.
        :return: Страница результатов.
        :raises SearchQueryError: Запрос слишком короткий или длинный.
        """
        query = self.normalize_query(query_text)
        page = max(0, min(page, MAX_PAGE))

        # Запрашиваем на одну запись больше страницы: её наличие и есть
        # ответ на вопрос «показывать ли кнопку Вперёд».
        hits = await self._repo.search(
            query,
            limit=self._page_size + 1,
            offset=page * self._page_size,
            channel=channel,
        )

        has_next = len(hits) > self._page_size
        visible = hits[: self._page_size]

        logger.info(
            "Поиск %r, страница %d: показано %d, есть ещё: %s",
            query[:64], page, len(visible), has_next,
        )
        return SearchPage(
            query=query,
            page=page,
            results=tuple(self._to_result(hit) for hit in visible),
            has_next=has_next,
        )

    @staticmethod
    def normalize_query(query_text: str) -> str:
        """Проверяет и приводит запрос к виду, пригодному для поиска.

        :param query_text: Ввод пользователя.
        :return: Очищенный запрос.
        :raises SearchQueryError: Запрос слишком короткий или длинный.
        """
        query = " ".join(query_text.split())
        if len(query) < MIN_QUERY_LENGTH:
            raise SearchQueryError("search.errors.too_short")
        if len(query) > MAX_QUERY_LENGTH:
            raise SearchQueryError("search.errors.too_long")
        return query

    @staticmethod
    def _to_result(hit: SearchHit) -> SearchResult:
        """Превращает найденную запись в готовую к показу."""
        return SearchResult(
            post_id=hit.post_id,
            channel_name=hit.channel_name,
            source_url=hit.source_url,
            snippet=render_snippet(hit.snippet),
            post_time_iso=hit.post_time.isoformat(),
        )


def render_snippet(raw: str) -> str:
    """Превращает разметку подсветки в безопасный HTML.

    Порядок действий здесь единственно возможный: сначала экранируется
    весь фрагмент, и только потом метки подсветки заменяются на теги.
    Обратный порядок означал бы, что вставленные нами ``<b>`` тоже будут
    экранированы и пользователь увидит их как текст.

    Метки — управляющие символы, которых не бывает в тексте новости,
    поэтому подменить их пользовательским вводом невозможно.

    :param raw: Фрагмент из ``ts_headline`` с метками.
    :return: HTML, пригодный для отправки в Telegram.
    """
    safe = escape(raw)
    return safe.replace(HIGHLIGHT_START, "<b>").replace(HIGHLIGHT_STOP, "</b>")
