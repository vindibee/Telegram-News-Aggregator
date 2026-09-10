"""Хендлеры смены языка интерфейса.

Выбор сохраняется в двух местах и в строгом порядке: сначала база, потом
кэш. Обратный порядок оставил бы кэш «впереди» базы при сбое транзакции,
и пользователь получил бы язык, который на самом деле не сохранён.

Ответ на новом языке формируется сразу же: локализатор из контекста
относится к прежнему языку, поэтому для подтверждения создаётся новый
через :meth:`~services.i18n.Translator.switch`. Иначе человек нажимал бы
«English» и получал подтверждение по-русски.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from core.config import Settings
from core.logger import get_logger
from db.enums import Language
from db.models import User
from db.uow import UnitOfWork
from services.i18n import LanguageCache, Translator
from tg_bot.callbacks import ACTION_LANGUAGE, LanguageCB, MenuCB
from tg_bot.handlers.onboarding import show_about
from tg_bot.flags import rate_limit
from tg_bot.keyboards import kb_languages
from tg_bot.states import LanguageStates
from tg_bot.utils import get_message, safe_edit_text

logger = get_logger(__name__)

router = Router(name="language")


@router.message(Command("language"), **rate_limit(5, 60, scope="language"))
async def cmd_language(message: Message, user: User, i18n: Translator, state: FSMContext) -> None:
    """Показывает меню выбора языка командой."""
    await state.set_state(LanguageStates.choosing)
    await message.answer(_menu_text(user.language, i18n), reply_markup=kb_languages(user.language, i18n))


@router.callback_query(MenuCB.filter(F.action == ACTION_LANGUAGE))
async def show_languages(
    callback: CallbackQuery,
    user: User,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Показывает меню выбора языка по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    await state.set_state(LanguageStates.choosing)
    await safe_edit_text(
        target, _menu_text(user.language, i18n), kb_languages(user.language, i18n)
    )


@router.callback_query(LanguageCB.filter())
async def choose_language(
    callback: CallbackQuery,
    callback_data: LanguageCB,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    language_cache: LanguageCache,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Сохраняет выбранный язык и отвечает уже на нём."""
    try:
        language = Language(callback_data.code)
    except ValueError:
        # Кнопка из версии бота, где был другой набор языков.
        logger.info("Запрошен неизвестный язык %r пользователем %s", callback_data.code, user.id)
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    localized = i18n.switch(language)

    if user.apply_language(language):
        await uow.users.set_language(user.id, language)
        # Кэш обновляется после базы: транзакцию фиксирует middleware
        # зависимостей уже после выхода из хендлера, но записать в кэш
        # значение, которое не доедет до базы, мы не можем — при откате
        # исключение прервёт обработку раньше этой строки.
        await language_cache.set(user.telegram_id, language)

    await state.clear()
    await callback.answer(localized("language.changed_alert"))
    target = get_message(callback)
    if target is None:
        return

    # Сразу после выбора языка человек должен увидеть, зачем ему
    # этот бот, — уже на выбранном языке. Отправлять его в список
    # каналов на этом шаге значило бы показать инструмент раньше,
    # чем объяснено, что он делает.
    await show_about(target, localized, edit=True)


@router.message(LanguageStates.choosing, Command("cancel"))
async def cancel_language(message: Message, i18n: Translator, state: FSMContext) -> None:
    """Выходит из меню выбора языка, ничего не меняя."""
    await state.clear()
    await message.answer(i18n("language.cancelled"))


@router.message(LanguageStates.choosing)
async def remind_language_button(message: Message, i18n: Translator) -> None:
    """Отвечает на произвольный ввод в меню выбора языка."""
    await message.answer(i18n("language.need_button"))


def _menu_text(current: Language, i18n: Translator) -> str:
    """Собирает текст меню с названием текущего языка."""
    return i18n("language.menu", current=i18n(f"language.names.{current.value}"))
