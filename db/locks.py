"""Рекомендательные блокировки PostgreSQL (advisory locks).

``SELECT ... FOR UPDATE`` умеет блокировать только существующую строку.
Когда критическая секция начинается *до* появления записи — например, два
параллельных вебхука об оплате пытаются создать подписку одному и тому же
пользователю, у которого её ещё нет, — блокировать нечего, и оба создадут
свою. Advisory-лок решает ровно эту задачу: он берётся по произвольному
ключу и живёт до конца транзакции.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger

logger = get_logger(__name__)


class LockNamespace(StrEnum):
    """Пространства имён блокировок.

    Разделяют ключи разных подсистем: пользователь с ``id=1`` в контексте
    подписки и в контексте платежа — это разные блокировки.
    """

    USER_SUBSCRIPTION = "user_subscription"
    USER_TRIAL = "user_trial"
    PAYMENT = "payment"


def build_lock_key(namespace: LockNamespace, value: str | int) -> int:
    """Превращает пару «пространство имён + значение» в 64-битный ключ.

    Хэширование вместо арифметики над идентификаторами: ключ advisory-лока
    в PostgreSQL — знаковое 64-битное целое, и наивная упаковка вроде
    ``namespace << 32 | user_id`` переполняется на больших идентификаторах.

    :param namespace: Пространство имён блокировки.
    :param value: Идентификатор объекта.
    :return: Ключ в диапазоне ``BIGINT``.
    """
    digest = hashlib.blake2b(f"{namespace.value}:{value}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def acquire_xact_lock(session: AsyncSession, namespace: LockNamespace, value: str | int) -> None:
    """Берёт блокировку до конца текущей транзакции.

    Блокировка снимается автоматически при COMMIT или ROLLBACK, поэтому
    «утечь» она не может даже при падении процесса.

    :param session: Сессия внутри открытой транзакции.
    :param namespace: Пространство имён блокировки.
    :param value: Идентификатор блокируемого объекта.
    """
    key = build_lock_key(namespace, value)
    await session.execute(select(func.pg_advisory_xact_lock(key)))
    logger.debug("Взята advisory-блокировка %s:%s", namespace.value, value)


async def try_acquire_xact_lock(
    session: AsyncSession,
    namespace: LockNamespace,
    value: str | int,
) -> bool:
    """Пытается взять блокировку, не дожидаясь освобождения.

    Нужна там, где ждать бессмысленно: если операция уже выполняется в
    соседней транзакции, повторный запрос пользователя можно просто
    отклонить, а не держать соединение.

    :param session: Сессия внутри открытой транзакции.
    :param namespace: Пространство имён блокировки.
    :param value: Идентификатор блокируемого объекта.
    :return: ``True``, если блокировка получена.
    """
    key = build_lock_key(namespace, value)
    acquired = await session.scalar(select(func.pg_try_advisory_xact_lock(key)))
    if not acquired:
        logger.info("Advisory-блокировка %s:%s занята другой транзакцией", namespace.value, value)
    return bool(acquired)
