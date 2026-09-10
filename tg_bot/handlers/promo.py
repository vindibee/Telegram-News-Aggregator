"""Пользовательская часть привлечения: промокоды и реферальная ссылка.

Оба сценария заканчиваются начислением дней подписки, и оба должны быть
устойчивы к повторному нажатию — человек, не увидевший мгновенного ответа,
жмёт кнопку ещё раз. Однократность обеспечивают уникальные индексы в базе,
а не проверки здесь; хендлеру остаётся показать результат.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from core.config import Settings
from core.logger import get_logger
from db.models import User
from db.uow import UnitOfWork
from services.i18n import Translator
from services.promocodes import PromocodeService, PromoOutcome
from services.referrals import ReferralService
from tg_bot.callbacks import ACTION_PROMO, ACTION_REFERRAL, MenuCB
from tg_bot.flags import rate_limit
from tg_bot.keyboards import kb_cabinet_back
from tg_bot.utils import get_message

logger = get_logger(__name__)

router = Router(name="promo")

#: Ключи локализации для каждого исхода активации промокода.
_PROMO_MESSAGES: dict[PromoOutcome, str] = {
    PromoOutcome.INVALID: "promo.invalid",
    PromoOutcome.UNKNOWN: "promo.unknown",
    PromoOutcome.DISABLED: "promo.disabled",
    PromoOutcome.EXPIRED: "promo.expired",
    PromoOutcome.EXHAUSTED: "promo.exhausted",
    PromoOutcome.ALREADY_USED: "promo.already_used",
    PromoOutcome.CHECKOUT_ONLY: "promo.checkout_only",
}


class PromoSG(StatesGroup):
    """Ожидание ввода промокода."""

    code = State()


# --------------------------------------------------------------- промокоды
# Лимит строже общего: перебор кодов — это именно то, от чего защищает
# ограничение частоты, а честному человеку десяти попыток в час хватит.
@router.message(Command("promo"), **rate_limit(10, 3600, scope="promo"))
async def cmd_promo(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
) -> None:
    """Активирует промокод, переданный аргументом команды."""
    raw = (command.args or "").strip()
    if not raw:
        await state.set_state(PromoSG.code)
        await message.answer(i18n("promo.ask"))
        return

    await _apply(message, raw, user=user, uow=uow, i18n=i18n)


@router.callback_query(MenuCB.filter(F.action == ACTION_PROMO))
async def ask_promo(callback: CallbackQuery, state: FSMContext, i18n: Translator) -> None:
    """Запрашивает промокод по кнопке."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return

    await state.set_state(PromoSG.code)
    await message.answer(i18n("promo.ask"))


@router.message(Command("cancel"), PromoSG.code)
async def cancel_promo(message: Message, state: FSMContext, i18n: Translator) -> None:
    """Выходит из ввода промокода."""
    await state.clear()
    await message.answer(i18n("promo.cancelled"), reply_markup=kb_cabinet_back(i18n))


@router.message(PromoSG.code, **rate_limit(10, 3600, scope="promo"))
async def receive_promo(
    message: Message,
    state: FSMContext,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
) -> None:
    """Активирует промокод, введённый отдельным сообщением."""
    await state.clear()
    await _apply(message, message.text or "", user=user, uow=uow, i18n=i18n)


async def _apply(
    message: Message,
    raw_code: str,
    *,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
) -> None:
    """Применяет промокод и отвечает пользователю.

    :param message: Сообщение, на которое отвечаем.
    :param raw_code: Пользовательский ввод.
    :param user: Кто активирует.
    :param uow: Единица работы.
    :param i18n: Локализатор.
    """
    result = await PromocodeService(uow).activate(
        user=user, raw_code=raw_code, now=datetime.now(tz=timezone.utc)
    )

    if result.activated:
        await message.answer(
            i18n("promo.activated", days=i18n.plural("units.days", result.days)),
            reply_markup=kb_cabinet_back(i18n),
        )
        return

    key = _PROMO_MESSAGES.get(result.outcome, "promo.unknown")
    await message.answer(i18n(key), reply_markup=kb_cabinet_back(i18n))


# ---------------------------------------------------------------- рефералы
@router.message(Command("ref"))
async def cmd_referral(
    message: Message,
    bot: Bot,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Показывает реферальную ссылку и статистику приглашений."""
    await _show_referral(message, bot, user=user, uow=uow, settings=settings, i18n=i18n)


@router.callback_query(MenuCB.filter(F.action == ACTION_REFERRAL))
async def show_referral(
    callback: CallbackQuery,
    bot: Bot,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
) -> None:
    """То же по кнопке из кабинета."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return

    await _show_referral(message, bot, user=user, uow=uow, settings=settings, i18n=i18n)


async def _show_referral(
    message: Message,
    bot: Bot,
    *,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Собирает и отправляет экран реферальной программы."""
    service = ReferralService(uow, bonus_days=settings.admin.referral_bonus_days)

    me = await bot.get_me()
    link = service.build_link(me.username or "", user)
    stats = await uow.referrals.stats_for(user.id)

    text = "\n".join(
        [
            i18n("referral.header"),
            "",
            i18n(
                "referral.rules",
                days=i18n.plural("units.days", service.bonus_days),
            ),
            "",
            i18n("referral.link", link=escape(link)),
            "",
            i18n(
                "referral.stats",
                invited=stats.total,
                days=i18n.plural("units.days", stats.bonus_days),
            ),
        ]
    )
    await message.answer(
        text, reply_markup=kb_cabinet_back(i18n), disable_web_page_preview=True
    )


async def notify_referrer(
    bot: Bot,
    *,
    referrer: User,
    days: int,
    i18n: Translator,
) -> None:
    """Сообщает пригласившему о новом реферале.

    Сообщение уходит на языке получателя, а не того, кто перешёл по
    ссылке: у пригласившего свой интерфейс, и он не обязан совпадать.

    Ошибки гасятся: пригласивший мог заблокировать бота, и это не повод
    ронять обработку ``/start`` у нового пользователя.

    :param bot: Клиент Bot API.
    :param referrer: Кого уведомляем.
    :param days: Сколько суток ему начислено.
    :param i18n: Локализатор текущего запроса — язык берётся из него.
    """
    translator = i18n.switch(referrer.language)
    try:
        await bot.send_message(
            chat_id=referrer.telegram_id,
            text=translator(
                "referral.notify",
                days=translator.plural("units.days", days),
            ),
        )
    except TelegramAPIError as exc:
        logger.info("Не удалось уведомить реферера id=%s: %s", referrer.id, exc)


__all__ = ["notify_referrer", "router"]
