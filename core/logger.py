"""Единая точка настройки логирования приложения."""

from __future__ import annotations

import logging
import sys
from typing import Final

ROOT_LOGGER_NAME: Final[str] = "bot"

_LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

#: Корневой логгер приложения. Все модульные логгеры создаются как его потомки.
logger: Final[logging.Logger] = logging.getLogger(ROOT_LOGGER_NAME)


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Настраивает вывод логов в stdout (важно для корректного сбора логов Docker).

    Функция идемпотентна: повторный вызов не приводит к дублированию обработчиков
    и, как следствие, к дублированию строк в выводе.

    :param level: Уровень логирования (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    :return: Настроенный корневой логгер приложения.
    """
    resolved_level = logging.getLevelName(level.upper())
    if not isinstance(resolved_level, int):
        resolved_level = logging.INFO

    logger.setLevel(resolved_level)
    # Логи пишем только своим обработчиком, чтобы не дублировать записи через root.
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(handler)

    # Приглушаем избыточный шум сторонних библиотек.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Возвращает дочерний логгер приложения.

    :param name: Обычно ``__name__`` вызывающего модуля.
    :return: Логгер вида ``bot.<module>``.
    """
    suffix = name.rsplit(".", maxsplit=1)[-1]
    return logger.getChild(suffix)
