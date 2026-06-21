import re
from datetime import datetime
from bs4 import BeautifulSoup
import aiohttp
from core.logger import logger


async def parse_channel(session: aiohttp.ClientSession, channel: str, max_posts: int = 10) -> tuple[
    list[dict], str | None]:
    """
    Парсит публичный канал Telegram через веб-интерфейс t.me/s/.
    Возвращает кортеж (список постов, текст ошибки).
    """
    url = f"https://t.me/s/{channel}"

    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                return [], f"Ошибка HTTP {resp.status}"
            html = await resp.text()
    except Exception as e:
        logger.error(f"Network error parsing @{channel}: {e}")
        return [], "Ошибка сети при парсинге"

    soup = BeautifulSoup(html, "html.parser")
    raw_messages = soup.find_all("div", class_="tgme_widget_message")

    # Фильтруем сообщения (оставляем только те, где есть текст или фото)
    messages = [
        m for m in raw_messages
        if m.find("div", class_="tgme_widget_message_text")
           or m.find("a", class_="tgme_widget_message_photo_wrap")
    ]

    result = []
    for msg in messages[-max_posts:]:
        # Извлечение времени
        time_tag = msg.find("time")
        p_time = datetime.now()
        if time_tag and time_tag.get("datetime"):
            try:
                # Конвертируем ISO формат в naive datetime для БД
                dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
                p_time = dt.replace(tzinfo=None)
            except ValueError:
                pass

        # Извлечение текста
        text_div = msg.find("div", class_="tgme_widget_message_text")
        text = text_div.get_text(separator="\n").strip() if text_div else ""

        # Извлечение медиа
        media = []
        for photo in msg.find_all("a", class_="tgme_widget_message_photo_wrap"):
            m = re.search(r"url\('(.*?)'\)", photo.get("style", ""))
            if m:
                media.append({"type": "photo", "url": m.group(1)})

        for video in msg.find_all("video"):
            if video.get("src"):
                media.append({"type": "video", "url": video["src"]})

        result.append({
            "post_time": p_time,
            "text": text,
            "media": media,
        })

    return result, None