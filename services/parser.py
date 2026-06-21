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