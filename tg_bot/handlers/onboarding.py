"""Знакомство с ботом: старт, выбор языка, главное меню и справка.

Раньше первым, что видел человек, был список каналов — экран, понятный
только тому, кто уже знает, зачем сюда пришёл. Теперь путь новичка разбит
на три коротких шага: кнопка «Начать», выбор языка и рассказ о том, что бот
умеет. Меню после этого состоит из функций, а не из каналов: канал — это
одна из возможностей, а не сам продукт.

Выбор языка стоит вторым шагом, до любого содержательного текста. Обратный
порядок означал бы, что первый и самый важный экран человек читает на
языке, который ему подставили по настройкам Telegram, а не выбрал сам.

Справка устроена как каталог: список функций, и по каждой — что это, как
пользоваться и зачем нужно. Плоский список команд остался, но ушёл на
отдельный экран: команды нужны тем, кто уже освоился, а новичку они не
говорят ничего.
"""

from __future__ import annotations

from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from core.logger import get_logger
from services.i18n import Translator
from tg_bot.callbacks import (
    ACTION_ABOUT,
    ACTION_COMMANDS,
    ACTION_HELP,
    ACTION_MENU,
    ACTION_START,
    HelpCB,
    MenuCB,
)
from tg_bot.keyboards import (
    kb_about,
    kb_choose_language_first,
    kb_help_hub,
    kb_help_topic,
    kb_main_menu,
    kb_start,
)
from tg_bot.states import LanguageStates
from tg_bot.utils import get_message, safe_edit_text

logger = get_logger(__name__)

router = Router(name="onboarding")

#: Разделы справки в порядке показа.
#:
#: Порядок повторяет путь освоения: сначала то, что работает сразу, затем
#: настройка под себя, и лишь потом платное и вспомогательное.
HELP_TOPICS: Final[tuple[str, ...]] = (
    "feed",
    "cabinet",
    "autopost",
    "keywords",
    "search",
    "stats",
    "subscription",
    "referral",
    "promo",
)


async def show_start(message: Message, i18n: Translator) -> None:
    """Показывает первый экран с единственной кнопкой.

    Текст здесь двуязычный: язык ещё не выбран, а подставленный из настроек
    Telegram может оказаться не тем, на котором человек хочет читать.
    """
    await message.answer(i18n("onboarding.start"), reply_markup=kb_start(i18n))


@router.callback_query(MenuCB.filter(F.action == ACTION_START))
async def begin(callback: CallbackQuery, i18n: Translator, state: FSMContext) -> None:
    """Переходит от кнопки «Начать» к выбору языка."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    await state.set_state(LanguageStates.choosing)
    await safe_edit_text(
        target, i18n("onboarding.choose_language"), kb_choose_language_first(i18n)
    )


async def show_about(message: Message, i18n: Translator, *, edit: bool = False) -> None:
    """Показывает рассказ о возможностях бота.

    :param message: Сообщение, в которое отвечать.
    :param i18n: Локализатор на языке пользователя.
    :param edit: Заменить текст существующего сообщения вместо отправки нового.
    """
    text = i18n("onboarding.about")
    markup = kb_about(i18n)

    if edit:
        await safe_edit_text(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(MenuCB.filter(F.action == ACTION_ABOUT))
async def about(callback: CallbackQuery, i18n: Translator) -> None:
    """Открывает рассказ о боте из меню."""
    await callback.answer()
    target = get_message(callback)
    if target is not None:
        await show_about(target, i18n, edit=True)


# ------------------------------------------------------------- главное меню
@router.message(Command("menu"))
async def cmd_menu(message: Message, i18n: Translator, state: FSMContext) -> None:
    """Открывает главное меню командой.

    Состояние сбрасывается: команда меню — обычный способ выйти из
    затянувшегося диалога, и требовать для этого отдельного /cancel
    значило бы ловить человека в сценарии, из которого он уже ушёл.
    """
    await state.clear()
    await message.answer(i18n("menu.title"), reply_markup=kb_main_menu(i18n))


@router.callback_query(MenuCB.filter(F.action == ACTION_MENU))
async def open_menu(callback: CallbackQuery, i18n: Translator, state: FSMContext) -> None:
    """Возвращает в главное меню по кнопке."""
    await callback.answer()
    await state.clear()
    target = get_message(callback)
    if target is not None:
        await safe_edit_text(target, i18n("menu.title"), kb_main_menu(i18n))


# ------------------------------------------------------------------ справка
@router.message(Command("help"))
async def cmd_help(message: Message, i18n: Translator) -> None:
    """Открывает каталог функций командой."""
    await message.answer(
        i18n("help.hub"), reply_markup=kb_help_hub(HELP_TOPICS, i18n)
    )


@router.callback_query(MenuCB.filter(F.action == ACTION_HELP))
async def help_hub(callback: CallbackQuery, i18n: Translator) -> None:
    """Показывает список функций, о которых можно почитать."""
    await callback.answer()
    target = get_message(callback)
    if target is not None:
        await safe_edit_text(target, i18n("help.hub"), kb_help_hub(HELP_TOPICS, i18n))


@router.callback_query(HelpCB.filter())
async def help_topic(
    callback: CallbackQuery,
    callback_data: HelpCB,
    i18n: Translator,
) -> None:
    """Объясняет одну функцию: что это, как пользоваться и зачем."""
    topic = callback_data.topic
    if topic not in HELP_TOPICS:
        # Кнопка из версии бота с другим набором разделов.
        logger.info("Запрошен неизвестный раздел справки %r", topic)
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    await callback.answer()
    target = get_message(callback)
    if target is not None:
        await safe_edit_text(
            target, i18n(f"help.topics.{topic}.body"), kb_help_topic(i18n)
        )


@router.callback_query(MenuCB.filter(F.action == ACTION_COMMANDS))
async def show_commands(callback: CallbackQuery, i18n: Translator) -> None:
    """Показывает плоский список команд."""
    await callback.answer()
    target = get_message(callback)
    if target is not None:
        await safe_edit_text(target, i18n("help.commands"), kb_help_topic(i18n))


__all__ = ["HELP_TOPICS", "router", "show_about", "show_start"]
