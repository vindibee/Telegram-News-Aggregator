"""Парсер публичных Telegram-каналов через веб-превью t.me/s/<channel>."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Literal
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup, Tag

from core.config import ParserConfig
from core.logger import get_logger

logger = get_logger(__name__)

MediaType = Literal["photo", "video"]

_BASE_URL: Final[str] = "https://t.me/s/{channel}"
_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_BACKGROUND_URL_RE: Final[re.Pattern[str]] = re.compile(r"url\((?P<quote>['\"]?)(?P<url>.*?)(?P=quote)\)")

#: Домены, с которых разрешено скачивать медиа (защита от SSRF: HTML приходит
#: извне, и подменённый в нём URL иначе заставил бы бота ходить во внутреннюю сеть).
_ALLOWED_MEDIA_HOSTS: Final[tuple[str, ...]] = (
    "cdn-telegram.org",
    "cdn-telegram.com",
    "telesco.pe",
    "telegram.org",
    "telegram-cdn.org",
    "t.me",
)

_MEDIA_SELECTOR: Final[str] = (
    "a.tgme_widget_message_photo_wrap, "
    "video.tgme_widget_message_video, "
    "video.tgme_widget_message_roundvideo"
)


class ParserError(Exception):
    """Базовая ошибка парсинга с текстом, пригодным для показа пользователю."""


class ChannelUnavailableError(ParserError):
    """Канал не существует, приватный или не отдаёт веб-превью."""


class NetworkError(ParserError):
    """Сетевая ошибка или таймаут при обращении к t.me."""


@dataclass(frozen=True, slots=True)
class MediaItem:
    """Одно медиавложение поста."""

    type: MediaType
    url: str

    def as_dict(self) -> dict[str, str]:
        """Сериализация для хранения в JSONB."""
        return {"type": self.type, "url": self.url}


@dataclass(frozen=True, slots=True)
class ParsedPost:
    """Распарсенный пост канала."""

    message_id: int
    post_time: datetime
    text: str
    media: tuple[MediaItem, ...]


def is_valid_username(username: str) -> bool:
    """Проверяет имя канала по правилам Telegram (защита от подстановки в URL)."""
    return bool(_USERNAME_RE.match(username))


def is_allowed_media_url(url: str) -> bool:
    """Разрешён ли URL медиа к скачиванию (только HTTPS и домены Telegram)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_MEDIA_HOSTS)


class TelegramWebParser:
    """Достаёт посты канала из HTML веб-превью.

    Экземпляр создаётся один раз на всё приложение и переиспользует общий
    ``aiohttp.ClientSession`` (пул соединений и keep-alive).
    """

    def __init__(self, session: aiohttp.ClientSession, config: ParserConfig) -> None:
        self._session = session
        self._config = config
        self._timeout = aiohttp.ClientTimeout(total=config.request_timeout)

    async def fetch_posts(self, channel: str, limit: int | None = None) -> list[ParsedPost]:
        """Возвращает последние посты канала, отсортированные от старых к новым.

        :param channel: Имя канала без ``@``.
        :param limit: Сколько постов вернуть (по умолчанию — из конфигурации).
        :raises ParserError: Канал недоступен, сеть недоступна или HTML не разобран.
        """
        if not is_valid_username(channel):
            raise ChannelUnavailableError("Некорректное имя канала.")

        html = await self._download(channel)
        # Разбор HTML — CPU-bound операция: уводим её в отдельный поток, чтобы
        # не блокировать event loop на страницах в несколько мегабайт.
        posts = await asyncio.to_thread(self._parse_html, html, channel)

        if not posts:
            raise ChannelUnavailableError(
                "Канал не отдаёт публичные записи (возможно, он приватный или пуст)."
            )

        effective_limit = limit if limit is not None else self._config.max_posts
        return posts[-effective_limit:]

    async def _download(self, channel: str) -> str:
        url = _BASE_URL.format(channel=channel)
        try:
            async with self._session.get(url, timeout=self._timeout, allow_redirects=True) as resp:
                if resp.status == 404:
                    raise ChannelUnavailableError("Канал @" + channel + " не найден.")
                if resp.status != 200:
                    logger.warning("t.me вернул HTTP %s для @%s", resp.status, channel)
                    raise NetworkError(f"Telegram вернул ошибку HTTP {resp.status}.")
                # Кодировку задаём явно: t.me не всегда присылает charset в заголовке.
                return await resp.text(encoding="utf-8", errors="replace")
        except asyncio.TimeoutError as exc:
            logger.warning("Таймаут при загрузке @%s", channel)
            raise NetworkError("Превышено время ожидания ответа Telegram.") from exc
        except aiohttp.ClientError as exc:
            logger.warning("Сетевая ошибка при загрузке @%s: %s", channel, exc)
            raise NetworkError("Не удалось связаться с Telegram.") from exc

    def _parse_html(self, html: str, channel: str) -> list[ParsedPost]:
        soup = BeautifulSoup(html, "html.parser")
        posts: list[ParsedPost] = []

        for block in soup.select("div.tgme_widget_message[data-post]"):
            try:
                post = self._parse_message(block)
            except Exception as exc:  # noqa: BLE001 - битый пост не должен ронять весь разбор
                logger.warning("Пропущен неразобранный пост канала @%s: %s", channel, exc)
                continue
            if post is not None:
                posts.append(post)

        posts.sort(key=lambda item: item.message_id)
        return posts

    def _parse_message(self, block: Tag) -> ParsedPost | None:
        message_id = self._extract_message_id(block)
        if message_id is None:
            return None

        text = self._extract_text(block)
        media = self._extract_media(block)
        if not text and not media:
            # Служебные записи (вступления, закрепления) пользователю не нужны.
            return None

        return ParsedPost(
            message_id=message_id,
            post_time=self._extract_time(block),
            text=text,
            media=media,
        )

    @staticmethod
    def _extract_message_id(block: Tag) -> int | None:
        raw = block.get("data-post", "")
        if not isinstance(raw, str) or "/" not in raw:
            return None
        try:
            return int(raw.rsplit("/", maxsplit=1)[-1])
        except ValueError:
            return None

    @staticmethod
    def _extract_time(block: Tag) -> datetime:
        time_tag = block.find("time", attrs={"datetime": True})
        if isinstance(time_tag, Tag):
            raw = time_tag.get("datetime")
            if isinstance(raw, str):
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    logger.debug("Не удалось разобрать дату поста: %r", raw)
                else:
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc)
        return datetime.now(tz=timezone.utc)

    @staticmethod
    def _extract_text(block: Tag) -> str:
        # Именно `.js-message_text`: у цитируемого сообщения свой блок текста,
        # который не должен попадать в тело поста.
        text_block = block.select_one("div.tgme_widget_message_text.js-message_text")
        if text_block is None:
            text_block = block.select_one("div.tgme_widget_message_text")
        if text_block is None:
            return ""

        # get_text(separator="\n") рвал бы строку на каждом <b>/<a>;
        # переносы должны задавать только теги <br>.
        working_copy = BeautifulSoup(str(text_block), "html.parser")
        for br in working_copy.find_all("br"):
            br.replace_with("\n")

        lines = [line.rstrip() for line in working_copy.get_text().splitlines()]
        return "\n".join(lines).strip()

    def _extract_media(self, block: Tag) -> tuple[MediaItem, ...]:
        items: list[MediaItem] = []
        seen: set[str] = set()

        for element in block.select(_MEDIA_SELECTOR):
            url = self._extract_media_url(element)
            if not url or url in seen:
                continue
            if not is_allowed_media_url(url):
                logger.warning("Медиа с недоверенного домена пропущено: %s", url)
                continue

            seen.add(url)
            media_type: MediaType = "photo" if element.name == "a" else "video"
            items.append(MediaItem(type=media_type, url=url))

            if len(items) >= self._config.max_media_per_post:
                break

        return tuple(items)

    @staticmethod
    def _extract_media_url(element: Tag) -> str | None:
        if element.name == "video":
            src = element.get("src")
            return src if isinstance(src, str) and src else None

        style = element.get("style", "")
        if not isinstance(style, str):
            return None
        match = _BACKGROUND_URL_RE.search(style)
        if match is None:
            return None
        url = match.group("url").strip()
        return url or None
