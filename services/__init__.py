"""Прикладной слой: парсинг, загрузка медиа и сценарии работы с новостями."""

from services.cooldown import CooldownStorage
from services.fingerprint import TextFingerprint, build_fingerprint, hamming_distance
from services.media import DownloadedMedia, MediaDownloader
from services.news_service import NewsService, RefreshResult
from services.parser import (
    ChannelUnavailableError,
    MediaItem,
    NetworkError,
    ParsedPost,
    ParserError,
    TelegramWebParser,
)

__all__ = [
    "ChannelUnavailableError",
    "CooldownStorage",
    "DownloadedMedia",
    "MediaDownloader",
    "MediaItem",
    "NetworkError",
    "NewsService",
    "ParsedPost",
    "ParserError",
    "RefreshResult",
    "TelegramWebParser",
    "TextFingerprint",
    "build_fingerprint",
    "hamming_distance",
]
