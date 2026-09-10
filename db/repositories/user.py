"""Репозиторий пользователей, рефералов и защиты пробного периода."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Final

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from core.logger import get_logger
from db.enums import Language, TrialFingerprintKind
from db.enums import LIVE_SUBSCRIPTION_STATUSES
from db.models import Subscription, TrialClaim, User
from db.repositories.base import BaseRepository, handle_db_errors
from db.repositories.errors import ConflictError, RepositoryError

logger = get_logger(__name__)

#: Сколько раз пытаться подобрать свободный реферальный код.
_REFERRAL_CODE_ATTEMPTS: Final[int] = 5


@dataclass(frozen=True, slots=True)
class UserUpsertResult:
    """Итог операции «получить или создать пользователя»."""

    user: User
    created: bool


@dataclass(frozen=True, slots=True)
class TrialClaimResult:
    """Итог регистрации отпечатков пробного периода."""

    granted: bool
    conflicting_kind: TrialFingerprintKind | None = None

    @property
    def rejected(self) -> bool:
        """Был ли триал отклонён как повторный."""
        return not self.granted


class UserRepository(BaseRepository[User]):
    """Доступ к пользователям.

    Все операции создания устойчивы к гонкам: ``/start`` от одного человека
    легко приходит дважды подряд (двойной тап), и наивная связка
    «SELECT, потом INSERT» создала бы второго пользователя или упала бы на
    нарушении уникальности.
    """

    model: ClassVar[type[User]] = User

    @handle_db_errors
    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Возвращает пользователя по идентификатору Telegram."""
        stmt = select(User).where(User.telegram_id == telegram_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def get_by_telegram_id_for_update(self, telegram_id: int) -> User | None:
        """То же, но с блокировкой строки до конца транзакции."""
        # См. пояснение в BaseRepository.get_for_update.
        stmt = (
            select(User)
            .where(User.telegram_id == telegram_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def get_with_subscription(self, telegram_id: int) -> User | None:
        """Возвращает пользователя вместе с действующей подпиской.

        Связи объявлены с ``lazy="raise"``, поэтому обратиться к
        ``user.subscriptions`` после выхода из сессии нельзя — и это
        правильно: неявная подгрузка в асинхронном коде оборачивается
        либо лишним запросом на каждое обращение, либо падением вне
        контекста сессии. Здесь связь загружается явно.

        ``selectinload`` вместо ``joinedload``: отдельный запрос по
        списку идентификаторов не размножает строки пользователя по
        числу его подписок, а значит не заставляет базу и драйвер
        гонять один и тот же профиль несколько раз.

        Загружаются только действующие подписки: история продлений
        нужна отдельному экрану, а не каждому апдейту.

        :param telegram_id: Идентификатор пользователя в Telegram.
        :return: Пользователь с заполненным ``subscriptions`` либо ``None``.
        """
        stmt = (
            select(User)
            .where(User.telegram_id == telegram_id)
            .options(
                selectinload(
                    User.subscriptions.and_(
                        Subscription.status.in_(LIVE_SUBSCRIPTION_STATUSES)
                    )
                )
            )
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        """Проставляет пригласившего, если он ещё не назначен.

        Условие ``referred_by_id IS NULL`` — часть запроса, а не
        проверка в коде: иначе два одновременных перехода по разным
        реферальным ссылкам могли бы переписать «родителя» друг у
        друга. Сменить пригласившего задним числом нельзя вовсе —
        на этом держится честность реферальной программы.

        Начисление бонуса здесь не выполняется: его жизненный цикл
        живёт в таблице ``referrals`` и принадлежит сервисному слою.

        :param user_id: Приглашённый пользователь.
        :param referrer_id: Пригласивший пользователь.
        :return: ``True``, если связь установлена этим вызовом.
        :raises ValueError: Попытка назначить пользователя самому себе.
        """
        if user_id == referrer_id:
            raise ValueError(
                f"Пользователь {user_id} не может пригласить сам себя."
            )

        stmt = (
            update(User)
            .where(User.id == user_id, User.referred_by_id.is_(None))
            .values(referred_by_id=referrer_id, updated_at=func.now())
        )
        result = await self._session.execute(stmt)
        linked = bool(result.rowcount)

        if linked:
            logger.info("Пользователь id=%s привязан к рефереру id=%s", user_id, referrer_id)
        else:
            logger.info(
                "Реферер пользователя id=%s уже назначен, связь не изменена", user_id
            )
        return linked

    @handle_db_errors
    async def get_by_referral_code(self, code: str) -> User | None:
        """Возвращает владельца реферального кода.

        :param code: Код без учёта регистра.
        """
        normalized = code.strip().upper()
        if not normalized:
            return None
        stmt = select(User).where(User.referral_code == normalized)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        language: Language | None = None,
        referred_by_id: int | None = None,
    ) -> UserUpsertResult:
        """Возвращает пользователя, создавая его при первом обращении.

        Реализовано одним ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``:
        такая форма атомарна на стороне сервера и не оставляет окна между
        проверкой и вставкой. Профиль при этом освежается — имя и username
        в Telegram меняются.

        :param telegram_id: Идентификатор пользователя в Telegram.
        :param username: Имя пользователя без ``@``.
        :param first_name: Имя.
        :param last_name: Фамилия.
        :param language_code: Языковой код клиента.
        :param language: Язык интерфейса. Учитывается только при создании:
            подсказка клиента Telegram не должна перекрывать язык, который
            пользователь выбрал сам.
        :param referred_by_id: Пригласивший пользователь (учитывается только
            при создании — сменить «родителя» задним числом нельзя).
        :return: Пользователь и признак того, что он создан этим вызовом.
        :raises RepositoryError: Не удалось подобрать свободный реферальный код.
        """
        now = datetime.now(tz=timezone.utc)
        profile: dict[str, Any] = {
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "language_code": language_code,
        }

        # Поля, которые проставляются только при вставке: обновлять их из
        # каждого апдейта означало бы затирать выбор пользователя.
        initial: dict[str, Any] = {
            "language": language or Language.from_telegram(language_code),
        }

        last_conflict: ConflictError | None = None
        for attempt in range(1, _REFERRAL_CODE_ATTEMPTS + 1):
            try:
                return await self._upsert_user(
                    telegram_id=telegram_id,
                    profile=profile,
                    initial=initial,
                    referred_by_id=referred_by_id,
                    referral_code=User.generate_referral_code(),
                    now=now,
                )
            except ConflictError as exc:
                # Единственный ожидаемый здесь конфликт — коллизия
                # реферального кода; остальные пробрасываем как есть.
                if exc.constraint != "uq_users_referral_code":
                    raise
                last_conflict = exc
                logger.warning(
                    "Коллизия реферального кода при создании пользователя %s (попытка %d)",
                    telegram_id, attempt,
                )

        logger.error("Не удалось подобрать реферальный код за %d попыток", _REFERRAL_CODE_ATTEMPTS)
        raise RepositoryError("Не удалось создать пользователя: конфликт реферальных кодов.") from last_conflict

    @handle_db_errors
    async def _upsert_user(
        self,
        *,
        telegram_id: int,
        profile: Mapping[str, Any],
        initial: Mapping[str, Any],
        referred_by_id: int | None,
        referral_code: str,
        now: datetime,
    ) -> UserUpsertResult:
        """Выполняет сам upsert и определяет, была ли вставка.

        Системный столбец ``xmax`` равен нулю только у строк, вставленных
        текущей командой: это штатный способ отличить INSERT от UPDATE в
        одном запросе, не делая второго обращения к БД.
        """
        # Вложенная транзакция: конфликт по реферальному коду прерывает
        # текущую, и без SAVEPOINT повторная попытка была бы невозможна.
        async with self._session.begin_nested():
            stmt = (
                insert(User)
                .values(
                    telegram_id=telegram_id,
                    referral_code=referral_code,
                    referred_by_id=referred_by_id,
                    last_seen_at=now,
                    **profile,
                    **initial,
                )
                .on_conflict_do_update(
                    index_elements=["telegram_id"],
                    set_={**profile, "last_seen_at": now, "updated_at": func.now()},
                )
                .returning(User, literal_column("(xmax = 0)").label("inserted"))
            )
            row = (await self._session.execute(stmt)).one()

        user, inserted = row[0], bool(row[1])
        if inserted:
            logger.info("Создан пользователь telegram_id=%s (id=%s)", telegram_id, user.id)
        return UserUpsertResult(user=user, created=inserted)

    @handle_db_errors
    async def register_trial_fingerprints(
        self,
        user_id: int,
        fingerprints: Mapping[TrialFingerprintKind, str],
    ) -> TrialClaimResult:
        """Резервирует отпечатки пробного периода за пользователем.

        Проверка «есть ли уже такой отпечаток» отдельным запросом создавала
        бы окно гонки, поэтому решение принимает сама БД: вставка идёт с
        ``ON CONFLICT DO NOTHING``, и если хоть один отпечаток не записался,
        значит он уже занят другим аккаунтом — триал не выдаётся.

        :param user_id: Пользователь, запрашивающий триал.
        :param fingerprints: Отпечатки по типам признаков.
        :return: Результат с признаком выдачи и типом конфликтующего признака.
        """
        if not fingerprints:
            logger.warning("Запрос триала без отпечатков: user_id=%s", user_id)
            return TrialClaimResult(granted=False)

        rows = [
            {"user_id": user_id, "kind": kind, "fingerprint": value}
            for kind, value in fingerprints.items()
        ]

        stmt = (
            insert(TrialClaim)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["kind", "fingerprint"])
            .returning(TrialClaim.kind)
        )
        inserted_kinds = set((await self._session.execute(stmt)).scalars().all())

        missing = [kind for kind in fingerprints if kind not in inserted_kinds]
        if missing:
            logger.warning(
                "Триал отклонён: отпечаток %s уже использован (user_id=%s)", missing[0], user_id
            )
            return TrialClaimResult(granted=False, conflicting_kind=missing[0])

        logger.info("Отпечатки триала зарезервированы за user_id=%s", user_id)
        return TrialClaimResult(granted=True)

    @handle_db_errors
    async def find_trial_claim_owner(
        self,
        kind: TrialFingerprintKind,
        fingerprint: str,
    ) -> int | None:
        """Возвращает идентификатор пользователя, занявшего отпечаток."""
        stmt = select(TrialClaim.user_id).where(
            TrialClaim.kind == kind, TrialClaim.fingerprint == fingerprint
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @handle_db_errors
    async def set_language(self, user_id: int, language: Language) -> None:
        """Сохраняет выбранный пользователем язык интерфейса.

        Отдельный точечный UPDATE, а не изменение ORM-объекта: смена языка
        приходит из хендлера, которому не нужна вся строка пользователя.

        :param user_id: Идентификатор пользователя.
        :param language: Новый язык.
        """
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(language=language, updated_at=func.now())
        )
        await self._session.execute(stmt)
        logger.info("Язык пользователя id=%s изменён на %s", user_id, language.value)

    @handle_db_errors
    async def mark_bot_blocked(self, user_id: int, *, blocked: bool = True) -> None:
        """Отмечает, что пользователь заблокировал бота.

        Вызывается из рассылки при ответе 403: помеченные пользователи
        исключаются из следующих рассылок и не расходуют лимиты Bot API.
        """
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(is_bot_blocked=blocked, updated_at=func.now())
        )
        await self._session.execute(stmt)
        logger.info("Пользователь id=%s помечен как %s", user_id, "заблокировавший бота" if blocked else "активный")

    @handle_db_errors
    async def touch_last_seen(self, user_id: int) -> None:
        """Обновляет отметку последней активности."""
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(last_seen_at=func.now(), updated_at=func.now())
        )
        await self._session.execute(stmt)

    @handle_db_errors
    async def count_referrals(self, user_id: int) -> int:
        """Считает приглашённых пользователем."""
        stmt = select(func.count()).select_from(User).where(User.referred_by_id == user_id)
        return int(await self._session.scalar(stmt) or 0)

    @handle_db_errors
    async def list_referrals(self, user_id: int, limit: int = 50) -> Sequence[User]:
        """Возвращает приглашённых пользователем, сначала новых."""
        stmt = (
            select(User)
            .where(User.referred_by_id == user_id)
            .order_by(User.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()
