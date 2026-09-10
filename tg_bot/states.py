"""Состояния конечных автоматов диалога.

Состояния объявлены в одном месте, а не рядом с хендлерами: их имена
попадают в хранилище FSM (в продакшене — Redis) и переживают перезапуск
бота, поэтому переименование класса или поля обесценивает состояния всех
пользователей, застрявших в середине диалога. Держать такие имена на
виду безопаснее.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LanguageStates(StatesGroup):
    """Смена языка интерфейса."""

    #: Ждём нажатия на кнопку с языком.
    choosing = State()


class AddSourceChannelSG(StatesGroup):
    """Подключение канала-источника."""

    #: Ждём ссылку или имя канала.
    waiting_for_link = State()


class AddTargetChannelSG(StatesGroup):
    """Привязка канала для автопостинга."""

    #: Ждём имя канала или пересланное из него сообщение.
    waiting_for_channel = State()


class SetKeywordsSG(StatesGroup):
    """Настройка словесного фильтра."""

    #: Ждём триггерные слова через запятую.
    waiting_for_triggers = State()

    #: Ждём стоп-слова через запятую.
    waiting_for_stop_words = State()


class TrialStates(StatesGroup):
    """Активация пробного периода."""

    #: Ждём нажатия кнопки «Поделиться номером».
    waiting_for_contact = State()
