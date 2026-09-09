"""Репозиторий платежей: идемпотентное создание счетов и подтверждение оплат."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.enums import FINAL_PAYMENT_STATUSES, PaymentProvider, PaymentStatus, SubscriptionPlan
from db.locks import LockNamespace, acquire_xact_lock
from db.models import Payment
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PaymentCreateResult:
    """Итог создания счёта."""

    payment: Payment
    created: bool

    @property
    def is_duplicate_request(self) -> bool:
        """Был ли счёт возвращён по ранее выданному ключу идемпотентности."""
        return not self.created


class PaymentRepository(BaseRepository[Payment]):
    """Доступ к платежам.

    Платёжный поток по определению принимает повторы: пользователь жмёт
    «Оплатить» дважды, Telegram переотправляет ``successful_payment``,
    CryptoBot дублирует вебхук. Поэтому каждая операция здесь либо
    защищена уникальным ограничением, либо выполняется под блокировкой.
    """

    model: ClassVar[type[Payment]] = Payment

    # ----------------------------------------------------------------- чтение
    @handle_db_errors
    async def get_by_invoice_id(self, provider: PaymentProvider, invoice_id: str) -> Payment | None:
        """Возвращает платёж по нашему идентификатору счёта."""
        stmt = select(Payment).where(
            Payment.provider == provider, Payment.invoice_id == invoice_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def get_by_invoice_id_for_update(
        self,
        provider: PaymentProvider,
        invoice_id: str,
    ) -> Payment | None:
        """Читает платёж по счёту, блокируя строку до конца транзакции.

        Именно этот метод следует вызывать в обработчике
        ``successful_payment``: блокировка сериализует две параллельные
        доставки одного события, и вторая увидит уже подтверждённый платёж.
        """
        stmt = (
            select(Payment)
            .where(Payment.provider == provider, Payment.invoice_id == invoice_id)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def get_by_external_id(
        self,
        provider: PaymentProvider,
        external_id: str,
    ) -> Payment | None:
        """Возвращает платёж по идентификатору транзакции провайдера."""
        stmt = select(Payment).where(
            Payment.provider == provider, Payment.external_id == external_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def get_by_idempotency_key(self, key: str) -> Payment | None:
        """Возвращает платёж по ключу идемпотентности запроса."""
        stmt = select(Payment).where(Payment.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def list_by_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        only_succeeded: bool = False,
    ) -> Sequence[Payment]:
        """История платежей пользователя, сначала новые."""
        stmt = select(Payment).where(Payment.user_id == user_id)
        if only_succeeded:
            stmt = stmt.where(Payment.status == PaymentStatus.SUCCEEDED)
        stmt = stmt.order_by(Payment.created_at.desc()).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    # ---------------------------------------------------------------- запись
    @handle_db_errors
    async def create_invoice(
        self,
        *,
        user_id: int,
        provider: PaymentProvider,
        invoice_id: str,
        idempotency_key: str,
        amount: Decimal,
        currency: str,
        plan: SubscriptionPlan,
        period_days: int,
        expires_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> PaymentCreateResult:
        """Создаёт счёт или возвращает ранее созданный по тому же ключу.

        Повторный тап по кнопке «Оплатить» не должен плодить счета.
        Вставка идёт с ``ON CONFLICT (idempotency_key) DO NOTHING``: если
        строка не появилась, счёт уже существует и возвращается как есть.
        Это дешевле и надёжнее, чем «сначала SELECT, потом INSERT».

        :param user_id: Плательщик.
        :param provider: Платёжный провайдер.
        :param invoice_id: Наш идентификатор счёта (invoice payload).
        :param idempotency_key: Ключ идемпотентности прикладного запроса.
        :param amount: Сумма (строго положительная).
        :param currency: Валюта (``XTR`` для Telegram Stars).
        :param plan: Оплачиваемый тариф.
        :param period_days: Оплачиваемый период в сутках.
        :param expires_at: Момент протухания счёта.
        :param payload: Дополнительные данные для аудита.
        :return: Платёж и признак того, что он создан этим вызовом.
        :raises ValueError: Некорректная сумма или период.
        """
        if amount <= 0:
            raise ValueError(f"Сумма платежа должна быть положительной, получено: {amount}")
        if period_days <= 0:
            raise ValueError(f"Период оплаты должен быть положительным, получено: {period_days}")

        stmt = (
            insert(Payment)
            .values(
                user_id=user_id,
                provider=provider,
                status=PaymentStatus.PENDING,
                invoice_id=invoice_id,
                idempotency_key=idempotency_key,
                amount=amount,
                currency=currency.upper(),
                plan=plan,
                period_days=period_days,
                expires_at=expires_at,
                payload=payload or {},
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(Payment)
        )
        payment = (await self._session.execute(stmt)).scalar_one_or_none()

        if payment is not None:
            logger.info(
                "Создан счёт id=%s (user_id=%s, %s %s, провайдер %s)",
                payment.id, user_id, amount, currency, provider,
            )
            return PaymentCreateResult(payment=payment, created=True)

        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is None:
            # Строка исчезла между вставкой и чтением — такое возможно только
            # при откате соседней транзакции; повтор операции решает проблему.
            logger.error("Счёт с ключом %s не найден после конфликта вставки", idempotency_key)
            raise LookupError(f"Счёт с ключом идемпотентности {idempotency_key!r} недоступен.")

        logger.info("Повторный запрос счёта по ключу %s вернул платёж id=%s", idempotency_key, existing.id)
        return PaymentCreateResult(payment=existing, created=False)

    async def confirm_payment(
        self,
        *,
        provider: PaymentProvider,
        invoice_id: str,
        external_id: str,
        paid_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> tuple[Payment | None, bool]:
        """Подтверждает оплату счёта.

        Строка платежа блокируется до изменения, поэтому две параллельные
        доставки события выстраиваются в очередь: первая переводит платёж в
        ``succeeded``, вторая видит уже подтверждённый платёж и получает
        ``changed=False``. Сама модель дополнительно проверяет совпадение
        ``external_id`` и не даёт «подтвердить» счёт чужой транзакцией.

        :param provider: Платёжный провайдер.
        :param invoice_id: Наш идентификатор счёта.
        :param external_id: Идентификатор транзакции у провайдера.
        :param paid_at: Момент списания средств (timezone-aware).
        :param payload: Сырой ответ провайдера.
        :return: Пара ``(платёж, признак изменения статуса)``; ``None``,
            если счёт не найден.
        :raises db.exceptions.InvalidStateTransitionError: Платёж находится
            в состоянии, из которого подтверждение невозможно.
        """
        # Лок по счёту берётся до чтения: он защищает и от гонки с
        # обработчиком отмены, который мог бы перевести счёт в expired.
        await acquire_xact_lock(self._session, LockNamespace.PAYMENT, invoice_id)

        payment = await self.get_by_invoice_id_for_update(provider, invoice_id)
        if payment is None:
            logger.warning("Подтверждение неизвестного счёта: %s / %s", provider, invoice_id)
            return None, False

        changed = payment.mark_succeeded(external_id, paid_at, payload)
        await self._session.flush()
        return payment, changed

    @handle_db_errors
    async def expire_stale_invoices(self, *, now: datetime, limit: int = 200) -> int:
        """Помечает протухшие неоплаченные счета.

        Чистка нужна не только для порядка: пока счёт висит в ``pending``,
        по нему нельзя выпустить новый с тем же ключом идемпотентности.

        :param now: Текущий момент (timezone-aware).
        :param limit: Максимальный размер порции.
        :return: Число помеченных счетов.
        :raises ValueError: Некорректный размер порции.
        """
        if limit < 1:
            raise ValueError(f"Размер порции должен быть не меньше 1, получено: {limit}")

        candidates = (
            select(Payment.id)
            .where(
                Payment.status.in_((PaymentStatus.PENDING, PaymentStatus.PROCESSING)),
                Payment.expires_at.is_not(None),
                Payment.expires_at <= now,
            )
            .order_by(Payment.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Payment)
            .where(Payment.id.in_(candidates))
            .values(status=PaymentStatus.EXPIRED, updated_at=func.now())
            .returning(Payment.id)
            .execution_options(synchronize_session=False)
        )
        expired = (await self._session.execute(stmt)).scalars().all()
        if expired:
            logger.info("Просрочено %d неоплаченных счетов", len(expired))
        return len(expired)

    @handle_db_errors
    async def total_revenue(
        self,
        *,
        currency: str,
        since: datetime | None = None,
    ) -> Decimal:
        """Сумма успешных платежей в указанной валюте.

        :param currency: Код валюты.
        :param since: Нижняя граница по дате оплаты.
        :return: Итоговая сумма (``0``, если платежей не было).
        """
        stmt = select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.currency == currency.upper(),
        )
        if since is not None:
            stmt = stmt.where(Payment.paid_at >= since)
        return Decimal(await self._session.scalar(stmt) or 0)

    @handle_db_errors
    async def count_pending(self, user_id: int) -> int:
        """Сколько у пользователя незавершённых счетов."""
        stmt = (
            select(func.count())
            .select_from(Payment)
            .where(
                Payment.user_id == user_id,
                Payment.status.not_in(tuple(FINAL_PAYMENT_STATUSES)),
            )
        )
        return int(await self._session.scalar(stmt) or 0)
