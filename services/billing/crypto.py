"""Клиент CryptoBot и проверка подлинности его вебхуков.

Модуль отвечает только за разговор с внешним API: он не знает ни про
подписки, ни про пользователей. Прикладные правила живут в
:class:`~services.billing.service.BillingService`, а идемпотентность
обеспечивается тем же механизмом, что и для звёзд, — уникальностью
``external_id`` и события подписки.

Обращения к чужому API устроены по принципу «повторяем только то, что
безопасно повторить». Создание счёта повторяется при сетевых сбоях и
ответах 5xx, потому что счёт с тем же ``payload`` идемпотентен на нашей
стороне: он привязан к строке ``payments``, созданной заранее. Ошибки
4xx не повторяются — они означают, что запрос неверен, и второй такой же
будет отвергнут точно так же.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import aiohttp

from core.logger import get_logger

logger = get_logger(__name__)

#: Заголовок с подписью тела вебхука.
SIGNATURE_HEADER: Final[str] = "crypto-pay-api-signature"

#: Заголовок с токеном приложения.
_TOKEN_HEADER: Final[str] = "Crypto-Pay-API-Token"

#: Сколько раз повторять безопасные для повтора запросы.
_MAX_ATTEMPTS: Final[int] = 3

#: Базовая пауза между повторами, секунды.
_BASE_DELAY: Final[float] = 0.5

#: Статусы счёта в терминах CryptoBot.
STATUS_ACTIVE: Final[str] = "active"
STATUS_PAID: Final[str] = "paid"
STATUS_EXPIRED: Final[str] = "expired"

#: Тип события вебхука об оплате.
UPDATE_INVOICE_PAID: Final[str] = "invoice_paid"


class CryptoBotError(RuntimeError):
    """Ошибка обращения к CryptoBot."""


class CryptoBotUnavailableError(CryptoBotError):
    """API недоступно: сеть, таймаут или ответ 5xx."""


class CryptoBotRejectedError(CryptoBotError):
    """API отвергло запрос: неверный токен, параметры или сумма."""


@dataclass(frozen=True, slots=True)
class CryptoInvoice:
    """Счёт, созданный на стороне CryptoBot."""

    invoice_id: int
    #: Ссылка, по которой пользователь оплачивает счёт.
    pay_url: str
    asset: str
    amount: Decimal
    status: str
    #: Наш идентификатор счёта, переданный в ``payload``.
    payload: str
    expires_at: datetime | None = None

    @property
    def is_paid(self) -> bool:
        """Оплачен ли счёт."""
        return self.status == STATUS_PAID


def verify_signature(token: str, body: bytes, signature: str) -> bool:
    """Проверяет подпись вебхука.

    CryptoBot подписывает тело запроса как ``HMAC-SHA256`` с ключом
    ``SHA256(токен приложения)``. Проверка обязательна: вебхук —
    единственный публично доступный вход в приложение, и без неё любой
    желающий начислил бы себе подписку, отправив нужный JSON.

    Сравнение идёт через :func:`hmac.compare_digest`: обычное ``==``
    завершается на первом несовпавшем байте, и по времени ответа подпись
    подбирается посимвольно.

    :param token: Токен приложения CryptoBot.
    :param body: Сырое тело запроса ровно в том виде, в каком оно пришло.
    :param signature: Значение заголовка с подписью.
    :return: ``True``, если подпись верна.
    """
    if not token or not signature:
        return False

    candidate = signature.strip()
    # compare_digest на строках отказывается работать с не-ASCII и бросает
    # TypeError. Заголовок приходит извне и содержать может что угодно, а
    # исключение здесь означало бы 500 в ответ на вебхук и бесконечные
    # повторы доставки.
    if not candidate.isascii():
        return False

    secret = hashlib.sha256(token.encode("utf-8")).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, candidate)


def parse_invoice(payload: dict[str, Any]) -> CryptoInvoice:
    """Разбирает объект счёта из ответа API или тела вебхука.

    :param payload: Словарь с полями счёта.
    :return: Счёт в виде значения.
    :raises CryptoBotRejectedError: Ответ не похож на счёт.
    """
    try:
        amount = Decimal(str(payload["amount"]))
        invoice = CryptoInvoice(
            invoice_id=int(payload["invoice_id"]),
            pay_url=str(payload.get("bot_invoice_url") or payload.get("pay_url") or ""),
            asset=str(payload.get("asset") or payload.get("currency_type") or ""),
            amount=amount,
            status=str(payload["status"]),
            payload=str(payload.get("payload") or ""),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise CryptoBotRejectedError(f"Неожиданный формат счёта CryptoBot: {exc}") from exc

    return invoice


class CryptoBotClient:
    """HTTP-клиент CryptoBot.

    Использует общую для приложения сессию aiohttp: отдельная сессия на
    сервис означала бы собственный пул соединений и потерю keep-alive.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        *,
        api_url: str = "https://pay.crypt.bot/api",
        timeout: float = 15.0,
    ) -> None:
        if not token:
            raise ValueError("Токен CryptoBot не задан.")
        self._session = session
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def create_invoice(
        self,
        *,
        asset: str,
        amount: Decimal,
        payload: str,
        description: str,
        expires_in: int | None = None,
    ) -> CryptoInvoice:
        """Создаёт счёт на оплату.

        :param asset: Криптовалюта счёта (``USDT``).
        :param amount: Сумма.
        :param payload: Наш идентификатор счёта — вернётся в вебхуке.
        :param description: Описание для пользователя.
        :param expires_in: Через сколько секунд счёт протухнет.
        :return: Созданный счёт со ссылкой на оплату.
        :raises CryptoBotUnavailableError: Сеть или сбой на стороне API.
        :raises CryptoBotRejectedError: API отвергло запрос.
        """
        body: dict[str, Any] = {
            "asset": asset,
            # Сумма передаётся строкой: JSON-число превратилось бы в float
            # и потеряло точность на суммах вроде 2.50.
            "amount": format(amount, "f"),
            "payload": payload,
            "description": description[:1024],
            # Счёт нужен один на платёж: повторное нажатие кнопки не должно
            # плодить счета на стороне провайдера.
            "allow_comments": False,
            "allow_anonymous": False,
        }
        if expires_in is not None:
            body["expires_in"] = expires_in

        result = await self._call("createInvoice", body)
        invoice = parse_invoice(result)
        logger.info(
            "Создан криптосчёт %s на %s %s (payload=%s)",
            invoice.invoice_id, invoice.amount, invoice.asset, payload,
        )
        return invoice

    async def get_invoice(self, invoice_id: int) -> CryptoInvoice | None:
        """Запрашивает состояние счёта.

        Нужен для сверки: вебхук может не дойти, и тогда состояние
        выясняется опросом.

        :param invoice_id: Идентификатор счёта в CryptoBot.
        :return: Счёт либо ``None``, если его нет.
        """
        result = await self._call("getInvoices", {"invoice_ids": str(invoice_id)})
        items = result.get("items") if isinstance(result, dict) else None
        if not items:
            return None
        return parse_invoice(items[0])

    async def get_me(self) -> dict[str, Any]:
        """Проверяет токен приложения.

        Вызывается на старте: неверный токен должен обнаруживаться при
        запуске, а не при первой попытке пользователя заплатить.
        """
        return await self._call("getMe", None)

    async def _call(self, method: str, body: dict[str, Any] | None) -> Any:
        """Выполняет запрос к API с повторами на временных сбоях.

        :param method: Имя метода API.
        :param body: Тело запроса.
        :return: Содержимое поля ``result``.
        :raises CryptoBotUnavailableError: Не удалось получить ответ.
        :raises CryptoBotRejectedError: API вернуло ошибку.
        """
        url = f"{self._api_url}/{method}"
        headers = {_TOKEN_HEADER: self._token}
        last_error: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._session.post(
                    url, json=body, headers=headers, timeout=self._timeout
                ) as response:
                    text = await response.text()

                    if response.status >= 500:
                        # Сбой на их стороне — повторяем.
                        last_error = CryptoBotUnavailableError(
                            f"CryptoBot ответил {response.status} на {method}"
                        )
                        logger.warning(
                            "CryptoBot %s: ответ %s (попытка %d из %d)",
                            method, response.status, attempt, _MAX_ATTEMPTS,
                        )
                    else:
                        return self._unpack(method, response.status, text)

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = CryptoBotUnavailableError(f"Сбой связи с CryptoBot: {exc}")
                logger.warning(
                    "CryptoBot %s: %s (попытка %d из %d)",
                    method, exc, attempt, _MAX_ATTEMPTS,
                )

            if attempt < _MAX_ATTEMPTS:
                # Экспоненциальная задержка: мгновенный повтор по тому же
                # перегруженному каналу только усугубляет ситуацию.
                await asyncio.sleep(_BASE_DELAY * 2 ** (attempt - 1))

        logger.error("CryptoBot %s недоступен после %d попыток", method, _MAX_ATTEMPTS)
        raise CryptoBotUnavailableError(
            f"CryptoBot не ответил на {method} за {_MAX_ATTEMPTS} попыток."
        ) from last_error

    @staticmethod
    def _unpack(method: str, status: int, text: str) -> Any:
        """Разбирает ответ API.

        :param method: Имя метода — для сообщения об ошибке.
        :param status: HTTP-статус.
        :param text: Тело ответа.
        :return: Содержимое поля ``result``.
        :raises CryptoBotRejectedError: Ответ неJSON или содержит ошибку.
        """
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CryptoBotRejectedError(
                f"CryptoBot вернул не JSON на {method}: {text[:200]!r}"
            ) from exc

        if not isinstance(payload, dict) or not payload.get("ok"):
            error = payload.get("error") if isinstance(payload, dict) else None
            raise CryptoBotRejectedError(
                f"CryptoBot отклонил {method} (HTTP {status}): {error or text[:200]!r}"
            )

        return payload.get("result")
