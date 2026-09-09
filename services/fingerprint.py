"""Вычисление отпечатков текста для дедупликации новостей.

Модуль отвечает только за расчёт значений, которые затем ложатся в колонки
``content_hash``, ``simhash`` и ``simhash_band_*`` модели
:class:`db.models.post.Post`. Само принятие решения «дубликат или нет»
относится к сервису дедупликации и здесь не выполняется.

Схема работы:

1. текст нормализуется (регистр, ссылки, пунктуация, пробелы);
2. считается SHA-256 — точное совпадение ловится одним индексным поиском;
3. считается 64-битный simhash по шинглам — близкие тексты дают близкие
   значения, а расстояние Хэмминга показывает степень сходства;
4. simhash режется на четыре 16-битных среза: совпадение хотя бы одного
   среза служит дешёвым индексируемым фильтром кандидатов.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from core.logger import get_logger

logger = get_logger(__name__)

#: Разрядность simhash.
SIMHASH_BITS: Final[int] = 64

#: Количество срезов (band) и их разрядность.
BAND_COUNT: Final[int] = 4
BAND_BITS: Final[int] = SIMHASH_BITS // BAND_COUNT
_BAND_MASK: Final[int] = (1 << BAND_BITS) - 1

#: Размер шингла в словах.
DEFAULT_SHINGLE_SIZE: Final[int] = 3

_UINT64_MASK: Final[int] = (1 << SIMHASH_BITS) - 1
_INT64_OFFSET: Final[int] = 1 << SIMHASH_BITS
_INT64_MAX: Final[int] = (1 << (SIMHASH_BITS - 1)) - 1

_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_MENTION_RE: Final[re.Pattern[str]] = re.compile(r"[@#]\w+")
_NON_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class TextFingerprint:
    """Полный набор отпечатков одного текста."""

    content_hash: str
    simhash: int
    """Simhash как знаковое 64-битное целое, пригодное для ``BIGINT``."""
    bands: tuple[int, int, int, int]

    @property
    def is_empty(self) -> bool:
        """Был ли исходный текст пустым после нормализации."""
        return self.simhash == 0 and all(band == 0 for band in self.bands)


def normalize_text(text: str) -> str:
    """Приводит текст к канонической форме для сравнения.

    Удаляются ссылки, упоминания, хэштеги, эмодзи и пунктуация: одна и та же
    новость в разных каналах отличается именно этой «обвязкой», а не сутью.

    :param text: Исходный текст.
    :return: Нормализованный текст в нижнем регистре.
    """
    if not text:
        return ""

    lowered = text.lower()
    without_urls = _URL_RE.sub(" ", lowered)
    without_mentions = _MENTION_RE.sub(" ", without_urls)
    without_punctuation = _NON_WORD_RE.sub(" ", without_mentions)
    return _SPACE_RE.sub(" ", without_punctuation).strip()


def content_hash(text: str) -> str:
    """Возвращает SHA-256 нормализованного текста.

    :param text: Исходный текст.
    :return: 64 hex-символа; для пустого текста — хэш пустой строки.
    """
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def shingles(text: str, size: int = DEFAULT_SHINGLE_SIZE) -> list[str]:
    """Разбивает текст на пересекающиеся словосочетания длиной ``size``.

    :param text: Исходный текст (нормализация выполняется внутри).
    :param size: Число слов в шингле (не меньше 1).
    :return: Список шинглов; для коротких текстов — список из одного элемента.
    :raises ValueError: Некорректный размер шингла.
    """
    if size < 1:
        raise ValueError(f"Размер шингла должен быть не меньше 1, получено: {size}")

    words = normalize_text(text).split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]

    return [" ".join(words[index : index + size]) for index in range(len(words) - size + 1)]


def simhash(text: str, shingle_size: int = DEFAULT_SHINGLE_SIZE) -> int:
    """Вычисляет 64-битный simhash текста.

    Алгоритм Чарикара: каждый шингл хэшируется, биты его хэша голосуют
    «+1»/«-1» в аккумуляторе, итоговый бит определяется знаком суммы.
    Схожие тексты отличаются малым числом бит.

    :param text: Исходный текст.
    :param shingle_size: Размер шингла в словах.
    :return: Беззнаковое 64-битное значение (0 для пустого текста).
    :raises ValueError: Некорректный размер шингла.
    """
    parts = shingles(text, shingle_size)
    if not parts:
        logger.debug("Simhash не вычислен: текст пуст после нормализации.")
        return 0

    accumulator = [0] * SIMHASH_BITS
    for part in parts:
        digest = hashlib.blake2b(part.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, byteorder="big", signed=False)
        for bit in range(SIMHASH_BITS):
            if value >> bit & 1:
                accumulator[bit] += 1
            else:
                accumulator[bit] -= 1

    result = 0
    for bit, weight in enumerate(accumulator):
        if weight > 0:
            result |= 1 << bit
    return result & _UINT64_MASK


def hamming_distance(left: int, right: int) -> int:
    """Считает расстояние Хэмминга между двумя simhash.

    :param left: Первое значение (знаковое или беззнаковое).
    :param right: Второе значение.
    :return: Число различающихся бит (0..64).
    """
    return ((to_unsigned64(left) ^ to_unsigned64(right)) & _UINT64_MASK).bit_count()


def to_signed64(value: int) -> int:
    """Переводит беззнаковое 64-битное значение в знаковое для ``BIGINT``.

    PostgreSQL не поддерживает беззнаковые целые: без этого преобразования
    любой simhash со старшим установленным битом вызвал бы переполнение
    при вставке.

    :param value: Беззнаковое значение.
    :return: Значение в диапазоне ``BIGINT``.
    """
    masked = value & _UINT64_MASK
    return masked - _INT64_OFFSET if masked > _INT64_MAX else masked


def to_unsigned64(value: int) -> int:
    """Обратное преобразование знакового ``BIGINT`` в беззнаковое значение.

    :param value: Знаковое значение из БД.
    :return: Беззнаковое 64-битное значение.
    """
    return value & _UINT64_MASK


def simhash_bands(value: int) -> tuple[int, int, int, int]:
    """Режет simhash на четыре 16-битных среза.

    :param value: Simhash (знаковый или беззнаковый).
    :return: Кортеж из четырёх целых в диапазоне 0..65535.
    """
    unsigned = to_unsigned64(value)
    bands = tuple(
        (unsigned >> (index * BAND_BITS)) & _BAND_MASK for index in range(BAND_COUNT)
    )
    # Явная распаковка вместо среза: сигнатура обещает ровно четыре элемента.
    return bands[0], bands[1], bands[2], bands[3]


def build_fingerprint(text: str, shingle_size: int = DEFAULT_SHINGLE_SIZE) -> TextFingerprint:
    """Собирает полный отпечаток текста.

    Метод не бросает исключений на «плохих» данных: пустой или состоящий из
    одних ссылок пост — штатная ситуация конвейера, он получает нулевой
    simhash и просто не участвует в поиске похожих.

    :param text: Исходный текст записи.
    :param shingle_size: Размер шингла в словах.
    :return: Отпечаток, готовый к записи в модель :class:`db.models.post.Post`.
    """
    try:
        raw_simhash = simhash(text, shingle_size)
    except ValueError:
        logger.exception("Некорректные параметры simhash, отпечаток обнулён.")
        raw_simhash = 0

    return TextFingerprint(
        content_hash=content_hash(text),
        simhash=to_signed64(raw_simhash),
        bands=simhash_bands(raw_simhash),
    )
