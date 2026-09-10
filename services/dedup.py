"""Дедупликация новостей.

Одна и та же новость приходит из нескольких каналов: где-то дословно,
где-то с переписанным заголовком, добавленным призывом подписаться или
другой ссылкой. Показывать её пользователю трижды нельзя.

Решение принимается в три ступени, от дешёвой к дорогой — так подавляющее
большинство записей отсеивается или пропускается без тяжёлых вычислений:

1. **Точное совпадение.** SHA-256 нормализованного текста, один поиск по
   индексу. Ловит дословные перепечатки, которых большинство.
2. **Отбор кандидатов** двумя индексируемыми каналами. Первый — совпадение
   хотя бы одного 16-битного среза simhash. Второй — полнотекстовый поиск
   по общей лексике. Одного первого мало: при четырёх срезах различающиеся
   биты перепечатки с дописанным абзацем попадают во все срезы сразу, и
   кандидат не находится. Полный перебор пар при этом недопустим — на
   миллионе записей это миллион сравнений на каждую новость.
3. **Подтверждение.** Расстояние Хэмминга по simhash отбирает близкие
   тексты, а решение принимает прямая мера сходства. Роли распределены
   именно так: приближённая метрика обязана обладать высокой полнотой и
   не терять дубликаты, а отсеивать лишнее — задача точной.

Чего алгоритм принципиально не ловит: рерайт, пересказанный своими
словами, и транслитерацию («Twitter» против «Твиттер»). Лексические меры
видят там разные тексты; для таких случаев нужны векторные представления.

Мера сходства выбирается по длине текста. Для обычных новостей это Jaccard
по шинглам — он устойчив к перестановке абзацев. Для коротких сообщений
шинглов слишком мало (у текста из пяти слов их три, и одно изменённое
слово роняет Jaccard почти до нуля), поэтому там применяется нормализованное
расстояние Левенштейна.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final

from core.logger import get_logger
from db.models import Post
from db.repositories.post import PostRepository
from services.dedup_index import DedupIndex, NullDedupIndex
from services.fingerprint import (
    TextFingerprint,
    build_fingerprint,
    extract_search_terms,
    hamming_distance,
    jaccard_similarity,
    levenshtein_ratio,
    normalize_text,
    word_count,
)

logger = get_logger(__name__)


class MatchMethod(StrEnum):
    """Каким способом обнаружен дубликат."""

    EXACT = "exact"
    SIMHASH = "simhash"
    LEVENSHTEIN = "levenshtein"
    BATCH = "batch"


@dataclass(frozen=True, slots=True)
class DedupConfig:
    """Параметры дедупликации."""

    #: Включена ли фильтрация.
    enabled: bool = True
    #: Максимальное расстояние Хэмминга для отбора на проверку.
    #:
    #: Это фильтр с высокой полнотой, а не критерий дубликата: решение
    #: принимает прямая мера сходства. Замеры на новостных парах дают
    #: расстояние 0..8 у настоящих перепечаток и 23..31 у разных новостей,
    #: поэтому порог поставлен в середину разрыва. Более строгое значение
    #: отсекало бы перепечатки с дописанным абзацем до того, как их успеет
    #: подтвердить Jaccard.
    hamming_threshold: int = 16
    #: Порог прямой меры сходства для подтверждения дубликата.
    similarity_threshold: float = 0.75
    #: Порог для коротких текстов: там мера строже, ошибка дороже.
    short_text_threshold: float = 0.9
    #: Тексты короче этого числа слов сравниваются по Левенштейну.
    short_text_words: int = 12
    #: Насколько глубоко в прошлое искать оригинал, часы.
    lookback_hours: int = 48
    #: Верхняя граница числа кандидатов на одну запись.
    candidate_limit: int = 200

    def __post_init__(self) -> None:
        if not 0 <= self.hamming_threshold <= 64:
            raise ValueError("hamming_threshold должен быть в диапазоне 0..64.")
        if not 0.0 < self.similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold должен быть в диапазоне (0, 1].")
        if not 0.0 < self.short_text_threshold <= 1.0:
            raise ValueError("short_text_threshold должен быть в диапазоне (0, 1].")
        if self.short_text_words < 1:
            raise ValueError("short_text_words должен быть не меньше 1.")
        if self.lookback_hours < 1:
            raise ValueError("lookback_hours должен быть не меньше 1.")
        if self.candidate_limit < 1:
            raise ValueError("candidate_limit должен быть не меньше 1.")

    @property
    def lookback(self) -> timedelta:
        """Окно поиска оригинала."""
        return timedelta(hours=self.lookback_hours)


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """Входящая запись, проверяемая на дублирование."""

    key: tuple[str, int]
    """Ключ ``(канал, message_id)`` — до вставки идентификатора ещё нет."""
    text: str
    post_time: datetime
    fingerprint: TextFingerprint


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    """Найденный оригинал для отдельно проверенного текста."""

    post_id: int
    similarity: float
    method: MatchMethod

    @property
    def similarity_percent(self) -> float:
        """Степень сходства в процентах, округлённая до десятых."""
        return round(self.similarity * 100, 1)


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    """Найденный оригинал."""

    method: MatchMethod
    similarity: float
    canonical_id: int | None = None
    canonical_key: tuple[str, int] | None = None

    @property
    def is_in_batch(self) -> bool:
        """Найден ли оригинал среди записей той же пачки."""
        return self.canonical_id is None and self.canonical_key is not None


#: Порог сходства, ниже которого совпадение хэша считать нельзя.
_EXACT_SIMILARITY: Final[float] = 1.0


class DeduplicationService:
    """Определяет, повторяет ли новость уже известную."""

    def __init__(
        self,
        repo: PostRepository,
        config: DedupConfig | None = None,
        index: DedupIndex | None = None,
    ) -> None:
        self._repo = repo
        self._config = config or DedupConfig()
        # Индекс — ускоритель, а не источник правды: его отсутствие
        # возвращает сервис к работе через одну лишь базу.
        self._index = index or NullDedupIndex()

    # ---------------------------------------------------------- одиночная проверка
    async def is_duplicate(self, new_text: str, threshold: float | None = None) -> bool:
        """Отвечает, повторяет ли текст уже сохранённую новость.

        Удобен там, где нужен только ответ «да или нет» — например, перед
        ручной публикацией. Для пачки записей парсера используйте
        :meth:`classify`: он дополнительно сравнивает записи между собой.

        :param new_text: Проверяемый текст.
        :param threshold: Порог сходства; по умолчанию берётся из настроек.
        :return: ``True``, если найден оригинал.
        """
        return await self.find_duplicate(new_text, threshold) is not None

    async def find_duplicate(
        self,
        new_text: str,
        threshold: float | None = None,
    ) -> DuplicateReport | None:
        """Ищет оригинал для отдельного текста.

        :param new_text: Проверяемый текст.
        :param threshold: Порог сходства; по умолчанию берётся из настроек.
        :return: Отчёт с идентификатором оригинала и степенью сходства
            либо ``None``, если совпадений нет.
        """
        if not self._config.enabled:
            return None

        fingerprint = build_fingerprint(new_text)
        if fingerprint.is_empty:
            return None

        limit = threshold if threshold is not None else self._config.similarity_threshold
        since = datetime.now(tz=timezone.utc) - self._config.lookback
        normalized = normalize_text(new_text)

        exact_id = await self._find_exact(fingerprint.content_hash, since)
        if exact_id is not None:
            return DuplicateReport(
                post_id=exact_id, similarity=_EXACT_SIMILARITY, method=MatchMethod.EXACT
            )

        best_id: int | None = None
        best_similarity = 0.0
        for post_id, simhash, text in await self._window(fingerprint, normalized, since):
            if hamming_distance(fingerprint.simhash, simhash) > self._config.hamming_threshold:
                continue
            similarity, _ = self._compare(normalized, text)
            if similarity >= limit and similarity > best_similarity:
                best_id, best_similarity = post_id, similarity

        if best_id is None:
            return None

        logger.info(
            "Текст признан дубликатом записи id=%s (сходство %.1f%%)",
            best_id, best_similarity * 100,
        )
        return DuplicateReport(
            post_id=best_id, similarity=best_similarity, method=MatchMethod.SIMHASH
        )

    async def remember(self, post_id: int, text: str) -> None:
        """Кладёт сохранённую запись в окно быстрого поиска.

        Вызывается после записи в базу: до появления идентификатора
        запоминать нечего.

        :param post_id: Идентификатор сохранённой записи.
        :param text: Исходный текст новости.
        """
        if not self._config.enabled:
            return

        fingerprint = build_fingerprint(text)
        await self._index.remember(post_id, fingerprint, normalize_text(text))

    async def _find_exact(self, content_hash: str, since: datetime) -> int | None:
        """Ищет точное совпадение сначала в индексе, затем в базе."""
        cached = await self._index.find_exact(content_hash)
        if cached is not None:
            return cached

        stored = await self._repo.get_by_content_hash(content_hash, since=since)
        return stored.id if stored is not None else None

    async def _window(
        self,
        fingerprint: TextFingerprint,
        normalized: str,
        since: datetime,
    ) -> list[tuple[int, int, str]]:
        """Собирает записи окна для сравнения.

        Индекс отдаёт кандидатов дешевле, но полагаться только на него
        нельзя: он мог не прогреться после перезапуска. Поэтому база
        опрашивается всегда по срезам, а дорогой полнотекстовый поиск —
        только когда кандидатов всё ещё мало.
        """
        window: dict[int, tuple[int, int, str]] = {
            item.post_id: (item.post_id, item.simhash, item.text)
            for item in await self._index.find_candidates(
                fingerprint.bands, self._config.candidate_limit
            )
        }

        by_bands = await self._repo.find_similar_candidates(
            fingerprint.bands, since=since, limit=self._config.candidate_limit
        )
        for post in by_bands:
            if post.simhash is not None:
                window[post.id] = (post.id, post.simhash, normalize_text(post.content))

        if not window:
            terms = extract_search_terms(normalized)
            by_text = await self._repo.find_candidates_by_text(
                terms, since=since, limit=self._config.candidate_limit
            )
            for post in by_text:
                if post.simhash is not None:
                    window[post.id] = (post.id, post.simhash, normalize_text(post.content))

        return list(window.values())

    async def classify(
        self,
        candidates: Sequence[DedupCandidate],
    ) -> dict[tuple[str, int], DuplicateMatch]:
        """Находит оригиналы для входящей пачки записей.

        Пачка обрабатывается целиком, а не по одной записи: одна и та же
        новость нередко приходит из двух каналов в одном проходе парсера, и
        такие пары обнаруживаются только при сравнении внутри пачки.

        Порядок обработки — по времени публикации: канонической становится
        самая ранняя запись, что соответствует смыслу «оригинал и его
        перепечатки».

        :param candidates: Проверяемые записи.
        :return: Отображение «ключ записи → найденный оригинал» только для
            тех, что признаны дубликатами.
        """
        if not self._config.enabled or not candidates:
            return {}

        ordered = sorted(candidates, key=lambda item: (item.post_time, item.key))
        matches: dict[tuple[str, int], DuplicateMatch] = {}
        # Записи текущей пачки, уже признанные каноническими: только они
        # могут выступать оригиналом для последующих.
        batch_canonicals: list[DedupCandidate] = []

        for candidate in ordered:
            if candidate.fingerprint.is_empty:
                # Пост без текста (только медиа) дедуплицировать нечем.
                batch_canonicals.append(candidate)
                continue

            match = self._match_in_batch(candidate, batch_canonicals)
            if match is None:
                match = await self._match_in_storage(candidate)

            if match is None:
                batch_canonicals.append(candidate)
                continue

            matches[candidate.key] = match
            logger.info(
                "Запись %s признана дубликатом (%s, сходство %.2f)",
                candidate.key, match.method.value, match.similarity,
            )

        if matches:
            logger.info(
                "Дедупликация: из %d записей отсеяно %d", len(candidates), len(matches)
            )
        return matches

    # ------------------------------------------------------------- внутри пачки
    def _match_in_batch(
        self,
        candidate: DedupCandidate,
        canonicals: Sequence[DedupCandidate],
    ) -> DuplicateMatch | None:
        """Ищет оригинал среди записей той же пачки."""
        for other in canonicals:
            if other.fingerprint.content_hash == candidate.fingerprint.content_hash:
                return DuplicateMatch(
                    method=MatchMethod.BATCH,
                    similarity=_EXACT_SIMILARITY,
                    canonical_key=other.key,
                )

            distance = hamming_distance(
                candidate.fingerprint.simhash, other.fingerprint.simhash
            )
            if distance > self._config.hamming_threshold:
                continue

            similarity, threshold = self._compare(candidate.text, other.text)
            if similarity >= threshold:
                return DuplicateMatch(
                    method=MatchMethod.BATCH,
                    similarity=similarity,
                    canonical_key=other.key,
                )
        return None

    # ------------------------------------------------------------- в хранилище
    async def _match_in_storage(self, candidate: DedupCandidate) -> DuplicateMatch | None:
        """Ищет оригинал среди уже сохранённых записей."""
        since = candidate.post_time - self._config.lookback

        # Точные перепечатки составляют большинство дубликатов, поэтому
        # самый частый ответ стоит одного обращения к Redis вместо запроса
        # к базе.
        exact_id = await self._index.find_exact(candidate.fingerprint.content_hash)
        if exact_id is None:
            exact = await self._repo.get_by_content_hash(
                candidate.fingerprint.content_hash, since=since
            )
            exact_id = exact.id if exact is not None else None

        if exact_id is not None:
            return DuplicateMatch(
                method=MatchMethod.EXACT,
                similarity=_EXACT_SIMILARITY,
                canonical_id=exact_id,
            )

        candidates = await self._collect_candidates(candidate, since)
        if not candidates:
            return None

        best = self._select_best(candidate, candidates)
        if best is None:
            logger.debug(
                "Для записи %s проверено %d кандидатов, дубликатов нет",
                candidate.key, len(candidates),
            )
        return best

    async def _collect_candidates(
        self,
        candidate: DedupCandidate,
        since: datetime,
    ) -> list[Post]:
        """Собирает кандидатов обоими каналами отбора, убирая повторы."""
        by_bands = await self._repo.find_similar_candidates(
            candidate.fingerprint.bands,
            since=since,
            limit=self._config.candidate_limit,
        )

        terms = extract_search_terms(candidate.text)
        by_text = await self._repo.find_candidates_by_text(
            terms, since=since, limit=self._config.candidate_limit
        )

        unique: dict[int, Post] = {post.id: post for post in by_bands}
        unique.update({post.id: post for post in by_text})
        logger.debug(
            "Кандидатов для %s: %d по срезам, %d по тексту, всего уникальных %d",
            candidate.key, len(by_bands), len(by_text), len(unique),
        )
        return list(unique.values())

    def _select_best(
        self,
        candidate: DedupCandidate,
        stored: Sequence[Post],
    ) -> DuplicateMatch | None:
        """Выбирает наиболее похожую запись из кандидатов.

        Проверяются все кандидаты, а не первый подходящий: при нескольких
        близких записях оригиналом должна стать самая похожая, иначе
        кластер распадается на несколько.
        """
        best_match: DuplicateMatch | None = None

        for post in stored:
            if post.simhash is None or post.id == 0:
                continue

            distance = hamming_distance(candidate.fingerprint.simhash, post.simhash)
            if distance > self._config.hamming_threshold:
                continue

            similarity, threshold = self._compare(candidate.text, post.content or "")
            if similarity < threshold:
                # Simhash сблизил тексты ошибочно — прямая мера это отсекла.
                logger.debug(
                    "Кандидат id=%s отклонён: расстояние %d, сходство %.2f < %.2f",
                    post.id, distance, similarity, threshold,
                )
                continue

            method = (
                MatchMethod.LEVENSHTEIN
                if self._is_short(candidate.text)
                else MatchMethod.SIMHASH
            )
            if best_match is None or similarity > best_match.similarity:
                best_match = DuplicateMatch(
                    method=method, similarity=similarity, canonical_id=post.id
                )

        return best_match

    # ------------------------------------------------------------------ метрики
    def _compare(self, left: str, right: str) -> tuple[float, float]:
        """Считает сходство текстов и порог, с которым его сравнивать.

        :return: Пара «значение меры, требуемый порог».
        """
        if self._is_short(left) or self._is_short(right):
            return levenshtein_ratio(left, right), self._config.short_text_threshold
        return jaccard_similarity(left, right), self._config.similarity_threshold

    def _is_short(self, text: str) -> bool:
        """Считается ли текст коротким для шингловой оценки."""
        return word_count(text) < self._config.short_text_words
