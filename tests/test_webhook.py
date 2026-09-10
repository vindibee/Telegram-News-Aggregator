"""Тесты приёмника вебхуков CryptoBot."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.config import Settings
from db.enums import PaymentProvider, PaymentStatus, SubscriptionStatus
from db.models import User
from services.billing import SIGNATURE_HEADER
from services.i18n import TranslationManager
from tests.conftest import build_payment
from tests.test_crypto import TOKEN, sign
from web import build_web_app

pytestmark = pytest.mark.db


def _paid_update(payload: str, *, invoice_id: int = 900, amount: str = "2.50") -> dict:
    """Строит событие об оплате в формате CryptoBot."""
    return {
        "update_id": 1,
        "update_type": "invoice_paid",
        "payload": {
            "invoice_id": invoice_id,
            "status": "paid",
            "asset": "USDT",
            "amount": amount,
            "payload": payload,
            "bot_invoice_url": "https://t.me/CryptoBot?start=x",
        },
    }


@pytest.fixture
def notifier() -> AsyncMock:
    """Нотификатор без сети."""
    from services.notifier import DeliveryResult, DeliveryStatus, TelegramNotifier

    mock = AsyncMock(spec=TelegramNotifier)
    mock.send.return_value = DeliveryResult(1, DeliveryStatus.DELIVERED)
    return mock


@pytest.fixture
def crypto_settings(settings: Settings) -> Settings:
    """Настройки с включённой криптооплатой."""
    from dataclasses import replace

    return replace(settings, crypto=replace(settings.crypto, token=TOKEN))


@pytest.fixture
async def client(
    crypto_settings: Settings,
    uow_factory,
    notifier: AsyncMock,
) -> TestClient:
    """HTTP-клиент к приёмнику вебхуков."""
    app = build_web_app(
        settings=crypto_settings,
        uow_factory=uow_factory,
        notifier=notifier,
        translations=TranslationManager.from_directory(),
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


async def _post(client: TestClient, update: dict, *, signature: str | None = None):
    """Отправляет событие с подписью."""
    body = json.dumps(update).encode("utf-8")
    headers = {SIGNATURE_HEADER: signature if signature is not None else sign(body)}
    return await client.post("/webhook/cryptobot", data=body, headers=headers)


async def _seed_payment(db_session, user: User, invoice_id: str) -> None:
    """Создаёт неоплаченный криптосчёт."""
    db_session.add(
        build_payment(
            user.id,
            provider=PaymentProvider.CRYPTO_BOT,
            invoice_id=invoice_id,
            idempotency_key=f"crypto:{invoice_id}",
            amount=Decimal("2.50"),
            currency="USDT",
        )
    )
    await db_session.commit()


# --------------------------------------------------------------------------- #
# Подлинность
# --------------------------------------------------------------------------- #


async def test_wrong_signature_is_rejected_with_401(client: TestClient, user: User, db_session) -> None:
    # Вебхук — единственный публичный вход: без проверки подписи любой
    # начислил бы себе подписку, отправив подходящий JSON.
    await _seed_payment(db_session, user, "inv_hook_1")

    response = await _post(client, _paid_update("inv_hook_1"), signature="deadbeef")

    assert response.status == 401
    assert (await response.json())["ok"] is False


async def test_missing_signature_is_rejected(client: TestClient) -> None:
    response = await client.post("/webhook/cryptobot", data=b"{}")

    assert response.status == 401


async def test_tampered_amount_breaks_signature(client: TestClient, user: User, db_session) -> None:
    await _seed_payment(db_session, user, "inv_hook_2")
    update = _paid_update("inv_hook_2")
    body = json.dumps(update).encode("utf-8")
    signature = sign(body)

    update["payload"]["amount"] = "0.01"
    tampered = json.dumps(update).encode("utf-8")
    response = await client.post(
        "/webhook/cryptobot", data=tampered, headers={SIGNATURE_HEADER: signature}
    )

    assert response.status == 401, "Изменённое тело не должно проходить проверку"


async def test_non_ascii_signature_does_not_crash_the_endpoint(client: TestClient) -> None:
    # Заголовок приходит извне: исключение здесь означало бы 500 и
    # бесконечные повторы доставки со стороны CryptoBot.
    response = await _post(client, _paid_update("x"), signature="подпись")

    assert response.status == 401


# --------------------------------------------------------------------------- #
# Начисление
# --------------------------------------------------------------------------- #


async def test_paid_invoice_grants_subscription_and_notifies(
    client: TestClient,
    user: User,
    db_session,
    uow_factory,
    notifier: AsyncMock,
) -> None:
    await _seed_payment(db_session, user, "inv_hook_3")

    response = await _post(client, _paid_update("inv_hook_3"))

    assert response.status == 200
    async with uow_factory() as uow:
        subscription = await uow.subscriptions.get_live(user.id)
        payment = await uow.payments.get_by_invoice_id(PaymentProvider.CRYPTO_BOT, "inv_hook_3")

    assert subscription is not None and subscription.status is SubscriptionStatus.ACTIVE
    assert payment is not None and payment.status is PaymentStatus.SUCCEEDED
    notifier.send.assert_awaited_once()


async def test_repeated_delivery_is_answered_200_without_second_grant(
    client: TestClient,
    user: User,
    db_session,
    uow_factory,
    notifier: AsyncMock,
) -> None:
    # CryptoBot повторяет доставку, пока не получит 200. Ответить ошибкой
    # на дубликат — обречь себя на бесконечные повторы.
    await _seed_payment(db_session, user, "inv_hook_4")
    update = _paid_update("inv_hook_4", invoice_id=901)

    first = await _post(client, update)
    second = await _post(client, update)

    assert first.status == 200
    assert second.status == 200

    async with uow_factory() as uow:
        history = await uow.subscriptions.list_history(user.id)
    assert len(history) == 1, "Повторный вебхук не должен создавать вторую подписку"
    assert notifier.send.await_count == 1, "Уведомление отправляется только при первом начислении"


async def test_unknown_invoice_is_answered_200_and_logged(
    client: TestClient,
    notifier: AsyncMock,
) -> None:
    # Повторять доставку бессмысленно: счёта нет и не появится.
    response = await _post(client, _paid_update("нет-такого-счёта"))

    assert response.status == 200
    notifier.send.assert_not_awaited()


async def test_underpayment_does_not_grant_subscription(
    client: TestClient,
    user: User,
    db_session,
    uow_factory,
    notifier: AsyncMock,
) -> None:
    await _seed_payment(db_session, user, "inv_hook_5")

    response = await _post(client, _paid_update("inv_hook_5", invoice_id=902, amount="0.50"))

    assert response.status == 200
    async with uow_factory() as uow:
        assert await uow.subscriptions.get_live(user.id) is None, "Недоплата не открывает доступ"
    notifier.send.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Прочие события и мусор
# --------------------------------------------------------------------------- #


async def test_other_update_types_are_acknowledged(client: TestClient, notifier: AsyncMock) -> None:
    response = await _post(client, {"update_type": "invoice_expired", "payload": {}})

    assert response.status == 200, "Неинтересное событие подтверждается, чтобы его не слали снова"
    notifier.send.assert_not_awaited()


async def test_malformed_json_is_rejected_with_400(client: TestClient) -> None:
    body = "{это не json".encode("utf-8")
    response = await client.post(
        "/webhook/cryptobot", data=body, headers={SIGNATURE_HEADER: sign(body)}
    )

    assert response.status == 400


async def test_event_without_invoice_object_is_acknowledged(client: TestClient) -> None:
    response = await _post(client, {"update_type": "invoice_paid", "payload": "не объект"})

    assert response.status == 200


async def test_health_endpoint_answers(client: TestClient) -> None:
    response = await client.get("/health")

    assert response.status == 200
    assert (await response.json())["status"] == "ok"
