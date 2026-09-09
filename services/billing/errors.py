"""Ошибки биллинга."""

from __future__ import annotations


class BillingError(Exception):
    """Базовая ошибка биллинга с текстом, пригодным для показа пользователю."""


class PlanNotFoundError(BillingError):
    """Запрошен несуществующий вариант оплаты."""

    def __init__(self, option_id: str) -> None:
        self.option_id = option_id
        super().__init__("Такой тариф больше не доступен.")


class PaymentNotFoundError(BillingError):
    """Счёт с указанным идентификатором не найден."""

    def __init__(self, invoice_id: str) -> None:
        self.invoice_id = invoice_id
        super().__init__("Счёт не найден или устарел.")


class PaymentMismatchError(BillingError):
    """Параметры платежа не совпадают с выставленным счётом.

    Возникает, если сумма, валюта или плательщик отличаются от
    зафиксированных при выставлении счёта.
    """


class TooManyPendingInvoicesError(BillingError):
    """У пользователя слишком много неоплаченных счетов."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            "Слишком много неоплаченных счетов. Завершите или отмените предыдущий."
        )
