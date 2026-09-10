"""Локализатор, привязанный к одному языку.

Существует ради того, чтобы прикладной код не таскал язык через каждый
вызов. Хендлер получает готовый объект и пишет ``i18n("start.greeting")``
вместо ``manager.get(user.language, "start.greeting")`` — язык уже внутри,
и забыть его передать невозможно.
"""

from __future__ import annotations

from typing import Any

from db.enums import Language
from services.i18n.catalog import TranslationManager


class Translator:
    """Доступ к переводам на конкретном языке."""

    __slots__ = ("_manager", "_language")

    def __init__(self, manager: TranslationManager, language: Language) -> None:
        self._manager = manager
        self._language = language

    @property
    def language(self) -> Language:
        """Язык этого локализатора."""
        return self._language

    def __call__(self, key: str, /, **params: Any) -> str:
        """Короткая форма :meth:`get` — основной способ обращения.

        :param key: Ключ строки.
        :param params: Значения для подстановки.
        :return: Готовый текст.
        """
        return self._manager.get(self._language, key, **params)

    def get(self, key: str, /, **params: Any) -> str:
        """Возвращает переведённую строку.

        :param key: Ключ строки.
        :param params: Значения для подстановки.
        :return: Готовый текст.
        """
        return self._manager.get(self._language, key, **params)

    def plural(self, key: str, count: int, /, **params: Any) -> str:
        """Возвращает строку в форме, согласованной с числом.

        :param key: Ключ с формами числа.
        :param count: Число.
        :param params: Дополнительные значения для подстановки.
        :return: Готовый текст.
        """
        return self._manager.plural(self._language, key, count, **params)

    def switch(self, language: Language) -> Translator:
        """Создаёт локализатор того же каталога на другом языке.

        Нужен там, где ответ формируется на языке, отличном от текущего:
        например, сразу после смены языка пользователь должен увидеть
        подтверждение уже на новом.

        :param language: Требуемый язык.
        :return: Новый локализатор.
        """
        return Translator(self._manager, language)

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<Translator language={self._language.value}>"
