"""Ошибки биллинга."""

from __future__ import annotations


class BillingError(Exception):
    """Базовая ошибка биллинга.

    Несёт не готовый текст, а ключ перевода: сообщение показывается на
    языке пользователя, а сервисный слой языка не знает и знать не должен.
    Текст в самом исключении остаётся — он попадает в логи и в трассировки,
    где перевод только мешал бы.
    """

    #: Ключ строки в каталоге переводов.
    key: str = "common.error"


class PlanNotFoundError(BillingError):
    """Запрошен несуществующий вариант оплаты."""

    key = "billing.errors.plan_not_found"

    def __init__(self, option_id: str) -> None:
        self.option_id = option_id
        super().__init__("Такой тариф больше не доступен.")


class PaymentNotFoundError(BillingError):
    """Счёт с указанным идентификатором не найден."""

    key = "billing.errors.payment_not_found"

    def __init__(self, invoice_id: str) -> None:
        self.invoice_id = invoice_id
        super().__init__("Счёт не найден или устарел.")


class PaymentMismatchError(BillingError):
    """Параметры платежа не совпадают с выставленным счётом.

    Возникает, если сумма, валюта или плательщик отличаются от
    зафиксированных при выставлении счёта.
    """

    key = "billing.errors.payment_mismatch"


class TooManyPendingInvoicesError(BillingError):
    """У пользователя слишком много неоплаченных счетов."""

    key = "billing.errors.too_many_pending"

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            "Слишком много неоплаченных счетов. Завершите или отмените предыдущий."
        )
