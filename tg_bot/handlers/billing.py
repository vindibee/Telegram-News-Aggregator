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
from core.pricing import PLAN_OPTIONS, get_plan_option
from db.models import User
from db.uow import UnitOfWork
from services.billing import BillingError, BillingService, CryptoBotError
from services.i18n import Translator
from services.trial import TrialService
from tg_bot.callbacks import (
    ACTION_PLANS,
    ACTION_SUBSCRIPTION,
    PAY_CRYPTO,
    MenuCB,
    PayMethodCB,
    PlanCB,
)
from tg_bot.flags import critical, merge, rate_limit, skip_throttling
from tg_bot.keyboards import (
    kb_after_payment,
    kb_crypto_invoice,
    kb_pay_methods,
    kb_plans,
    kb_subscription,
)
from tg_bot.utils import get_message, safe_edit_text

logger = get_logger(__name__)

router = Router(name="billing")



@router.message(Command("premium"))
async def cmd_premium(message: Message, i18n: Translator) -> None:
    """Показывает список тарифов."""
    await message.answer(
        i18n("billing.plans_header"), reply_markup=kb_plans(PLAN_OPTIONS, i18n)
    )


@router.callback_query(MenuCB.filter(F.action == ACTION_PLANS))
async def show_plans(callback: CallbackQuery, i18n: Translator) -> None:
    """Показывает список тарифов по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return
    await safe_edit_text(
        target, i18n("billing.plans_header"), kb_plans(PLAN_OPTIONS, i18n)
    )


@router.message(Command("subscription"))
async def cmd_subscription(
    message: Message,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    trial: TrialService,
    i18n: Translator,
) -> None:
    """Показывает состояние подписки командой."""
    text, markup = await _subscription_screen(user, uow, settings, trial, i18n)
    await message.answer(text, reply_markup=markup)


@router.callback_query(MenuCB.filter(F.action == ACTION_SUBSCRIPTION))
async def show_subscription(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    trial: TrialService,
    i18n: Translator,
) -> None:
    """Показывает состояние подписки по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return
    text, markup = await _subscription_screen(user, uow, settings, trial, i18n)
    await safe_edit_text(target, text, markup)


# Выбор способа оплаты дешёвый: обращений к внешним сервисам нет,
# поэтому отдельного лимита ему не нужно.
@router.callback_query(PlanCB.filter())
async def choose_pay_method(
    callback: CallbackQuery,
    callback_data: PlanCB,
    billing: BillingService,
    i18n: Translator,
) -> None:
    """Предлагает выбрать, чем платить за выбранный тариф."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    option = get_plan_option(callback_data.option_id)
    if option is None:
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    await safe_edit_text(
        target,
        i18n("billing.method_header", title=escape(option.title)),
        kb_pay_methods(option, i18n, crypto_enabled=billing.crypto_enabled),
    )


# Выставление счёта обращается к внешнему API и создаёт строку в БД,
# поэтому лимит здесь строже общего для кнопок.
# Выставление счёта критично вдвойне: повтор создаёт второй счёт, а
# приходит он обычно уже после того, как хендлер отработал, — одной
# защиты от одновременного нажатия для этого мало.
@router.callback_query(
    PayMethodCB.filter(),
    **merge(rate_limit(5, 60, scope="invoice"), critical("invoice")),
)
async def send_invoice(
    callback: CallbackQuery,
    callback_data: PayMethodCB,
    user: User,
    billing: BillingService,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Выставляет счёт выбранным способом."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    if callback_data.method == PAY_CRYPTO:
        await _send_crypto_invoice(target, user, callback_data.option_id, billing, settings, i18n)
        return

    await _send_stars_invoice(target, user, callback_data.option_id, billing, i18n)


async def _send_stars_invoice(
    target: Message,
    user: User,
    option_id: str,
    billing: BillingService,
    i18n: Translator,
) -> None:
    """Отправляет счёт на оплату звёздами."""
    try:
        invoice = await billing.create_invoice(user, option_id)
    except BillingError as exc:
        logger.info("Отказ в выставлении счёта пользователю %s: %s", user.id, exc)
        await target.answer(i18n(exc.key))
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
        await target.answer(i18n("billing.invoice_failed"))


async def _send_crypto_invoice(
    target: Message,
    user: User,
    option_id: str,
    billing: BillingService,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Создаёт счёт в CryptoBot и присылает ссылку на оплату.

    Недоступность провайдера не должна выглядеть как поломка бота:
    пользователю предлагается заплатить звёздами, а подробности сбоя
    остаются в логе.
    """
    try:
        invoice = await billing.create_crypto_invoice(user, option_id)
    except BillingError as exc:
        logger.info("Отказ в криптосчёте пользователю %s: %s", user.id, exc)
        await target.answer(i18n(exc.key))
        return
    except CryptoBotError as exc:
        logger.error("CryptoBot недоступен при выставлении счёта пользователю %s: %s", user.id, exc)
        await target.answer(i18n("billing.crypto_unavailable"))
        return
    except RuntimeError:
        logger.error("Запрошена криптооплата при выключенном провайдере")
        await target.answer(i18n("billing.crypto_unavailable"))
        return

    ttl_minutes = settings.crypto.invoice_ttl_minutes
    await target.answer(
        i18n(
            "billing.crypto_invoice",
            title=escape(invoice.title),
            amount=invoice.amount,
            asset=escape(invoice.asset),
            ttl=i18n.plural("units.minutes", ttl_minutes),
        ),
        reply_markup=kb_crypto_invoice(invoice.pay_url, i18n),
    )


# Telegram ждёт ответ не дольше 10 секунд и отменяет платёж при опоздании,
# поэтому троттлинг здесь отключён: задержка дороже риска флуда.
@router.pre_checkout_query(**skip_throttling())
async def process_pre_checkout(
    query: PreCheckoutQuery,
    billing: BillingService,
    i18n: Translator,
) -> None:
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
            await query.answer(
                ok=False,
                error_message=i18n(decision.error_key or "billing.precheckout.declined"),
            )
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
    i18n: Translator,
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
            i18n(
                "billing.not_activated",
                charge_id=escape(payment.telegram_payment_charge_id),
            )
        )
        return

    if outcome.already_processed:
        await message.answer(
            i18n("billing.already_applied"), reply_markup=kb_after_payment(i18n)
        )
        return

    expires = (
        outcome.expires_at.astimezone(settings.display_timezone).strftime("%d.%m.%Y %H:%M")
        if outcome.expires_at
        else "—"
    )
    await message.answer(
        i18n(
            "billing.paid",
            plan=escape(outcome.plan.value),
            expires=escape(expires),
        ),
        reply_markup=kb_after_payment(i18n),
    )


async def _subscription_screen(
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    trial: TrialService,
    i18n: Translator,
) -> tuple[str, InlineKeyboardMarkup]:
    """Собирает экран подписки вместе с клавиатурой.

    Доступность триала спрашивается здесь, а не в клавиатуре: обращение к
    базе из функции сборки разметки спрятало бы запрос в неожиданном месте.

    :return: Пара «текст сообщения, клавиатура».
    """
    text, has_subscription = await _describe_subscription(user, uow, settings, i18n)
    eligibility = await trial.check_eligibility(user)
    markup = kb_subscription(
        has_subscription,
        i18n,
        trial_available=eligibility.available,
        trial_days=trial.days,
    )
    return text, markup


async def _describe_subscription(
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
) -> tuple[str, bool]:
    """Формирует описание текущей подписки.

    :return: Пара «текст сообщения, есть ли действующая подписка».
    """
    subscription = await uow.subscriptions.get_live(user.id)
    if subscription is None:
        return i18n("billing.no_subscription"), False

    now = datetime.now(tz=timezone.utc)
    expires = subscription.expires_at.astimezone(settings.display_timezone).strftime("%d.%m.%Y %H:%M")
    text = i18n(
        "billing.info",
        plan=escape(subscription.plan.value),
        status=escape(subscription.status.value),
        expires=escape(expires),
        left=i18n.plural("units.days", subscription.days_left(now)),
    )
    return text, True
