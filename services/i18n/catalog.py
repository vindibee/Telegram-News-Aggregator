"""Менеджер переводов: загрузка каталогов и подстановка значений.

Каталоги — обычные JSON-файлы, а не Fluent или gettext. Причина
практическая: объём текста в боте измеряется десятками строк, переводят
их разработчики, а не отдельная команда локализации. Fluent потребовал
бы ещё одну зависимость и свой синтаксис ради возможностей, которые здесь
не используются, а gettext — цикла компиляции ``.po`` → ``.mo`` при каждой
правке. JSON правится в любом редакторе и проверяется тестом.

Единственное, чего не хватает в «плоском» подходе — множественные формы.
Русский и украинский требуют три формы («1 день», «2 дня», «5 дней»), и
подстановка числа без учёта формы выглядит как машинный перевод. Поэтому
здесь есть свой минимальный выбор формы: см. :meth:`TranslationManager.plural`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from core.logger import get_logger
from db.enums import Language

logger = get_logger(__name__)

#: Каталог с файлами переводов по умолчанию: ``<корень проекта>/locales``.
DEFAULT_LOCALES_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "locales"

#: Разделитель уровней вложенности в ключе (``billing.plans.header``).
KEY_SEPARATOR: Final[str] = "."

#: Имя вложенного словаря с множественными формами.
PLURAL_KEY: Final[str] = "__plural__"


class TranslationError(RuntimeError):
    """Каталоги переводов повреждены или неполны."""


def _flatten(data: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Разворачивает вложенный словарь в плоские ключи через точку.

    Вложенность удобна человеку, который правит файл, а плоский ключ —
    коду, который ищет строку за одно обращение к словарю.

    :param data: Разобранный JSON.
    :param prefix: Накопленный префикс ключа.
    :yield: Пары «полный ключ, значение».
    """
    for key, value in data.items():
        full_key = f"{prefix}{KEY_SEPARATOR}{key}" if prefix else key
        if isinstance(value, dict) and PLURAL_KEY not in value:
            yield from _flatten(value, full_key)
        else:
            yield full_key, value


class TranslationManager:
    """Хранилище каталогов всех поддерживаемых языков.

    Экземпляр создаётся один раз на процесс: файлы читаются на старте, а
    дальше всё живёт в памяти. Читать JSON на каждое сообщение было бы
    расточительством, а перечитывать по изменению файла — источником
    гонок при выкатке.
    """

    def __init__(
        self,
        catalogs: Mapping[Language, Mapping[str, Any]],
        *,
        default: Language = Language.RU,
    ) -> None:
        if default not in catalogs:
            raise TranslationError(f"Каталог языка по умолчанию {default} не загружен.")
        self._catalogs: dict[Language, dict[str, Any]] = {
            language: dict(catalog) for language, catalog in catalogs.items()
        }
        self._default = default

    # ------------------------------------------------------------- загрузка
    @classmethod
    def from_directory(
        cls,
        directory: Path | str = DEFAULT_LOCALES_DIR,
        *,
        default: Language = Language.RU,
    ) -> TranslationManager:
        """Загружает каталоги из каталога с файлами ``<код языка>.json``.

        :param directory: Папка с файлами переводов.
        :param default: Язык, на который идёт откат при отсутствии строки.
        :return: Готовый менеджер.
        :raises TranslationError: Файл отсутствует, повреждён или пуст.
        """
        base = Path(directory)
        catalogs: dict[Language, dict[str, Any]] = {}

        for language in Language:
            path = base / f"{language.value}.json"
            if not path.is_file():
                raise TranslationError(f"Не найден файл перевода: {path}")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TranslationError(f"Не удалось прочитать {path}: {exc}") from exc
            if not isinstance(raw, dict) or not raw:
                raise TranslationError(f"Файл перевода {path} пуст или не является объектом.")
            catalogs[language] = dict(_flatten(raw))

        manager = cls(catalogs, default=default)
        manager.validate()
        logger.info(
            "Загружены переводы: %s (ключей: %d)",
            ", ".join(language.value for language in catalogs),
            len(catalogs[default]),
        )
        return manager

    def validate(self) -> None:
        """Сверяет каталоги между собой.

        Пропущенный ключ не ломает бота — он откатится на язык по
        умолчанию, — но означает, что часть пользователей увидит смесь
        языков. Такое нужно ловить тестом при сборке, а не отзывом
        пользователя, поэтому расхождения выписываются в лог полностью.
        """
        reference = set(self._catalogs[self._default])
        for language, catalog in self._catalogs.items():
            if language is self._default:
                continue
            missing = sorted(reference - set(catalog))
            extra = sorted(set(catalog) - reference)
            if missing:
                logger.warning(
                    "В каталоге %s нет %d ключей: %s",
                    language.value, len(missing), ", ".join(missing),
                )
            if extra:
                logger.warning(
                    "В каталоге %s лишние ключи: %s", language.value, ", ".join(extra)
                )

    # -------------------------------------------------------------- выборка
    @property
    def default_language(self) -> Language:
        """Язык, на который выполняется откат."""
        return self._default

    @property
    def languages(self) -> tuple[Language, ...]:
        """Загруженные языки."""
        return tuple(self._catalogs)

    def has(self, language: Language, key: str) -> bool:
        """Есть ли строка в каталоге указанного языка."""
        return key in self._catalogs.get(language, {})

    def get(self, language: Language, key: str, /, **params: Any) -> str:
        """Возвращает переведённую строку.

        Отсутствующий ключ не приводит к исключению: упасть посреди
        обработки апдейта из-за опечатки в ключе — худшее, что можно
        сделать с пользовательским опытом. Вместо этого строка ищется в
        языке по умолчанию, а в самом плохом случае возвращается сам ключ,
        и проблема остаётся видимой в интерфейсе и в логе.

        :param language: Требуемый язык.
        :param key: Ключ строки.
        :param params: Значения для подстановки.
        :return: Готовый текст.
        """
        template = self._lookup(language, key)
        if template is None:
            logger.error("Нет перевода для ключа %r ни в одном каталоге", key)
            return key

        if isinstance(template, dict):
            # Множественные формы требуют числа — без него выбрать нечего.
            logger.error("Ключ %r содержит формы числа, используйте plural()", key)
            return key

        try:
            return template.format(**params)
        except (KeyError, IndexError) as exc:
            # Шаблон и вызов разошлись: в тексте есть плейсхолдер, которому
            # не передали значения. Показываем текст как есть — он всё ещё
            # осмысленнее, чем сырой ключ или сообщение об ошибке.
            logger.error("Не хватает параметра %s для ключа %r", exc, key)
            return template

    def plural(self, language: Language, key: str, count: int, /, **params: Any) -> str:
        """Возвращает строку в форме, согласованной с числом.

        :param language: Требуемый язык.
        :param key: Ключ с формами числа.
        :param count: Число, определяющее форму.
        :param params: Дополнительные значения для подстановки.
        :return: Готовый текст с подставленным ``count``.
        """
        forms = self._lookup(language, key)
        if not isinstance(forms, dict) or PLURAL_KEY not in forms:
            logger.error("Ключ %r не содержит форм числа", key)
            return self.get(language, key, count=count, **params)

        variants: Mapping[str, str] = forms[PLURAL_KEY]
        form = select_plural_form(language, count)
        template = variants.get(form) or variants.get("other") or key
        try:
            return template.format(count=count, **params)
        except (KeyError, IndexError) as exc:
            logger.error("Не хватает параметра %s для ключа %r", exc, key)
            return template

    def _lookup(self, language: Language, key: str) -> Any | None:
        """Ищет значение в запрошенном языке, затем в языке по умолчанию."""
        catalog = self._catalogs.get(language)
        if catalog is not None and key in catalog:
            return catalog[key]

        if language is not self._default:
            fallback = self._catalogs[self._default]
            if key in fallback:
                logger.warning("Ключ %r отсутствует в каталоге %s", key, language.value)
                return fallback[key]

        return None


def select_plural_form(language: Language, count: int) -> str:
    """Выбирает имя формы множественного числа.

    Правила соответствуют CLDR: у русского и украинского три формы, у
    английского — две. Полноценная библиотека здесь избыточна: языков
    три, и правила для них стабильны десятилетиями.

    :param language: Язык.
    :param count: Число.
    :return: Имя формы: ``one``, ``few``, ``many`` или ``other``.
    """
    if language is Language.EN:
        return "one" if abs(count) == 1 else "other"

    number = abs(count)
    if number % 10 == 1 and number % 100 != 11:
        return "one"
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return "few"
    return "many"
