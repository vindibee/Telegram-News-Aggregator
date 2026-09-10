"""Тесты криптооплаты: подпись вебхука, клиент API и идемпотентность."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from db.enums import PaymentProvider, PaymentStatus, SubscriptionStatus
from db.models import User
from services.billing import (
    SIGNATURE_HEADER,
    BillingService,
    CryptoBotClient,
    CryptoBotRejectedError,
    CryptoBotUnavailableError,
    PaymentMismatchError,
    PaymentNotFoundError,
    verify_signature,
)
from tests.conftest import build_payment

TOKEN = "test-app-token"


def sign(body: bytes, token: str = TOKEN) -> str:
    """Подписывает тело так же, как это делает CryptoBot."""
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Подпись вебхука
# --------------------------------------------------------------------------- #


def test_valid_signature_is_accepted() -> None:
    body = b'{"update_type":"invoice_paid"}'

    assert verify_signature(TOKEN, body, sign(body)) is True


def test_signature_of_another_token_is_rejected() -> None:
    # Именно этим отличается «подписал CryptoBot» от «подписал кто угодно».
    body = b'{"update_type":"invoice_paid"}'

    assert verify_signature(TOKEN, body, sign(body, "чужой-токен")) is False


def test_tampered_body_invalidates_signature() -> None:
    body = b'{"amount":"2.50"}'
    signature = sign(body)

    assert verify_signature(TOKEN, b'{"amount":"250.00"}', signature) is False, (
        "Подмена суммы обязана ломать подпись"
    )


@pytest.mark.parametrize(
    ("token", "signature"),
    [("", "abc"), (TOKEN, ""), (TOKEN, "не-подпись"), (TOKEN, "a" * 64)],
)
def test_missing_or_garbage_signature_is_rejected(token: str, signature: str) -> None:
    assert verify_signature(token, b"{}", signature) is False


def test_signature_comparison_tolerates_surrounding_whitespace() -> None:
    body = b"{}"

    assert verify_signature(TOKEN, body, f"  {sign(body)}  ") is True


# --------------------------------------------------------------------------- #
# Клиент API
# --------------------------------------------------------------------------- #


async def _client_against(handler, path: str = "/api/createInvoice") -> tuple[CryptoBotClient, Any]:
    """Поднимает игрушечный сервер и клиент, направленный на него."""
    app = web.Application()
    app.router.add_post(path, handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = runner.addresses[0][1]

    session = aiohttp.ClientSession()
    client = CryptoBotClient(session, TOKEN, api_url=f"http://127.0.0.1:{port}/api", timeout=2.0)
    return client, (runner, session)


async def _close(resources) -> None:
    runner, session = resources
    await session.close()
    await runner.cleanup()


async def test_create_invoice_parses_response() -> None:
    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["amount"] == "2.50", "Сумма должна уходить строкой, а не числом"
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "invoice_id": 777,
                    "status": "active",
                    "asset": "USDT",
                    "amount": "2.50",
                    "bot_invoice_url": "https://t.me/CryptoBot?start=x",
                    "payload": "inv_abc",
                },
            }
        )

    client, resources = await _client_against(handler)
    try:
        invoice = await client.create_invoice(
            asset="USDT", amount=Decimal("2.50"), payload="inv_abc", description="Pro"
        )
    finally:
        await _close(resources)

    assert invoice.invoice_id == 777
    assert invoice.amount == Decimal("2.50")
    assert invoice.pay_url.startswith("https://t.me/CryptoBot")


async def test_api_error_is_not_retried_and_raises_rejected() -> None:
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.json_response({"ok": False, "error": {"code": 400}}, status=400)

    client, resources = await _client_against(handler)
    try:
        with pytest.raises(CryptoBotRejectedError):
            await client.create_invoice(
                asset="USDT", amount=Decimal("2.50"), payload="x", description="Pro"
            )
    finally:
        await _close(resources)

    assert calls == 1, "Отказ 4xx повторять бессмысленно: второй такой же запрос отвергнут так же"


async def test_server_error_is_retried_then_reported_as_unavailable() -> None:
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.json_response({"ok": False}, status=503)

    client, resources = await _client_against(handler)
    try:
        with pytest.raises(CryptoBotUnavailableError):
            await client.create_invoice(
                asset="USDT", amount=Decimal("2.50"), payload="x", description="Pro"
            )
    finally:
        await _close(resources)

    assert calls == 3, f"Сбой на их стороне должен повторяться, попыток: {calls}"


async def test_transient_failure_recovers_on_retry() -> None:
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response({"ok": False}, status=502)
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "invoice_id": 1,
                    "status": "active",
                    "asset": "USDT",
                    "amount": "2.50",
                    "bot_invoice_url": "https://t.me/CryptoBot?start=y",
                    "payload": "inv_x",
                },
            }
        )

    client, resources = await _client_against(handler)
    try:
        invoice = await client.create_invoice(
            asset="USDT", amount=Decimal("2.50"), payload="inv_x", description="Pro"
        )
    finally:
        await _close(resources)

    assert invoice.invoice_id == 1
    assert calls == 2, "Временный сбой должен переживаться повтором"


async def test_non_json_response_is_reported_as_rejected() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html>502 Bad Gateway</html>", status=200)

    client, resources = await _client_against(handler)
    try:
        with pytest.raises(CryptoBotRejectedError):
            await client.create_invoice(
                asset="USDT", amount=Decimal("2.50"), payload="x", description="Pro"
            )
    finally:
        await _close(resources)


def test_client_requires_token() -> None:
    with pytest.raises(ValueError):
        CryptoBotClient(AsyncMock(), "")


# --------------------------------------------------------------------------- #
# Начисление подписки по криптооплате
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_crypto_payment_grants_subscription(uow, user: User, db_session) -> None:
    payment = build_payment(
        user.id,
        provider=PaymentProvider.CRYPTO_BOT,
        invoice_id="inv_crypto_1",
        idempotency_key="crypto:1",
        amount=Decimal("2.50"),
        currency="USDT",
    )
    db_session.add(payment)
    await db_session.commit()

    billing = BillingService(uow)
    outcome = await billing.apply_crypto_payment(
        invoice_payload="inv_crypto_1",
        external_id="777",
        amount=Decimal("2.50"),
        asset="USDT",
    )

    assert outcome.newly_applied is True
    assert outcome.days_granted == 30

    subscription = await uow.subscriptions.get_live(user.id)
    assert subscription is not None
    assert subscription.status is SubscriptionStatus.ACTIVE


@pytest.mark.db
async def test_repeated_webhook_does_not_grant_twice(uow, user: User, db_session) -> None:
    # CryptoBot повторяет доставку, пока не получит 200, поэтому дубль —
    # штатная ситуация, а не сбой.
    payment = build_payment(
        user.id,
        provider=PaymentProvider.CRYPTO_BOT,
        invoice_id="inv_crypto_2",
        idempotency_key="crypto:2",
        amount=Decimal("2.50"),
        currency="USDT",
    )
    db_session.add(payment)
    await db_session.commit()

    billing = BillingService(uow)
    first = await billing.apply_crypto_payment(
        invoice_payload="inv_crypto_2", external_id="778", amount=Decimal("2.50"), asset="USDT"
    )
    second = await billing.apply_crypto_payment(
        invoice_payload="inv_crypto_2", external_id="778", amount=Decimal("2.50"), asset="USDT"
    )

    assert first.newly_applied is True
    assert second.already_processed is True, "Повторный вебхук не должен начислять дни второй раз"
    assert second.days_granted == 0

    history = await uow.subscriptions.list_history(user.id)
    assert len(history) == 1, "Подписка должна остаться одна"


@pytest.mark.db
async def test_underpayment_is_rejected(uow, user: User, db_session) -> None:
    # Сумма приходит из вебхука и лишь сообщает о факте перевода:
    # недоплата не должна открывать подписку.
    payment = build_payment(
        user.id,
        provider=PaymentProvider.CRYPTO_BOT,
        invoice_id="inv_crypto_3",
        idempotency_key="crypto:3",
        amount=Decimal("2.50"),
        currency="USDT",
    )
    db_session.add(payment)
    await db_session.commit()

    billing = BillingService(uow)
    with pytest.raises(PaymentMismatchError):
        await billing.apply_crypto_payment(
            invoice_payload="inv_crypto_3",
            external_id="779",
            amount=Decimal("1.00"),
            asset="USDT",
        )


@pytest.mark.db
async def test_foreign_asset_is_rejected(uow, user: User, db_session) -> None:
    payment = build_payment(
        user.id,
        provider=PaymentProvider.CRYPTO_BOT,
        invoice_id="inv_crypto_4",
        idempotency_key="crypto:4",
        amount=Decimal("2.50"),
        currency="USDT",
    )
    db_session.add(payment)
    await db_session.commit()

    billing = BillingService(uow)
    with pytest.raises(PaymentMismatchError):
        await billing.apply_crypto_payment(
            invoice_payload="inv_crypto_4", external_id="780", amount=Decimal("2.50"), asset="TON"
        )


@pytest.mark.db
async def test_unknown_invoice_raises_not_found(uow, user: User) -> None:
    billing = BillingService(uow)

    with pytest.raises(PaymentNotFoundError):
        await billing.apply_crypto_payment(
            invoice_payload="нет-такого", external_id="781", amount=Decimal("2.50"), asset="USDT"
        )


@pytest.mark.db
async def test_overpayment_is_accepted(uow, user: User, db_session) -> None:
    # Переплата — не повод отказать: деньги уже переведены, и вернуть их
    # сложнее, чем открыть оплаченный доступ.
    payment = build_payment(
        user.id,
        provider=PaymentProvider.CRYPTO_BOT,
        invoice_id="inv_crypto_5",
        idempotency_key="crypto:5",
        amount=Decimal("2.50"),
        currency="USDT",
    )
    db_session.add(payment)
    await db_session.commit()

    billing = BillingService(uow)
    outcome = await billing.apply_crypto_payment(
        invoice_payload="inv_crypto_5", external_id="782", amount=Decimal("3.00"), asset="USDT"
    )

    assert outcome.newly_applied is True


@pytest.mark.db
async def test_payment_is_marked_succeeded_with_external_id(uow, user: User, db_session) -> None:
    payment = build_payment(
        user.id,
        provider=PaymentProvider.CRYPTO_BOT,
        invoice_id="inv_crypto_6",
        idempotency_key="crypto:6",
        amount=Decimal("2.50"),
        currency="USDT",
    )
    db_session.add(payment)
    await db_session.commit()

    billing = BillingService(uow)
    await billing.apply_crypto_payment(
        invoice_payload="inv_crypto_6", external_id="783", amount=Decimal("2.50"), asset="USDT"
    )

    stored = await uow.payments.get_by_invoice_id(PaymentProvider.CRYPTO_BOT, "inv_crypto_6")
    assert stored is not None
    assert stored.status is PaymentStatus.SUCCEEDED
    assert stored.external_id == "783", "Идентификатор провайдера обязан сохраняться"
    assert stored.paid_at is not None
