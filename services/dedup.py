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
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from core.logger import get_logger
from db.models import Post
from db.repositories.post import PostRepository
from services.fingerprint import (
    TextFingerprint,
    extract_search_terms,
    hamming_distance,
    jaccard_similarity,
    levenshtein_ratio,
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

    def __init__(self, repo: PostRepository, config: DedupConfig | None = None) -> None:
        self._repo = repo
        self._config = config or DedupConfig()

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

        exact = await self._repo.get_by_content_hash(
            candidate.fingerprint.content_hash, since=since
        )
        if exact is not None:
            return DuplicateMatch(
                method=MatchMethod.EXACT,
                similarity=_EXACT_SIMILARITY,
                canonical_id=exact.id,
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
