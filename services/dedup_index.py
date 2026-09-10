"""Индекс свежих новостей в Redis для быстрого поиска дубликатов.

Кандидаты на совпадение можно искать и в PostgreSQL — по срезам simhash и
полнотекстовому индексу, — но это два запроса на каждую входящую запись, и
второй из них тяжёлый. Окно дедупликации при этом узкое: сутки-двое, а
объём — тысячи записей, которые прекрасно помещаются в память.

**Redis здесь ускоряет, а PostgreSQL гарантирует.** Индекс — кэш, а не
источник правды: он может быть пустым после перезапуска, отставать или
быть недоступным вовсе. Поэтому промах никогда не означает «дубликатов
нет»: точное совпадение при промахе проверяется в базе, а отбор кандидатов
по срезам всегда дополняется базой. Экономится самый дорогой шаг —
полнотекстовый поиск, — и только когда индекс уже дал достаточно
кандидатов.

Хранятся три вида ключей, все с одинаковым временем жизни:

* ``<prefix>:dd:h:<content_hash>`` → идентификатор записи. Точные
  перепечатки составляют большинство дубликатов, и такой ответ стоит
  одного обращения;
* ``<prefix>:dd:b:<номер среза>:<значение>`` → множество идентификаторов.
  Это тот же LSH by banding, что и в колонках таблицы;
* ``<prefix>:dd:p:<id>`` → simhash и нормализованный текст записи, чтобы
  досчитать сходство, не обращаясь к базе.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from core.config import RedisConfig
from core.logger import get_logger
from services.fingerprint import TextFingerprint

logger = get_logger(__name__)

#: Сколько символов нормализованного текста хранить. Меры сходства
#: работают на шинглах, и хвост очень длинной новости на результат почти
#: не влияет, а память экономит заметно.
MAX_CACHED_TEXT: Final[int] = 4096

#: Верхняя граница числа идентификаторов, читаемых из одного среза.
#: Популярный срез собирает сотни записей, и тащить их все незачем.
MAX_IDS_PER_BAND: Final[int] = 200


@dataclass(frozen=True, slots=True)
class IndexedPost:
    """Запись окна дедупликации, восстановленная из индекса."""

    post_id: int
    simhash: int
    text: str


class DedupIndex(ABC):
    """Индекс свежих записей."""

    @abstractmethod
    async def find_exact(self, content_hash: str) -> int | None:
        """Возвращает идентификатор записи с таким же текстом."""

    @abstractmethod
    async def find_candidates(self, bands: Sequence[int], limit: int) -> list[IndexedPost]:
        """Возвращает записи, у которых совпал хотя бы один срез simhash."""

    @abstractmethod
    async def remember(self, post_id: int, fingerprint: TextFingerprint, text: str) -> None:
        """Запоминает запись на время окна дедупликации."""

    @abstractmethod
    async def close(self) -> None:
        """Освобождает ресурсы."""


class NullDedupIndex(DedupIndex):
    """Заглушка на случай, когда Redis не настроен.

    Не «пустой кэш», а именно отсутствие индекса: все обращения — промах,
    и сервис работает ровно так, как работал до его появления.
    """

    async def find_exact(self, content_hash: str) -> int | None:
        """Всегда промах."""
        return None

    async def find_candidates(self, bands: Sequence[int], limit: int) -> list[IndexedPost]:
        """Всегда пусто."""
        return []

    async def remember(self, post_id: int, fingerprint: TextFingerprint, text: str) -> None:
        """Ничего не делает."""

    async def close(self) -> None:
        """Ничего не делает."""


class RedisDedupIndex(DedupIndex):
    """Индекс окна дедупликации поверх Redis."""

    def __init__(self, client: Any, *, prefix: str = "newsbot", ttl: int = 48 * 3600) -> None:
        if ttl <= 0:
            raise ValueError(f"Время жизни индекса должно быть положительным, получено: {ttl}")
        self._client = client
        self._prefix = prefix
        self._ttl = ttl

    # ------------------------------------------------------------------ ключи
    def _hash_key(self, content_hash: str) -> str:
        return f"{self._prefix}:dd:h:{content_hash}"

    def _band_key(self, index: int, value: int) -> str:
        return f"{self._prefix}:dd:b:{index}:{value}"

    def _post_key(self, post_id: int) -> str:
        return f"{self._prefix}:dd:p:{post_id}"

    # ----------------------------------------------------------------- чтение
    async def find_exact(self, content_hash: str) -> int | None:
        """Ищет точное совпадение текста.

        :param content_hash: Хэш нормализованного текста.
        :return: Идентификатор записи либо ``None``.
        """
        from redis.exceptions import RedisError

        try:
            raw = await self._client.get(self._hash_key(content_hash))
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при поиске точного совпадения: %s", exc)
            return None

        if raw is None:
            return None

        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("В индексе нечисловой идентификатор: %r", raw)
            return None

    async def find_candidates(self, bands: Sequence[int], limit: int) -> list[IndexedPost]:
        """Возвращает записи, совпавшие хотя бы одним срезом simhash.

        :param bands: Срезы simhash проверяемой записи.
        :param limit: Верхняя граница числа кандидатов.
        :return: Восстановленные записи окна.
        """
        from redis.exceptions import RedisError

        if not bands or limit <= 0:
            return []

        try:
            pipe = self._client.pipeline(transaction=False)
            for index, value in enumerate(bands):
                pipe.srandmember(self._band_key(index, value), MAX_IDS_PER_BAND)
            groups = await pipe.execute()
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при отборе кандидатов: %s", exc)
            return []

        post_ids = self._unique_ids(groups, limit)
        if not post_ids:
            return []

        return await self._load(post_ids)

    @staticmethod
    def _unique_ids(groups: Iterable[Any], limit: int) -> list[int]:
        """Сводит идентификаторы из всех срезов, сохраняя порядок."""
        seen: dict[int, None] = {}
        for group in groups or ():
            for raw in group or ():
                try:
                    seen.setdefault(int(raw), None)
                except (TypeError, ValueError):
                    continue
                if len(seen) >= limit:
                    return list(seen)
        return list(seen)

    async def _load(self, post_ids: Sequence[int]) -> list[IndexedPost]:
        """Читает содержимое записей по идентификаторам."""
        from redis.exceptions import RedisError

        try:
            pipe = self._client.pipeline(transaction=False)
            for post_id in post_ids:
                pipe.hmget(self._post_key(post_id), "simhash", "text")
            rows = await pipe.execute()
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при чтении записей окна: %s", exc)
            return []

        loaded: list[IndexedPost] = []
        for post_id, row in zip(post_ids, rows or ()):
            if not row:
                continue
            raw_simhash, raw_text = row[0], row[1]
            if raw_simhash is None or raw_text is None:
                # Запись из среза уже вытеснена по TTL: срез живёт столько
                # же, но истекает независимо.
                continue
            try:
                loaded.append(
                    IndexedPost(post_id=post_id, simhash=int(raw_simhash), text=str(raw_text))
                )
            except (TypeError, ValueError):
                logger.warning("В индексе повреждённая запись id=%s", post_id)

        return loaded

    # ----------------------------------------------------------------- запись
    async def remember(self, post_id: int, fingerprint: TextFingerprint, text: str) -> None:
        """Кладёт запись в окно дедупликации.

        Все ключи пишутся одним конвейером: три отдельных обращения на
        каждую сохранённую новость превратились бы в заметную долю времени
        обработки пачки.

        :param post_id: Идентификатор сохранённой записи.
        :param fingerprint: Отпечаток её текста.
        :param text: Нормализованный текст.
        """
        from redis.exceptions import RedisError

        if post_id <= 0 or fingerprint.is_empty:
            return

        try:
            pipe = self._client.pipeline(transaction=False)
            pipe.set(self._hash_key(fingerprint.content_hash), post_id, ex=self._ttl)

            post_key = self._post_key(post_id)
            pipe.hset(
                post_key,
                mapping={"simhash": fingerprint.simhash, "text": text[:MAX_CACHED_TEXT]},
            )
            pipe.expire(post_key, self._ttl)

            for index, value in enumerate(fingerprint.bands):
                band_key = self._band_key(index, value)
                pipe.sadd(band_key, post_id)
                # Срок продлевается на каждой записи: срез должен жить не
                # меньше самой свежей попавшей в него новости.
                pipe.expire(band_key, self._ttl)

            await pipe.execute()
        except (RedisError, OSError) as exc:
            # Потеря записи в кэше не влияет на корректность: дубликат
            # по-прежнему найдётся через базу.
            logger.warning("Redis недоступен при записи в индекс: %s", exc)

    async def close(self) -> None:
        """Закрывает клиент вместе с пулом соединений."""
        from redis.exceptions import RedisError

        try:
            await self._client.aclose()
            await self._client.connection_pool.disconnect()
        except (RedisError, OSError, AttributeError) as exc:  # pragma: no cover
            logger.warning("Не удалось закрыть клиент Redis: %s", exc)


def build_dedup_index(config: RedisConfig, ttl_hours: int) -> DedupIndex:
    """Создаёт индекс дедупликации по конфигурации.

    :param config: Параметры подключения к Redis.
    :param ttl_hours: Ширина окна дедупликации в часах.
    :return: Индекс поверх Redis либо заглушка.
    :raises RuntimeError: Redis настроен, но библиотека не установлена.
    """
    if not config.enabled:
        logger.info(
            "REDIS_URL не задан: кандидаты для дедупликации ищутся только в PostgreSQL."
        )
        return NullDedupIndex()

    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "REDIS_URL задан, но пакет redis не установлен: добавьте redis в зависимости."
        ) from exc

    client = Redis.from_url(config.url, decode_responses=True)
    logger.info("Индекс дедупликации работает через Redis (окно %d ч)", ttl_hours)
    return RedisDedupIndex(client, prefix=config.prefix, ttl=ttl_hours * 3600)
