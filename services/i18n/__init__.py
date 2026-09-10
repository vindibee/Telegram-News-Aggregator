"""Локализация интерфейса бота."""

from services.i18n.cache import (
    DEFAULT_TTL_SECONDS,
    InMemoryLanguageCache,
    LanguageCache,
    RedisLanguageCache,
    build_language_cache,
)
from services.i18n.catalog import (
    DEFAULT_LOCALES_DIR,
    TranslationError,
    TranslationManager,
    select_plural_form,
)
from services.i18n.translator import Translator

__all__ = [
    "DEFAULT_LOCALES_DIR",
    "DEFAULT_TTL_SECONDS",
    "InMemoryLanguageCache",
    "LanguageCache",
    "RedisLanguageCache",
    "TranslationError",
    "TranslationManager",
    "Translator",
    "build_language_cache",
    "select_plural_form",
]
