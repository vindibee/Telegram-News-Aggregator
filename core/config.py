"""Типизированная конфигурация приложения.

Все значения читаются из переменных окружения (или из ``.env`` при локальном
запуске) ровно один раз на старте и дальше используются как неизменяемые
объекты, что исключает рассинхронизацию настроек между модулями.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from sqlalchemy import URL

load_dotenv(".env", override=False)


class ConfigError(RuntimeError):
    """Некорректная или неполная конфигурация приложения."""


# --------------------------------------------------------------------------- #
# Жёсткие лимиты Bot API. Вынесены в константы, чтобы не «магичить» числами.
# --------------------------------------------------------------------------- #
MAX_MESSAGE_LENGTH: Final[int] = 4096
MAX_CAPTION_LENGTH: Final[int] = 1024
MAX_MEDIA_GROUP_SIZE: Final[int] = 10
MAX_BUTTON_TEXT_LENGTH: Final[int] = 64


def _get_str(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(key, default if default is not None else "").strip()
    if required and not value:
        raise ConfigError(f"Переменная окружения {key} не задана.")
    return value


def _get_int(key: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"Переменная окружения {key} должна быть целым числом, получено: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"Переменная окружения {key} должна быть не меньше {minimum}, получено: {value}")
    return value


def _get_float(key: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"Переменная окружения {key} должна быть числом, получено: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"Переменная окружения {key} должна быть не меньше {minimum}, получено: {value}")
    return value


def _get_timezone(key: str, default: str = "UTC") -> tzinfo:
    name = os.getenv(key, default).strip() or default
    # UTC берём из stdlib напрямую: он доступен даже там, где нет базы tzdata.
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"Переменная окружения {key} содержит неизвестную таймзону: {name!r}. "
            "Убедитесь, что установлен пакет tzdata."
        ) from exc


def _get_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Параметры подключения к PostgreSQL."""

    user: str
    password: str
    name: str
    host: str
    port: int
    echo: bool
    pool_size: int
    max_overflow: int
    pool_recycle: int

    @property
    def url(self) -> URL:
        """DSN для asyncpg.

        ``URL.create`` сам экранирует спецсимволы в пароле — ручная f-строка
        ломалась бы на паролях с ``@``, ``/`` или ``:``.
        """
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
        )


@dataclass(frozen=True, slots=True)
class ParserConfig:
    """Параметры парсера и загрузчика медиа."""

    cooldown: int
    max_posts: int
    request_timeout: float
    media_timeout: float
    max_media_bytes: int
    max_media_per_post: int
    user_agent: str


@dataclass(frozen=True, slots=True)
class Channel:
    """Описание отслеживаемого публичного канала."""

    label: str
    username: str


@dataclass(frozen=True, slots=True)
class Settings:
    """Корневой объект настроек приложения."""

    bot_token: str
    log_level: str
    display_timezone: tzinfo
    db: DatabaseConfig
    parser: ParserConfig
    channels: tuple[Channel, ...] = field(default_factory=tuple)

    def channel_by_username(self, username: str) -> Channel | None:
        """Возвращает канал из белого списка либо ``None``.

        Используется как валидация пользовательского ввода: бот работает только
        с заранее объявленными каналами и не ходит по произвольным URL.
        """
        normalized = username.strip().lstrip("@").lower()
        for channel in self.channels:
            if channel.username.lower() == normalized:
                return channel
        return None


#: Белый список каналов, доступных пользователю.
CHANNELS: Final[tuple[Channel, ...]] = (
    Channel(label="🎮 Игромания", username="igromania"),
    Channel(label="📱 Wylsacom Red", username="wylsared"),
    Channel(label="🌍 Новости Одесса", username="our_odessa"),
    Channel(label="🚀 Хабр", username="habr_com"),
    Channel(label="📊 РБК", username="rbc_news"),
    Channel(label="⚡️ Mash", username="breakingmash"),
    Channel(label="🧠 ПостНаука", username="postnauka"),
    Channel(label="🍿 Кинопоиск", username="kinopoisk"),
    Channel(label="📰 Лентач", username="lentachold"),
    Channel(label="💾 IT Музей", username="computer_history"),
)

_DEFAULT_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_settings() -> Settings:
    """Собирает и валидирует настройки. Бросает :class:`ConfigError` при ошибке."""
    bot_token = _get_str("BOT_TOKEN", required=True)
    if ":" not in bot_token:
        raise ConfigError("BOT_TOKEN имеет некорректный формат (ожидается '<id>:<hash>').")

    database = DatabaseConfig(
        user=_get_str("DB_USER", "postgres"),
        password=_get_str("DB_PASS", "postgres"),
        name=_get_str("DB_NAME", "news_db"),
        host=_get_str("DB_HOST", "db"),
        port=_get_int("DB_PORT", 5432, minimum=1),
        echo=_get_bool("DB_ECHO", False),
        pool_size=_get_int("DB_POOL_SIZE", 10, minimum=1),
        max_overflow=_get_int("DB_MAX_OVERFLOW", 5, minimum=0),
        pool_recycle=_get_int("DB_POOL_RECYCLE", 1800, minimum=60),
    )

    parser = ParserConfig(
        cooldown=_get_int("PARSE_COOLDOWN", 60, minimum=0),
        max_posts=_get_int("MAX_POSTS", 10, minimum=1),
        request_timeout=_get_float("REQUEST_TIMEOUT", 15.0, minimum=1.0),
        media_timeout=_get_float("MEDIA_TIMEOUT", 30.0, minimum=1.0),
        # 20 МиБ — компромисс между лимитами Bot API и защитой от OOM.
        max_media_bytes=_get_int("MAX_MEDIA_BYTES", 20 * 1024 * 1024, minimum=1024),
        max_media_per_post=_get_int("MAX_MEDIA_PER_POST", MAX_MEDIA_GROUP_SIZE, minimum=1),
        user_agent=_get_str("USER_AGENT", _DEFAULT_USER_AGENT),
    )

    return Settings(
        bot_token=bot_token,
        log_level=_get_str("LOG_LEVEL", "INFO"),
        display_timezone=_get_timezone("DISPLAY_TZ", "UTC"),
        db=database,
        parser=parser,
        channels=CHANNELS,
    )
