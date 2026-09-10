"""Трекинговые ссылки: подмена в тексте, учёт переходов и отчёты.

Три части с разными требованиями, поэтому и разделены.

**Подмена ссылок** выполняется при подготовке публикации: находит внешние
адреса в тексте и заменяет их короткими, за которыми стоит запись в базе.

**Учёт переходов** обязан быть быстрым: пользователь ждёт редиректа, и
запись в PostgreSQL в этот момент — самая дорогая часть ответа. Поэтому
событие кладётся в очередь Redis, а в базу переносится пачкой фоновой
задачей. Цена решения честная: падение Redis теряет переходы, накопленные
с последнего сброса. Для аналитики это допустимо — терять деньги или
доступ так нельзя, а несколько кликов можно.

**Отчёты** читают уже перенесённые в базу данные: показывать «живое»
число, собранное из двух источников, значит объяснять пользователю, почему
оно то растёт, то откатывается.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from core.logger import get_logger
from db.models import TrackedLink
from db.repositories.tracking import ClickEvent, LinkStats, OwnerTotals, TrackedLinkRepository

logger = get_logger(__name__)

#: Ссылки в тексте. Хвостовая пунктуация отсекается отдельно: точка в
#: конце предложения не является частью адреса, а вот точка внутри пути —
#: является, и различить их регулярным выражением одним махом не выйдет.
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

#: Символы, которые почти никогда не завершают настоящий адрес.
_TRAILING: Final[str] = ".,;:!?)»\"'"

#: Домены, которые подменять не нужно: ссылка на сам Telegram ведёт внутрь
#: клиента, и редирект через внешний сервер только ломает переход.
_SKIP_HOSTS: Final[frozenset[str]] = frozenset({"t.me", "telegram.me", "telegram.org"})

#: Очередь необработанных переходов в Redis.
_QUEUE_KEY: Final[str] = "clicks:queue"

#: Сколько событий забирать из очереди за один сброс.
DEFAULT_FLUSH_BATCH: Final[int] = 500

#: Сколько хранить соответствие «токен → адрес» в кэше редиректа.
CACHE_TTL_SECONDS: Final[int] = 6 * 3600


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """Итог подмены ссылок в тексте."""

    text: str
    links: tuple[TrackedLink, ...]

    @property
    def replaced(self) -> int:
        """Сколько ссылок заменено."""
        return len(self.links)


class LinkConverter:
    """Заменяет внешние ссылки в тексте на трекинговые."""

    def __init__(
        self,
        repo: TrackedLinkRepository,
        base_url: str,
        *,
        link_ttl: timedelta | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("Базовый адрес коротких ссылок не задан.")
        self._repo = repo
        self._base_url = base_url.rstrip("/")
        self._link_ttl = link_ttl

    async def convert(
        self,
        text: str,
        *,
        owner_id: int | None = None,
        post_id: int | None = None,
    ) -> ConversionResult:
        """Подменяет адреса в тексте короткими ссылками.

        Один и тот же адрес, встретившийся дважды, получает одну короткую
        ссылку: иначе статистика по нему разошлась бы на две записи, а
        читателю показались бы разные ссылки на одно и то же.

        :param text: Исходный текст поста.
        :param owner_id: Владелец ссылок.
        :param post_id: Пост, из которого они ведут.
        :return: Новый текст и созданные ссылки.
        """
        if not text:
            return ConversionResult(text=text, links=())

        expires_at = (
            datetime.now(tz=timezone.utc) + self._link_ttl if self._link_ttl else None
        )
        created: dict[str, TrackedLink] = {}
        replacements: list[tuple[int, int, str]] = []

        for match in _URL_RE.finditer(text):
            raw = match.group(0)
            url = raw.rstrip(_TRAILING)
            if not self._should_track(url):
                continue

            link = created.get(url)
            if link is None:
                try:
                    safe = TrackedLink.validate_target_url(url)
                except ValueError as exc:
                    logger.info("Ссылка %r пропущена: %s", url[:80], exc)
                    continue
                link = await self._repo.create(
                    target_url=safe,
                    owner_id=owner_id,
                    post_id=post_id,
                    expires_at=expires_at,
                )
                created[url] = link

            replacements.append((match.start(), match.start() + len(url), self.short_url(link)))

        if not replacements:
            return ConversionResult(text=text, links=())

        # Замены применяются с конца: иначе первая же сдвинула бы позиции
        # всех последующих совпадений.
        result = text
        for start, end, short in reversed(replacements):
            result = f"{result[:start]}{short}{result[end:]}"

        logger.info("В тексте заменено ссылок: %d", len(created))
        return ConversionResult(text=result, links=tuple(created.values()))

    def short_url(self, link: TrackedLink) -> str:
        """Собирает публичный адрес короткой ссылки."""
        return f"{self._base_url}/r/{link.token}"

    @staticmethod
    def _should_track(url: str) -> bool:
        """Решает, подменять ли адрес."""
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        return not any(host == skip or host.endswith(f".{skip}") for skip in _SKIP_HOSTS)


class ClickCounter:
    """Очередь переходов в Redis и её перенос в базу.

    Redis здесь — буфер, а не хранилище статистики: источником правды
    остаётся PostgreSQL. Такое разделение убирает запись в базу из ответа
    на редирект, где важна каждая миллисекунда, но означает, что переходы,
    не успевшие попасть в сброс, теряются вместе с Redis.
    """

    def __init__(self, client: Any | None, *, prefix: str = "newsbot") -> None:
        self._client = client
        self._key = f"{prefix}:{_QUEUE_KEY}"
        self._cache_prefix = f"{prefix}:clicks:url"

    @property
    def enabled(self) -> bool:
        """Настроен ли буфер переходов."""
        return self._client is not None

    async def record(self, event: ClickEvent) -> bool:
        """Ставит переход в очередь на запись.

        :param event: Состоявшийся переход.
        :return: ``True``, если событие принято в очередь.
        """
        if self._client is None:
            return False

        from redis.exceptions import RedisError

        payload = json.dumps(
            {
                "token": event.token,
                "clicked_at": event.clicked_at.isoformat(),
                "visitor_hash": event.visitor_hash,
                "user_id": event.user_id,
                "referer": event.referer,
            }
        )
        try:
            await self._client.rpush(self._key, payload)
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при учёте перехода: %s", exc)
            return False
        return True

    async def drain(self, batch: int = DEFAULT_FLUSH_BATCH) -> list[ClickEvent]:
        """Забирает накопленные переходы из очереди.

        События именно забираются, а не читаются: повторный сброс тех же
        строк удвоил бы счётчики. Обратная сторона — падение между
        изъятием и записью теряет пачку, и это осознанный размен в пользу
        того, чтобы никогда не считать переходы дважды.

        :param batch: Сколько событий забрать.
        :return: Разобранные переходы.
        """
        if self._client is None or batch <= 0:
            return []

        from redis.exceptions import RedisError

        try:
            raw_items = await self._client.lpop(self._key, batch)
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при чтении очереди переходов: %s", exc)
            return []

        if not raw_items:
            return []
        if isinstance(raw_items, (str, bytes)):
            raw_items = [raw_items]

        events: list[ClickEvent] = []
        for raw in raw_items:
            try:
                payload = json.loads(raw)
                events.append(
                    ClickEvent(
                        token=payload["token"],
                        clicked_at=datetime.fromisoformat(payload["clicked_at"]),
                        visitor_hash=payload.get("visitor_hash"),
                        user_id=payload.get("user_id"),
                        referer=payload.get("referer"),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Повреждённое событие перехода пропущено: %s", exc)

        return events

    async def pending(self) -> int:
        """Сколько переходов ждёт переноса в базу."""
        if self._client is None:
            return 0

        from redis.exceptions import RedisError

        try:
            return int(await self._client.llen(self._key))
        except (RedisError, OSError):
            return 0

    # ------------------------------------------------------- кэш редиректа
    async def cache_target(self, token: str, url: str) -> None:
        """Запоминает адрес назначения для быстрых редиректов."""
        if self._client is None:
            return

        from redis.exceptions import RedisError

        try:
            await self._client.set(f"{self._cache_prefix}:{token}", url, ex=CACHE_TTL_SECONDS)
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при записи адреса в кэш: %s", exc)

    async def cached_target(self, token: str) -> str | None:
        """Возвращает адрес назначения из кэша."""
        if self._client is None:
            return None

        from redis.exceptions import RedisError

        try:
            raw = await self._client.get(f"{self._cache_prefix}:{token}")
        except (RedisError, OSError) as exc:
            logger.warning("Redis недоступен при чтении адреса из кэша: %s", exc)
            return None

        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    async def forget_target(self, token: str) -> None:
        """Убирает адрес из кэша — например, при отключении ссылки."""
        if self._client is None:
            return

        from redis.exceptions import RedisError

        try:
            await self._client.delete(f"{self._cache_prefix}:{token}")
        except (RedisError, OSError):
            pass


@dataclass(frozen=True, slots=True)
class AnalyticsReport:
    """Отчёт по трекинговым ссылкам владельца."""

    totals: OwnerTotals
    top: tuple[LinkStats, ...]

    @property
    def is_empty(self) -> bool:
        """Есть ли о чём отчитываться."""
        return self.totals.clicks == 0

    @property
    def repeat_rate(self) -> float:
        """Доля повторных переходов среди всех."""
        if self.totals.clicks <= 0:
            return 0.0
        return 1 - self.totals.unique_clicks / self.totals.clicks


class AnalyticsService:
    """Собирает отчёты по переходам."""

    def __init__(self, repo: TrackedLinkRepository) -> None:
        self._repo = repo

    async def build_report(self, owner_id: int, *, top: int = 5) -> AnalyticsReport:
        """Готовит отчёт для владельца ссылок.

        :param owner_id: Владелец.
        :param top: Сколько ссылок показать в подборке.
        :return: Сводка и подборка самых популярных ссылок.
        """
        totals = await self._repo.totals_for_owner(owner_id)
        best = await self._repo.top_for_owner(owner_id, limit=top)

        logger.info(
            "Отчёт для владельца %s: ссылок %d, переходов %d",
            owner_id, totals.links, totals.clicks,
        )
        return AnalyticsReport(totals=totals, top=tuple(best))
