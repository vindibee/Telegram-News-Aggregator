"""Хендлеры пробного периода.

Слой переводит сценарий :class:`~services.trial.TrialService` на язык
Telegram: запрашивает номер штатной кнопкой, проверяет, что он
принадлежит отправителю, и показывает результат. Правила выдачи триала
живут в сервисе.

Диалог использует FSM, потому что между предложением и подтверждением
телефона проходит произвольное время, а пользователь тем временем может
уйти в другие разделы бота. Состояние хранится в Redis (см. ``main.py``),
поэтому перезапуск бота не обрывает начатый диалог.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Contact, Message

from core.config import Settings
from core.logger import get_logger
from db.models import User
from services.trial import ForeignContactError, TrialError, TrialOutcome, TrialService
from tg_bot.callbacks import ACTION_TRIAL, MenuCB
from tg_bot.flags import rate_limit
from tg_bot.keyboards import (
    kb_after_trial,
    kb_hide_contact_request,
    kb_request_contact,
    kb_to_channels,
    kb_trial_declined,
)
from tg_bot.states import TrialStates
from tg_bot.utils import get_message

logger = get_logger(__name__)

router = Router(name="trial")

_OFFER = (
    "🎁 <b>Пробный период на {days} дн.</b>\n\n"
    "Полный доступ ко всем каналам и функциям — бесплатно и без списаний.\n\n"
    "Чтобы исключить повторную активацию с нескольких аккаунтов, "
    "нужен номер телефона. Нажмите кнопку ниже — Telegram передаст его сам.\n\n"
    "Номер не сохраняется: в базу попадает только необратимый отпечаток.\n\n"
    "Передумали — отправьте /cancel."
)
_NEED_BUTTON = (
    "📱 Нужен именно номер из Telegram — введённый вручную текст подтвердить нельзя.\n\n"
    "Нажмите кнопку «Поделиться номером» или отправьте /cancel."
)
_CANCELLED = "Хорошо, пробный период не активирован."
_ACTIVATED_SHORT = "✅ Пробный период активирован."
_ACTIVATED = (
    "🎁 <b>Пробный период на {days} дн.</b>\n\n"
    "Тариф: <b>{plan}</b>\n"
    "Действует до: <b>{expires}</b>\n\n"
    "За сутки до окончания я напомню."
)


@router.message(Command("trial"), **rate_limit(3, 300, scope="trial"))
async def cmd_trial(
    message: Message,
    user: User,
    trial: TrialService,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Начинает выдачу пробного периода по команде."""
    await _offer_trial(message, user, trial, state, settings)


@router.callback_query(MenuCB.filter(F.action == ACTION_TRIAL), **rate_limit(3, 300, scope="trial"))
async def start_trial(
    callback: CallbackQuery,
    user: User,
    trial: TrialService,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Начинает выдачу пробного периода по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return
    await _offer_trial(target, user, trial, state, settings)


@router.message(TrialStates.waiting_for_contact, Command("cancel"))
async def cancel_trial(message: Message, state: FSMContext) -> None:
    """Прерывает диалог подтверждения телефона."""
    await state.clear()
    # Сначала снимаем клавиатуру запроса номера: инлайн-кнопки и
    # reply-клавиатуру нельзя приложить к одному сообщению.
    await message.answer(_CANCELLED, reply_markup=kb_hide_contact_request())
    await message.answer("Подписку можно оформить в любой момент.", reply_markup=kb_trial_declined())


@router.message(TrialStates.waiting_for_contact, F.contact)
async def process_contact(
    message: Message,
    user: User,
    trial: TrialService,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Проверяет присланный контакт и активирует пробный период."""
    contact = message.contact
    if contact is None:  # pragma: no cover - защищено фильтром
        return

    try:
        phone = _own_phone(contact, message)
        outcome = await trial.activate(user, phone=phone)
    except TrialError as exc:
        logger.info("Отказ в пробном периоде для user_id=%s: %s", user.id, exc)
        await state.clear()
        await message.answer(f"❌ {exc}", reply_markup=kb_hide_contact_request())
        await message.answer("Доступ можно получить по подписке.", reply_markup=kb_trial_declined())
        return

    await state.clear()
    await message.answer(_ACTIVATED_SHORT, reply_markup=kb_hide_contact_request())
    await message.answer(
        _describe_outcome(outcome, settings),
        reply_markup=kb_after_trial(),
    )


@router.message(TrialStates.waiting_for_contact)
async def remind_contact(message: Message) -> None:
    """Отвечает на любой другой ввод, не выходя из состояния."""
    await message.answer(_NEED_BUTTON, reply_markup=kb_request_contact())


async def _offer_trial(
    message: Message,
    user: User,
    trial: TrialService,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Проверяет доступность триала и запрашивает телефон.

    :param message: Сообщение, в которое отвечать.
    :param user: Пользователь.
    :param trial: Сервис пробного периода.
    :param state: Контекст FSM.
    :param settings: Настройки приложения.
    """
    eligibility = await trial.check_eligibility(user)
    if eligibility.blocked:
        await state.clear()
        await message.answer(
            f"ℹ️ {escape(eligibility.reason or 'Пробный период недоступен.')}",
            reply_markup=kb_to_channels(),
        )
        return

    if not trial.requires_contact:
        # Подтверждение отключено настройкой: спрашивать нечего, выдаём сразу.
        try:
            outcome = await trial.activate(user)
        except TrialError as exc:
            logger.info("Отказ в пробном периоде для user_id=%s: %s", user.id, exc)
            await message.answer(f"❌ {exc}", reply_markup=kb_trial_declined())
            return

        await state.clear()
        await message.answer(_describe_outcome(outcome, settings), reply_markup=kb_after_trial())
        return

    await state.set_state(TrialStates.waiting_for_contact)
    await message.answer(
        _OFFER.format(days=trial.days),
        reply_markup=kb_request_contact(),
    )


def _own_phone(contact: Contact, message: Message) -> str:
    """Возвращает номер отправителя, отвергая чужие контакты.

    Кнопка «Поделиться номером» присылает контакт, у которого ``user_id``
    равен идентификатору отправителя. Контакт, выбранный из адресной
    книги, приходит либо с чужим ``user_id``, либо вовсе без него — и
    именно так обходилась бы защита от мультиаккаунтов, будь проверка
    только по самому номеру.

    :param contact: Присланный контакт.
    :param message: Сообщение с контактом.
    :return: Номер телефона отправителя.
    :raises ForeignContactError: Контакт принадлежит другому человеку.
    """
    author = message.from_user
    if author is None or contact.user_id is None or contact.user_id != author.id:
        logger.warning(
            "Попытка подтвердить триал чужим контактом: from_user=%s, contact.user_id=%s",
            author.id if author else None,
            contact.user_id,
        )
        raise ForeignContactError

    if not contact.phone_number:
        raise ForeignContactError

    return contact.phone_number


def _describe_outcome(outcome: TrialOutcome, settings: Settings) -> str:
    """Формирует сообщение об активированном пробном периоде."""
    expires: datetime = outcome.expires_at.astimezone(settings.display_timezone)
    return _ACTIVATED.format(
        days=outcome.days_granted,
        plan=escape(outcome.plan.value),
        expires=escape(expires.strftime("%d.%m.%Y %H:%M")),
    )
