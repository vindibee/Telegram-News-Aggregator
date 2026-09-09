"""Загрузка медиафайлов постов в память с жёсткими лимитами."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import aiohttp

from core.config import ParserConfig
from core.logger import get_logger
from services.parser import MediaItem, is_allowed_media_url

logger = get_logger(__name__)

_CHUNK_SIZE: Final[int] = 64 * 1024

_EXTENSIONS: Final[dict[str, str]] = {"photo": "jpg", "video": "mp4"}


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """Скачанное медиа, готовое к отправке в Telegram."""

    item: MediaItem
    payload: bytes

    @property
    def filename(self) -> str:
        """Имя файла с расширением: без него Telegram не определит тип вложения."""
        return f"media.{_EXTENSIONS.get(self.item.type, 'bin')}"


class MediaDownloader:
    """Скачивает вложения параллельно, отбрасывая недоступные и слишком большие."""

    def __init__(self, session: aiohttp.ClientSession, config: ParserConfig) -> None:
        self._session = session
        self._config = config
        self._timeout = aiohttp.ClientTimeout(total=config.media_timeout)

    async def download_many(self, items: Sequence[MediaItem]) -> list[DownloadedMedia]:
        """Скачивает вложения, сохраняя исходный порядок.

        Отдельные сбои не считаются фатальными: недоступные файлы просто
        исключаются из результата.
        """
        limited = list(items)[: self._config.max_media_per_post]
        if not limited:
            return []

        results = await asyncio.gather(
            *(self._download_one(item) for item in limited),
            return_exceptions=True,
        )

        downloaded: list[DownloadedMedia] = []
        for item, result in zip(limited, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Не удалось скачать медиа %s: %s", item.url, result)
                continue
            if result is not None:
                downloaded.append(result)
        return downloaded

    async def _download_one(self, item: MediaItem) -> DownloadedMedia | None:
        if not is_allowed_media_url(item.url):
            logger.warning("Скачивание запрещённого URL отклонено: %s", item.url)
            return None

        try:
            async with self._session.get(item.url, timeout=self._timeout) as resp:
                if resp.status != 200:
                    logger.warning("Медиа %s недоступно: HTTP %s", item.url, resp.status)
                    return None

                # Ранний отказ по Content-Length, не начиная качать тело.
                declared = resp.content_length
                if declared is not None and declared > self._config.max_media_bytes:
                    logger.warning(
                        "Медиа %s пропущено: %d байт превышает лимит %d",
                        item.url, declared, self._config.max_media_bytes,
                    )
                    return None

                payload = await self._read_limited(resp, item.url)
                if payload is None:
                    return None
        except asyncio.TimeoutError:
            logger.warning("Таймаут загрузки медиа %s", item.url)
            return None
        except aiohttp.ClientError as exc:
            logger.warning("Сетевая ошибка загрузки медиа %s: %s", item.url, exc)
            return None

        return DownloadedMedia(item=item, payload=payload)

    async def _read_limited(self, resp: aiohttp.ClientResponse, url: str) -> bytes | None:
        """Читает тело ответа чанками, прерываясь при превышении лимита.

        Стриминг вместо ``resp.read()`` — защита от OOM: сервер может не
        объявить Content-Length и отдать файл произвольного размера.
        """
        buffer = bytearray()
        async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
            buffer.extend(chunk)
            if len(buffer) > self._config.max_media_bytes:
                logger.warning(
                    "Медиа %s прервано: превышен лимит %d байт", url, self._config.max_media_bytes
                )
                return None
        return bytes(buffer)
