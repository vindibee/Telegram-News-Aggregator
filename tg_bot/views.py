"""Отрисовка постов в Telegram.

Логика вывода вынесена из хендлеров: хендлер решает «что показать»,
рендерер — «как показать».
"""

from __future__ import annotations

from datetime import tzinfo
from html import escape
from typing import Any, Final

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from core.config import MAX_CAPTION_LENGTH, MAX_MEDIA_GROUP_SIZE
from core.logger import get_logger
from db.models import NewsPost
from services.media import DownloadedMedia, MediaDownloader
from services.parser import MediaItem
from tg_bot.keyboards import kb_back
from tg_bot.utils import safe_edit_text, split_text, with_flood_retry

logger = get_logger(__name__)

_EMPTY_TEXT: Final[str] = "🖼 Пост без текста."
_MEDIA_FAILED: Final[str] = "⚠️ Медиафайлы недоступны, показываю только текст."
_BACK_PROMPT: Final[str] = "Что дальше?"

_MEDIA_CLASSES: Final[dict[str, type[InputMediaPhoto] | type[InputMediaVideo]]] = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
}


class PostRenderer:
    """Отправляет карточку поста: заголовок, медиа и текст."""

    def __init__(self, downloader: MediaDownloader, display_tz: tzinfo) -> None:
        self._downloader = downloader
        self._display_tz = display_tz

    async def render(self, message: Message, post: NewsPost) -> None:
        """Показывает пост в чате.

        Исходное сообщение превращается в заголовок карточки, тело поста
        отправляется следом отдельными сообщениями.
        """
        await safe_edit_text(message, self._header(post))

        media_items = self._media_items(post)
        text = (post.content or "").strip()

        if not media_items:
            await self._send_text(message, text or _EMPTY_TEXT)
            await message.answer(_BACK_PROMPT, reply_markup=kb_back(post.channel_name))
            return

        downloaded = await self._downloader.download_many(media_items)
        if not downloaded:
            await message.answer(_MEDIA_FAILED)
            await self._send_text(message, text or _EMPTY_TEXT)
            await message.answer(_BACK_PROMPT, reply_markup=kb_back(post.channel_name))
            return

        # Подпись помещается в медиа, только если укладывается в лимит Bot API;
        # иначе текст уходит отдельными сообщениями и ничего не теряется.
        caption = text if 0 < len(text) <= MAX_CAPTION_LENGTH else None
        sent = await self._send_media(message, downloaded, caption)

        if not sent:
            await message.answer(_MEDIA_FAILED)
            await self._send_text(message, text or _EMPTY_TEXT)
        elif caption is None and text:
            await self._send_text(message, text)

        await message.answer(_BACK_PROMPT, reply_markup=kb_back(post.channel_name))

    def _header(self, post: NewsPost) -> str:
        stamp = post.post_time.astimezone(self._display_tz).strftime("%d.%m.%Y %H:%M")
        return f"📍 Пост от <b>{escape(stamp)}</b> | @{escape(post.channel_name)}"

    @staticmethod
    def _media_items(post: NewsPost) -> list[MediaItem]:
        """Восстанавливает медиа из JSONB, отбрасывая повреждённые записи."""
        raw: Any = post.media_urls or []
        if not isinstance(raw, list):
            logger.warning("Некорректный media_urls у поста id=%s: %r", post.id, type(raw))
            return []

        items: list[MediaItem] = []
        for entry in raw[:MAX_MEDIA_GROUP_SIZE]:
            if not isinstance(entry, dict):
                continue
            media_type = entry.get("type")
            url = entry.get("url")
            if media_type in _MEDIA_CLASSES and isinstance(url, str) and url:
                items.append(MediaItem(type=media_type, url=url))
        return items

    @staticmethod
    async def _send_text(message: Message, text: str) -> None:
        """Отправляет текст, разбивая его на части по лимиту Telegram.

        ``parse_mode=None`` — принципиально: содержимое канала произвольное,
        и любая попытка разметить его как HTML ломается на «<» или обрезанной
        HTML-сущности.
        """
        for chunk in split_text(text) or [_EMPTY_TEXT]:
            await with_flood_retry(lambda body=chunk: message.answer(body, parse_mode=None))

    async def _send_media(
        self,
        message: Message,
        downloaded: list[DownloadedMedia],
        caption: str | None,
    ) -> bool:
        """Отправляет одно вложение или альбом. Возвращает признак успеха."""
        try:
            if len(downloaded) == 1:
                await self._send_single(message, downloaded[0], caption)
            else:
                group = self._build_group(downloaded, caption)
                await with_flood_retry(lambda: message.answer_media_group(group))
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить медиа поста: %s", exc)
            return False
        return True

    @staticmethod
    async def _send_single(message: Message, media: DownloadedMedia, caption: str | None) -> None:
        file = BufferedInputFile(media.payload, filename=media.filename)
        if media.item.type == "photo":
            await with_flood_retry(
                lambda: message.answer_photo(file, caption=caption, parse_mode=None)
            )
        else:
            await with_flood_retry(
                lambda: message.answer_video(file, caption=caption, parse_mode=None)
            )

    @staticmethod
    def _build_group(
        downloaded: list[DownloadedMedia],
        caption: str | None,
    ) -> list[InputMediaPhoto | InputMediaVideo]:
        group: list[InputMediaPhoto | InputMediaVideo] = []
        for index, media in enumerate(downloaded[:MAX_MEDIA_GROUP_SIZE]):
            media_class = _MEDIA_CLASSES[media.item.type]
            file = BufferedInputFile(media.payload, filename=f"{index}_{media.filename}")
            group.append(
                media_class(
                    media=file,
                    # В альбоме подпись показывается только у первого элемента.
                    caption=caption if index == 0 else None,
                    parse_mode=None,
                )
            )
        return group
