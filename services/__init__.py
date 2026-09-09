"""Прикладной слой: парсинг, загрузка медиа и сценарии работы с новостями."""

from services.fingerprint import TextFingerprint, build_fingerprint, hamming_distance
from services.ratelimit import (
    AntiFloodPolicy,
    FallbackRateLimiter,
    InMemoryRateLimiter,
    RateLimiter,
    RateLimitRule,
)
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
    "AntiFloodPolicy",
    "FallbackRateLimiter",
    "InMemoryRateLimiter",
    "DownloadedMedia",
    "TextFingerprint",
    "MediaDownloader",
    "MediaItem",
    "NetworkError",
    "NewsService",
    "ParsedPost",
    "ParserError",
    "RefreshResult",
    "RateLimitRule",
    "RateLimiter",
    "TelegramWebParser",
    "build_fingerprint",
    "hamming_distance",
]
