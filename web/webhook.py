"""Приёмник вебхуков CryptoBot.

Это единственный публично доступный вход в приложение, поэтому здесь
действуют три правила, которых нет в остальном коде.

**Подпись проверяется до всего остального.** Тело читается сырым и
сверяется с ``HMAC-SHA256``: без этого любой желающий начислил бы себе
подписку, отправив подходящий JSON.

**Ответ всегда быстрый и по возможности 200.** CryptoBot повторяет
доставку, пока не получит успешный ответ, поэтому 500 в ответ на
дубликат означал бы бесконечный цикл повторов. Успехом отвечаем и на
повторную доставку, и на события, которые нас не касаются, — оба случая
штатные.

**Вебхук работает вне диспетчера aiogram**, а значит без его middleware:
ни транзакции, ни языка пользователя здесь нет по умолчанию, и то и
другое приходится открывать самому.
"""

from __future__ import annotations

import json
from typing import Any, Final

from aiohttp import web

from core.config import CryptoBotConfig, Settings
from core.logger import get_logger
from db.uow import UnitOfWorkFactory
from services.billing import (
    SIGNATURE_HEADER,
    UPDATE_INVOICE_PAID,
    BillingError,
    BillingService,
    PaymentMismatchError,
    PaymentNotFoundError,
    parse_invoice,
    verify_signature,
)
from services.i18n import TranslationManager, Translator
from services.notifier import TelegramNotifier

logger = get_logger(__name__)

#: Ключи, под которыми зависимости лежат в приложении aiohttp.
CONFIG_KEY: Final[web.AppKey[CryptoBotConfig]] = web.AppKey("crypto_config")
SETTINGS_KEY: Final[web.AppKey[Settings]] = web.AppKey("settings")
UOW_KEY: Final[web.AppKey[UnitOfWorkFactory]] = web.AppKey("uow_factory")
NOTIFIER_KEY: Final[web.AppKey[TelegramNotifier]] = web.AppKey("notifier")
TRANSLATIONS_KEY: Final[web.AppKey[TranslationManager]] = web.AppKey("translations")

#: Предел размера тела: вебхук CryptoBot умещается в килобайты, а всё
#: большее — попытка занять память процесса.
MAX_BODY_BYTES: Final[int] = 64 * 1024


async def handle_cryptobot_webhook(request: web.Request) -> web.Response:
    """Принимает уведомление CryptoBot об оплате.

    :param request: HTTP-запрос.
    :return: Ответ, который CryptoBot считает подтверждением доставки.
    """
    config = request.app[CONFIG_KEY]

    if request.content_length and request.content_length > MAX_BODY_BYTES:
        logger.warning("Вебхук CryptoBot отклонён: тело %s байт", request.content_length)
        return web.json_response({"ok": False}, status=413)

    body = await request.read()
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if not verify_signature(config.token, body, signature):
        # Отдаём 401 и ничего не рассказываем о причине: подробности
        # помогли бы подбирать подпись.
        logger.warning(
            "Вебхук CryptoBot с неверной подписью от %s", request.remote or "неизвестно"
        )
        return web.json_response({"ok": False}, status=401)

    try:
        update: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Вебхук CryptoBot с некорректным JSON")
        return web.json_response({"ok": False}, status=400)

    update_type = str(update.get("update_type") or "")
    if update_type != UPDATE_INVOICE_PAID:
        # Событие не про оплату: подтверждаем доставку, чтобы его не
        # присылали снова, и ничего не делаем.
        logger.info("Вебхук CryptoBot: пропускаю событие %r", update_type)
        return web.json_response({"ok": True})

    payload = update.get("payload")
    if not isinstance(payload, dict):
        logger.error("Вебхук CryptoBot без объекта счёта: %r", update)
        return web.json_response({"ok": True})

    try:
        invoice = parse_invoice(payload)
    except BillingError as exc:
        logger.error("Вебхук CryptoBot: не удалось разобрать счёт: %s", exc)
        return web.json_response({"ok": True})

    await _apply_payment(request.app, invoice, payload)
    return web.json_response({"ok": True})


async def _apply_payment(app: web.Application, invoice: Any, raw: dict[str, Any]) -> None:
    """Начисляет подписку по оплаченному счёту и сообщает пользователю.

    Ошибки не выпускаются наружу: вебхуку уже отвечено успехом, а
    повторять доставку бессмысленно — при повторе результат будет тем же.
    Всё, что пошло не так, попадает в лог с идентификаторами, достаточными
    для ручного разбора.

    :param app: Приложение aiohttp с зависимостями.
    :param invoice: Разобранный счёт.
    :param raw: Сырое тело события для аудита.
    """
    uow_factory = app[UOW_KEY]
    notifier = app[NOTIFIER_KEY]
    settings = app[SETTINGS_KEY]
    translations = app[TRANSLATIONS_KEY]

    try:
        async with uow_factory() as uow:
            billing = BillingService(uow)
            outcome = await billing.apply_crypto_payment(
                invoice_payload=invoice.payload,
                external_id=str(invoice.invoice_id),
                amount=invoice.amount,
                asset=invoice.asset,
                raw_payload=raw,
            )
            payment = await uow.payments.get_by_id(outcome.payment_id)
            user = await uow.users.get_by_id(payment.user_id) if payment else None
            recipient = user.telegram_id if user else None
            language = user.language if user else translations.default_language
            await uow.commit()
    except PaymentNotFoundError:
        # Деньги переведены, а счёта нет — случай для ручного разбора.
        logger.error(
            "Криптооплата по неизвестному счёту: payload=%r invoice=%s",
            invoice.payload, invoice.invoice_id,
        )
        return
    except PaymentMismatchError as exc:
        logger.error(
            "Криптооплата не совпала со счётом %s: %s", invoice.invoice_id, exc
        )
        return
    except Exception:  # noqa: BLE001 - вебхуку уже отвечено, падать некуда
        logger.exception("Сбой обработки криптооплаты %s", invoice.invoice_id)
        return

    if outcome.already_processed:
        logger.info("Повторная доставка вебхука по счёту id=%s", outcome.payment_id)
        return

    if recipient is None:
        logger.error("Оплаченный счёт id=%s без пользователя", outcome.payment_id)
        return

    i18n = Translator(translations, language)
    expires = (
        outcome.expires_at.astimezone(settings.display_timezone).strftime("%d.%m.%Y %H:%M")
        if outcome.expires_at
        else "—"
    )
    await notifier.send(
        recipient,
        i18n("billing.paid", plan=outcome.plan.value, expires=expires),
    )


async def handle_health(request: web.Request) -> web.Response:
    """Проверка живости для оркестратора."""
    return web.json_response({"status": "ok"})


def build_web_app(
    *,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    notifier: TelegramNotifier,
    translations: TranslationManager,
) -> web.Application:
    """Собирает приложение aiohttp с приёмником вебхуков.

    :param settings: Настройки приложения.
    :param uow_factory: Фабрика единиц работы.
    :param notifier: Отправитель уведомлений.
    :param translations: Каталоги переводов.
    :return: Готовое приложение.
    """
    app = web.Application()
    app[CONFIG_KEY] = settings.crypto
    app[SETTINGS_KEY] = settings
    app[UOW_KEY] = uow_factory
    app[NOTIFIER_KEY] = notifier
    app[TRANSLATIONS_KEY] = translations

    app.router.add_post(settings.crypto.webhook_path, handle_cryptobot_webhook)
    app.router.add_get("/health", handle_health)
    return app


async def start_web_app(app: web.Application, config: CryptoBotConfig) -> web.AppRunner:
    """Запускает приёмник вебхуков.

    :param app: Приложение aiohttp.
    :param config: Параметры CryptoBot.
    :return: Запущенный runner — его нужно остановить при завершении.
    """
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=config.webhook_host, port=config.webhook_port)
    await site.start()
    logger.info(
        "Приёмник вебхуков слушает %s:%s%s",
        config.webhook_host, config.webhook_port, config.webhook_path,
    )
    return runner
