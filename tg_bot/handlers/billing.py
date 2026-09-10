"""Хендлеры оплаты подписки звёздами Telegram.

Слой отвечает только за перевод между Bot API и сервисом биллинга: разбор
события, вызов сценария, показ результата. Правила оплаты и начисления
живут в :mod:`services.billing`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery

from core.config import Settings
from core.logger import get_logger
from core.pricing import PLAN_OPTIONS
from db.models import User
from db.uow import UnitOfWork
from services.billing import BillingError, BillingService
from services.trial import TrialService
from tg_bot.callbacks import ACTION_PLANS, ACTION_SUBSCRIPTION, MenuCB, PlanCB
from tg_bot.flags import rate_limit, skip_throttling
from tg_bot.keyboards import kb_after_payment, kb_plans, kb_subscription
from tg_bot.utils import get_message, safe_edit_text

logger = get_logger(__name__)

router = Router(name="billing")

_PLANS_HEADER = (
    "⭐ <b>Подписка</b>\n\n"
    "Оплата проходит звёздами Telegram — без карт и внешних платёжных систем.\n\n"
    "Выберите тариф:"
)
_NO_SUBSCRIPTION = (
    "💳 <b>Подписка не оформлена</b>\n\n"
    "Оформите подписку, чтобы пользоваться агрегатором без ограничений."
)
_PAYMENT_FAILED = "❌ Не удалось оформить счёт. Попробуйте позже."
_ALREADY_APPLIED = "ℹ️ Этот платёж уже был учтён ранее."


@router.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    """Показывает список тарифов."""
    await message.answer(_PLANS_HEADER, reply_markup=kb_plans(PLAN_OPTIONS))


@router.callback_query(MenuCB.filter(F.action == ACTION_PLANS))
async def show_plans(callback: CallbackQuery) -> None:
    """Показывает список тарифов по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return
    await safe_edit_text(target, _PLANS_HEADER, kb_plans(PLAN_OPTIONS))


@router.message(Command("subscription"))
async def cmd_subscription(
    message: Message,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    trial: TrialService,
) -> None:
    """Показывает состояние подписки командой."""
    text, markup = await _subscription_screen(user, uow, settings, trial)
    await message.answer(text, reply_markup=markup)


@router.callback_query(MenuCB.filter(F.action == ACTION_SUBSCRIPTION))
async def show_subscription(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    trial: TrialService,
) -> None:
    """Показывает состояние подписки по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return
    text, markup = await _subscription_screen(user, uow, settings, trial)
    await safe_edit_text(target, text, markup)


# Выставление счёта обращается к Bot API и создаёт строку в БД, поэтому
# лимит здесь строже общего для кнопок.
@router.callback_query(PlanCB.filter(), **rate_limit(5, 60, scope="invoice"))
async def send_invoice(
    callback: CallbackQuery,
    callback_data: PlanCB,
    user: User,
    billing: BillingService,
) -> None:
    """Выставляет счёт на выбранный тариф."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    try:
        invoice = await billing.create_invoice(user, callback_data.option_id)
    except BillingError as exc:
        logger.info("Отказ в выставлении счёта пользователю %s: %s", user.id, exc)
        await target.answer(f"❌ {exc}")
        return

    try:
        await target.answer_invoice(
            title=invoice.title,
            description=invoice.description,
            payload=invoice.payload,
            currency=invoice.currency,
            prices=[LabeledPrice(label=invoice.label, amount=invoice.amount)],
            # provider_token для Telegram Stars не используется: оплата идёт
            # внутри Telegram, без внешнего эквайринга.
        )
    except TelegramAPIError as exc:
        logger.error("Не удалось отправить счёт id=%s: %s", invoice.payment_id, exc)
        await target.answer(_PAYMENT_FAILED)


# Telegram ждёт ответ не дольше 10 секунд и отменяет платёж при опоздании,
# поэтому троттлинг здесь отключён: задержка дороже риска флуда.
@router.pre_checkout_query(**skip_throttling())
async def process_pre_checkout(query: PreCheckoutQuery, billing: BillingService) -> None:
    """Подтверждает или отклоняет оплату до списания средств."""
    decision = await billing.validate_pre_checkout(
        telegram_id=query.from_user.id,
        payload=query.invoice_payload,
        total_amount=query.total_amount,
        currency=query.currency,
    )

    try:
        if decision.ok:
            await query.answer(ok=True)
        else:
            await query.answer(ok=False, error_message=decision.error_message or "Оплата отклонена.")
    except TelegramAPIError as exc:
        # Ответить не удалось — Telegram отменит платёж сам. Записываем всё
        # необходимое для ручного разбора.
        logger.error(
            "Не удалось ответить на PreCheckoutQuery %s (payload=%r): %s",
            query.id, query.invoice_payload, exc,
        )


@router.message(F.successful_payment, **skip_throttling())
async def process_successful_payment(
    message: Message,
    billing: BillingService,
    settings: Settings,
) -> None:
    """Учитывает оплату и активирует подписку."""
    payment = message.successful_payment
    if payment is None:  # pragma: no cover - защищено фильтром
        return

    # Плательщик определяется по автору сообщения, а не по чату: в личной
    # переписке они совпадают, но полагаться на это совпадение не стоит.
    payer = message.from_user
    if payer is None:
        logger.error(
            "Оплата без автора сообщения: charge_id=%s", payment.telegram_payment_charge_id
        )
        return

    try:
        outcome = await billing.apply_successful_payment(
            telegram_id=payer.id,
            payload=payment.invoice_payload,
            charge_id=payment.telegram_payment_charge_id,
            total_amount=payment.total_amount,
            currency=payment.currency,
            raw_payload=payment.model_dump(mode="json"),
        )
    except BillingError as exc:
        # Деньги уже списаны, поэтому пользователю нельзя просто отказать:
        # сообщаем о проблеме и оставляем след в логах для возврата.
        logger.error(
            "Оплата не учтена: charge_id=%s payload=%r: %s",
            payment.telegram_payment_charge_id, payment.invoice_payload, exc,
        )
        await message.answer(
            "⚠️ Оплата прошла, но активировать подписку не удалось.\n"
            f"Сообщите в поддержку код операции: <code>{escape(payment.telegram_payment_charge_id)}</code>"
        )
        return

    if outcome.already_processed:
        await message.answer(_ALREADY_APPLIED, reply_markup=kb_after_payment())
        return

    expires = (
        outcome.expires_at.astimezone(settings.display_timezone).strftime("%d.%m.%Y %H:%M")
        if outcome.expires_at
        else "—"
    )
    await message.answer(
        "✅ <b>Оплата получена, спасибо!</b>\n\n"
        f"Тариф: <b>{escape(outcome.plan.value)}</b>\n"
        f"Подписка активна до: <b>{escape(expires)}</b>",
        reply_markup=kb_after_payment(),
    )


async def _subscription_screen(
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    trial: TrialService,
) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает экран подписки вместе с клавиатурой.

    Доступность триала спрашивается здесь, а не в клавиатуре: обращение к
    базе из функции сборки разметки спрятало бы запрос в неожиданном месте.

    :return: Пара «текст сообщения, клавиатура».
    """
    text, has_subscription = await _describe_subscription(user, uow, settings)
    eligibility = await trial.check_eligibility(user)
    markup = kb_subscription(
        has_subscription,
        trial_available=eligibility.available,
        trial_days=trial.days,
    )
    return text, markup


async def _describe_subscription(
    user: User,
    uow: UnitOfWork,
    settings: Settings,
) -> tuple[str, bool]:
    """Формирует описание текущей подписки.

    :return: Пара «текст сообщения, есть ли действующая подписка».
    """
    subscription = await uow.subscriptions.get_live(user.id)
    if subscription is None:
        return _NO_SUBSCRIPTION, False

    now = datetime.now(tz=timezone.utc)
    expires = subscription.expires_at.astimezone(settings.display_timezone).strftime("%d.%m.%Y %H:%M")
    text = (
        "💳 <b>Ваша подписка</b>\n\n"
        f"Тариф: <b>{escape(subscription.plan.value)}</b>\n"
        f"Статус: <b>{escape(subscription.status.value)}</b>\n"
        f"Действует до: <b>{escape(expires)}</b>\n"
        f"Осталось дней: <b>{subscription.days_left(now)}</b>"
    )
    return text, True
