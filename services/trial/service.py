"""Прикладной сервис пробного периода.

Модуль не зависит от aiogram: он принимает простые значения (номер
телефона строкой) и возвращает результат, а превращение этого в кнопки и
сообщения — задача слоя хендлеров. Так правила выдачи триала проверяются
без Telegram, а замена библиотеки их не затрагивает.

Основная сложность здесь не в самой выдаче, а в защите от повторов. Их
три вида, и каждый закрывается своим механизмом:

* **повтор тем же аккаунтом** — отметка ``users.trial_activated_at``;
* **повтор новым аккаунтом с тем же телефоном** — таблица ``trial_claims``
  с уникальностью пары ``(kind, fingerprint)``: решение принимает БД, а не
  предварительная проверка, которая создавала бы окно гонки;
* **два параллельных запроса одного пользователя** — advisory-блокировка
  по идентификатору: строки подписки ещё нет, блокировать ``FOR UPDATE``
  нечего.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.config import TrialConfig
from core.logger import get_logger
from db.enums import SubscriptionPlan, SubscriptionSource, SubscriptionStatus, TrialFingerprintKind
from db.exceptions import TrialAlreadyUsedError
from db.locks import LockNamespace
from db.models import Subscription, TrialClaim, User
from db.repositories.errors import EntityNotFoundError
from db.uow import UnitOfWork
from services.trial.errors import (
    ContactRequiredError,
    SubscriptionAlreadyActiveError,
    TrialAlreadyClaimedError,
    TrialDisabledError,
    TrialFingerprintTakenError,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrialEligibility:
    """Ответ на вопрос «можно ли выдать триал прямо сейчас».

    Отдельный тип, а не исключение: экран подписки спрашивает о доступности
    в штатном режиме, и управлять потоком через исключения там было бы
    неуместно. При самой активации причина отказа уже становится ошибкой.
    """

    available: bool
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        """Закрыт ли доступ к пробному периоду."""
        return not self.available


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    """Итог успешной активации пробного периода."""

    subscription_id: int
    plan: SubscriptionPlan
    days_granted: int
    expires_at: datetime


class TrialService:
    """Сценарий выдачи пробного периода."""

    #: План, который выдаётся на время пробного периода.
    TRIAL_PLAN = SubscriptionPlan.PRO

    def __init__(self, uow: UnitOfWork, config: TrialConfig) -> None:
        self._uow = uow
        self._config = config

    @property
    def days(self) -> int:
        """Длительность пробного периода в сутках."""
        return self._config.days

    @property
    def requires_contact(self) -> bool:
        """Нужен ли подтверждённый номер телефона."""
        return self._config.enabled and self._config.require_contact

    async def check_eligibility(self, user: User) -> TrialEligibility:
        """Проверяет, доступен ли пользователю пробный период.

        Проверка неблокирующая и без побочных эффектов: отпечатки здесь не
        резервируются, поэтому её результат к моменту активации может
        устареть. Окончательное решение принимает :meth:`activate` под
        блокировкой.

        :param user: Пользователь.
        :return: Доступность и причина отказа.
        """
        if not self._config.enabled:
            return TrialEligibility(available=False, reason="Пробный период отключён.")

        if user.is_banned:
            return TrialEligibility(available=False, reason="Аккаунт заблокирован.")

        if user.has_used_trial:
            return TrialEligibility(
                available=False, reason="Пробный период уже был активирован."
            )

        subscription = await self._uow.subscriptions.get_live(user.id)
        if subscription is not None:
            return TrialEligibility(
                available=False, reason="У вас уже есть действующая подписка."
            )

        return TrialEligibility(available=True)

    async def activate(
        self,
        user: User,
        *,
        phone: str | None = None,
        now: datetime | None = None,
    ) -> TrialOutcome:
        """Выдаёт пробный период.

        Порядок шагов принципиален. Отпечаток резервируется **после**
        проверок состояния пользователя и подписки: занятый и тут же
        откаченный отпечаток стоил бы человеку права на триал, если бы
        отказ случился на следующем шаге. И наоборот, отметка о триале и
        сама подписка ставятся **после** успешного резервирования, чтобы
        отказ по чужому номеру не оставил след в аккаунте.

        Вся операция выполняется в транзакции вызывающего кода: при любой
        ошибке откатывается всё целиком, включая занятые отпечатки.

        :param user: Пользователь, запрашивающий триал.
        :param phone: Подтверждённый номер телефона, если он требуется.
        :param now: Момент операции; по умолчанию — текущее время UTC.
        :return: Итог с датой окончания пробного периода.
        :raises TrialDisabledError: Пробный период отключён настройками.
        :raises TrialAlreadyClaimedError: Триал уже был активирован.
        :raises SubscriptionAlreadyActiveError: Есть действующая подписка.
        :raises ContactRequiredError: Нужен номер телефона, но он не передан.
        :raises TrialFingerprintTakenError: Признак занят другим аккаунтом.
        :raises db.repositories.errors.EntityNotFoundError: Пользователь исчез.
        """
        if not self._config.enabled:
            logger.info("Запрос триала при отключённой настройке: user_id=%s", user.id)
            raise TrialDisabledError

        moment = now or datetime.now(tz=timezone.utc)

        # Критическая секция начинается до появления строк подписки и
        # отпечатков, поэтому блокируется идентификатор, а не строка.
        await self._uow.lock(LockNamespace.USER_TRIAL, user.id)

        # Состояние перечитывается уже под блокировкой. Экземпляр, который
        # передал хендлер, загружен раньше — параллельный запрос успел бы
        # отметить триал, и проверка по устаревшей копии пропустила бы
        # вторую выдачу.
        locked = await self._uow.users.get_for_update(user.id)
        if locked is None:
            logger.error("Пользователь id=%s исчез между запросом и выдачей триала", user.id)
            raise EntityNotFoundError("User", user.id)

        if locked.is_banned or locked.has_used_trial:
            logger.info("Отказ в триале: user_id=%s уже использовал его либо заблокирован", locked.id)
            raise TrialAlreadyClaimedError(locked.id)

        existing = await self._uow.subscriptions.get_live(locked.id)
        if existing is not None:
            logger.info(
                "Отказ в триале: у user_id=%s уже есть подписка id=%s", locked.id, existing.id
            )
            raise SubscriptionAlreadyActiveError

        await self._reserve_fingerprints(locked, phone)

        try:
            locked.mark_trial_started(moment)
        except TrialAlreadyUsedError as exc:
            # Гонка проиграна: параллельный запрос успел отметить триал.
            # Доменное исключение переводится в ошибку сервиса, чтобы слой
            # хендлеров зависел от одного семейства ошибок.
            logger.warning("Гонка при активации триала: user_id=%s", locked.id)
            raise TrialAlreadyClaimedError(locked.id) from exc

        result = await self._uow.subscriptions.get_or_create_live(
            locked.id,
            plan=self.TRIAL_PLAN,
            source=SubscriptionSource.TRIAL,
            status=SubscriptionStatus.TRIALING,
            period_days=self._config.days,
            now=moment,
        )
        subscription: Subscription = result.subscription

        logger.info(
            "Пробный период выдан: user_id=%s, подписка id=%s, план %s, до %s",
            locked.id, subscription.id, subscription.plan, subscription.expires_at,
        )
        return TrialOutcome(
            subscription_id=subscription.id,
            plan=subscription.plan,
            days_granted=self._config.days,
            expires_at=subscription.expires_at,
        )

    async def _reserve_fingerprints(self, user: User, phone: str | None) -> None:
        """Резервирует признаки пользователя за его аккаунтом.

        Через Bot API доступен ровно один надёжный признак — номер
        телефона, подтверждённый самим Telegram. IP-адреса и идентификатора
        устройства у бота нет: они появятся вместе с веб-версией или
        mini app, и :class:`TrialFingerprintKind` уже это предусматривает.

        :param user: Пользователь.
        :param phone: Подтверждённый номер телефона.
        :raises ContactRequiredError: Номер нужен, но не передан.
        :raises TrialFingerprintTakenError: Признак занят другим аккаунтом.
        """
        if not self.requires_contact:
            logger.warning(
                "Триал выдаётся без отпечатков (TRIAL_REQUIRE_CONTACT=false): user_id=%s", user.id
            )
            return

        if not phone or not phone.strip():
            raise ContactRequiredError

        fingerprint = TrialClaim.build_fingerprint(
            TrialFingerprintKind.PHONE, phone, self._config.fingerprint_secret
        )
        claim = await self._uow.users.register_trial_fingerprints(
            user.id, {TrialFingerprintKind.PHONE: fingerprint}
        )
        if claim.rejected:
            owner_id = await self._uow.users.find_trial_claim_owner(
                TrialFingerprintKind.PHONE, fingerprint
            )
            logger.warning(
                "Мультиаккаунт: user_id=%s запросил триал по номеру, занятому user_id=%s",
                user.id, owner_id,
            )
            raise TrialFingerprintTakenError(claim.conflicting_kind or TrialFingerprintKind.PHONE)
