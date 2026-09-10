"""Общие фикстуры тестового окружения.

Файл собирает три независимых контура, которые дальше переиспользуются
всеми тестами:

* **База данных.** Настоящий PostgreSQL — подменить его на SQLite нельзя:
  схема опирается на нативные ENUM, JSONB, ``TSVECTOR`` с русской
  конфигурацией поиска, частичные индексы, ``FOR UPDATE SKIP LOCKED`` и
  advisory-блокировки. Изоляция — очистка таблиц после каждого теста, а не
  откат внешней транзакции: транзакции здесь фиксируются по-настоящему,
  поэтому блокировки, ``SKIP LOCKED`` и гонки двух параллельных операций
  ведут себя так же, как в бою. Внутри одной общей транзакции такие тесты
  были бы бессмысленны — сессия не конфликтует сама с собой.
* **Redis.** Ограничитель частоты проверяется либо на настоящем Redis
  (``TEST_REDIS_URL``), либо на ``fakeredis`` с поддержкой Lua — без Lua
  скрипт token bucket не исполнится, а именно он и есть предмет проверки.
  Политике анти-флуда и middleware хватает ``InMemoryRateLimiter``.
* **Telegram.** Сетевой слой aiogram заменён на :class:`MockedSession`:
  бот собирается настоящий, вызовы Bot API реально сериализуются и
  валидируются, но запрос не уходит в сеть, а попадает в очередь для
  проверок. Это ловит ошибки в параметрах вызова, которых не видит
  ``AsyncMock``.

Переменные окружения тестового прогона::

    TEST_DATABASE_URL   полный DSN тестовой БД (перекрывает TEST_DB_*)
    TEST_DB_HOST        по умолчанию localhost
    TEST_DB_PORT        по умолчанию 5432
    TEST_DB_USER        по умолчанию postgres
    TEST_DB_PASS        по умолчанию postgres
    TEST_DB_NAME        по умолчанию news_db_test
    TEST_REDIS_URL      если задан, используется настоящий Redis
"""

from __future__ import annotations

import asyncio
import os
import types
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final, Union, get_args, get_origin
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    Contact,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ResponseParameters,
    SuccessfulPayment,
    TelegramObject,
    Update,
    User as TelegramUser,
)
from sqlalchemy import URL, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from core.config import Settings, load_settings
from db.base import Base
from db.enums import (
    PaymentProvider,
    PaymentStatus,
    PostStatus,
    SubscriptionPlan,
    SubscriptionSource,
    SubscriptionStatus,
)
from db.models import Payment, Post, Subscription, User
from db.uow import UnitOfWork, UnitOfWorkFactory
from services.billing import BillingService
from services.dedup import DedupConfig
from services.notifier import DeliveryResult, DeliveryStatus, TelegramNotifier
from services.parser import MediaItem, ParsedPost, TelegramWebParser
from services.ratelimit.base import RateLimitRule
from services.ratelimit.memory import InMemoryRateLimiter
from services.ratelimit.policy import AntiFloodConfig, AntiFloodPolicy
from services.trial import TrialService

# --------------------------------------------------------------------------- #
# Константы тестового окружения.
#
# Все идентификаторы фиксированные: тест, падающий на случайных данных,
# невозможно воспроизвести, а «плавающие» значения прячут ошибки сравнения.
# --------------------------------------------------------------------------- #

#: Момент, относительно которого строятся все временны́е проверки.
FROZEN_NOW: Final[datetime] = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)

BOT_ID: Final[int] = 42
BOT_TOKEN: Final[str] = f"{BOT_ID}:TESTTOKENoooooooooooooooooooooooooo"
BOT_USERNAME: Final[str] = "news_aggregator_test_bot"

#: Telegram-идентификатор пользователя по умолчанию.
TELEGRAM_ID: Final[int] = 100_500

#: Идентификатор личного чата совпадает с идентификатором пользователя.
CHAT_ID: Final[int] = TELEGRAM_ID

DEFAULT_CHANNEL: Final[str] = "habr_com"

#: Значения окружения, зафиксированные на время прогона. Проверяемые
#: границы намеренно занижены: тест на пятое нарушение подряд не должен
#: превращаться в тест на терпение.
_TEST_ENVIRONMENT: Final[dict[str, str]] = {
    "BOT_TOKEN": BOT_TOKEN,
    "LOG_LEVEL": "CRITICAL",
    "DISPLAY_TZ": "UTC",
    "REDIS_URL": "",
    "REDIS_PREFIX": "test",
    "RATE_LIMIT_ENABLED": "true",
    "RL_MESSAGE_LIMIT": "3",
    "RL_MESSAGE_WINDOW": "60",
    "RL_MESSAGE_BURST": "3",
    "RL_CALLBACK_LIMIT": "5",
    "RL_CALLBACK_WINDOW": "60",
    "RL_CALLBACK_BURST": "5",
    "RL_REFRESH_LIMIT": "1",
    "RL_REFRESH_WINDOW": "60",
    "RL_SINGLE_FLIGHT_TTL": "5",
    "RL_VIOLATIONS_BEFORE_MUTE": "2",
    "RL_VIOLATION_WINDOW": "60",
    "RL_WARN_COOLDOWN": "10",
    "RL_MUTE_DURATIONS": "30,120,600",
    "RL_MUTE_LEVEL_TTL": "3600",
    "INVOICE_TTL_MINUTES": "15",
    "MAX_PENDING_INVOICES": "3",
    "TRIAL_ENABLED": "true",
    "TRIAL_DAYS": "7",
    "TRIAL_REQUIRE_CONTACT": "true",
    "TRIAL_FINGERPRINT_SECRET": "test-trial-secret",
    "WORKER_EXPIRY_NOTICE_HOURS": "24",
    "WORKER_EXPIRY_INTERVAL": "900",
    "WORKER_EXPIRATION_INTERVAL": "300",
    "WORKER_INVOICE_INTERVAL": "600",
    "WORKER_BATCH_SIZE": "100",
    "DEDUP_ENABLED": "true",
    "DEDUP_HAMMING_THRESHOLD": "16",
    "DEDUP_SIMILARITY_THRESHOLD": "0.75",
    "DEDUP_SHORT_TEXT_THRESHOLD": "0.9",
    "DEDUP_SHORT_TEXT_WORDS": "12",
    "DEDUP_LOOKBACK_HOURS": "48",
    "DEDUP_CANDIDATE_LIMIT": "200",
    "PARSE_COOLDOWN": "60",
    "MAX_POSTS": "10",
    "REQUEST_TIMEOUT": "5",
    "MEDIA_TIMEOUT": "5",
}


def _test_database_url() -> URL:
    """Собирает DSN тестовой базы.

    Параметры берутся из ``TEST_*``, а не из ``DB_*``: рабочий ``.env``
    указывает на боевую (или docker-compose) базу, и случайный прогон
    тестов не должен её затрагивать — схема здесь пересоздаётся с нуля.
    """
    raw = os.getenv("TEST_DATABASE_URL", "").strip()
    if raw:
        url = make_url(raw)
        # Драйвер приводится к асинхронному: DSN обычно копируют из psql,
        # а синхронный psycopg2 в асинхронном движке не заработает.
        if url.drivername in ("postgresql", "postgres"):
            url = url.set(drivername="postgresql+asyncpg")
        return url

    return URL.create(
        drivername="postgresql+asyncpg",
        username=os.getenv("TEST_DB_USER", "postgres"),
        password=os.getenv("TEST_DB_PASS", "postgres"),
        host=os.getenv("TEST_DB_HOST", "localhost"),
        port=int(os.getenv("TEST_DB_PORT", "5432")),
        database=os.getenv("TEST_DB_NAME", "news_db_test"),
    )


# --------------------------------------------------------------------------- #
# Окружение и конфигурация
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session", autouse=True)
def test_environment() -> Iterator[URL]:
    """Фиксирует переменные окружения на весь прогон.

    Патч ставится до первого обращения к :func:`core.config.load_settings`,
    поэтому настройки собираются из тестовых значений, а не из рабочего
    ``.env``, который может лежать рядом.
    """
    url = _test_database_url()
    patcher = pytest.MonkeyPatch()
    for key, value in _TEST_ENVIRONMENT.items():
        patcher.setenv(key, value)

    # Настройки приложения тоже должны смотреть на тестовую базу: иначе
    # код, собирающий Database из Settings, ушёл бы в боевую.
    patcher.setenv("DB_USER", url.username or "postgres")
    patcher.setenv("DB_PASS", url.password or "postgres")
    patcher.setenv("DB_NAME", url.database or "news_db_test")
    patcher.setenv("DB_HOST", url.host or "localhost")
    patcher.setenv("DB_PORT", str(url.port or 5432))

    try:
        yield url
    finally:
        patcher.undo()


@pytest.fixture(scope="session")
def settings(test_environment: URL) -> Settings:
    """Настройки приложения, собранные из тестового окружения."""
    return load_settings()


@pytest.fixture(scope="session")
def dedup_config(settings: Settings) -> DedupConfig:
    """Параметры дедупликации в виде, который принимает сервис."""
    values = settings.dedup
    return DedupConfig(
        enabled=values.enabled,
        hamming_threshold=values.hamming_threshold,
        similarity_threshold=values.similarity_threshold,
        short_text_threshold=values.short_text_threshold,
        short_text_words=values.short_text_words,
        lookback_hours=values.lookback_hours,
        candidate_limit=values.candidate_limit,
    )


# --------------------------------------------------------------------------- #
# База данных
# --------------------------------------------------------------------------- #


async def _ensure_database(url: URL) -> None:
    """Создаёт тестовую базу, если её ещё нет.

    Подключение идёт к служебной базе ``postgres`` в режиме AUTOCOMMIT:
    ``CREATE DATABASE`` не выполняется внутри транзакции.

    :raises Skipped: PostgreSQL недоступен — тесты слоя данных пропускаются
        с внятным объяснением вместо каскада ошибок соединения.
    """
    maintenance = create_async_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        connect_args={"timeout": 5},
    )
    try:
        async with maintenance.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            )
            if not exists:
                # Имя приходит из окружения разработчика, а не из запроса
                # пользователя, но кавычки всё равно обязательны: иначе
                # база с дефисом в имени не создастся.
                await connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    except (OSError, SQLAlchemyError, asyncio.TimeoutError) as exc:
        pytest.skip(
            f"PostgreSQL недоступен по адресу {url.host}:{url.port} "
            f"({exc.__class__.__name__}). Поднимите базу "
            "(docker compose up -d db) или задайте TEST_DATABASE_URL."
        )
    finally:
        await maintenance.dispose()


@pytest_asyncio.fixture(scope="session")
async def db_engine(test_environment: URL) -> AsyncIterator[AsyncEngine]:
    """Движок тестовой базы с развёрнутой с нуля схемой.

    Схема сначала удаляется, потом создаётся: предыдущий прогон мог
    оборваться на других моделях, и остатки старой схемы дали бы
    непредсказуемые падения.

    Пул соединений оставлен обычным: каждая сессия получает собственное
    соединение, поэтому две параллельные транзакции в тесте действительно
    конкурируют за строки, а не выполняются по очереди на одном канале.
    """
    url = test_environment
    await _ensure_database(url)

    engine = create_async_engine(
        url,
        pool_size=10,
        max_overflow=10,
        connect_args={"timeout": 5},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    db_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Фабрика сессий с очисткой таблиц после теста.

    Именно фабрика, а не готовая сессия: ``UnitOfWork`` и фоновые задачи
    открывают транзакции сами, и подсовывать им чужую сессию значило бы
    проверять не тот код, который работает в проде.

    Все изменения фиксируются по-настоящему, поэтому по завершении теста
    таблицы очищаются. ``TRUNCATE`` ждёт освобождения таблиц не дольше
    пяти секунд: незакрытая транзакция теста должна приводить к внятной
    ошибке, а не к бесконечно висящему прогону.
    """
    factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        yield factory
    finally:
        await _truncate_all(db_engine)


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Сессия для подготовки данных и прямых проверок в базе.

    Отдельная от той, в которой работает проверяемый код: так тест видит
    результат его транзакции, а не собственный кэш объектов.
    """
    async with session_factory() as session:
        yield session


@pytest.fixture
def uow_factory(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWorkFactory:
    """Фабрика единиц работы поверх транзакции теста."""
    return UnitOfWorkFactory(session_factory)


@pytest_asyncio.fixture
async def uow(uow_factory: UnitOfWorkFactory) -> AsyncIterator[UnitOfWork]:
    """Открытая единица работы — точка входа в репозиторный слой."""
    async with uow_factory() as unit:
        yield unit


async def _truncate_all(engine: AsyncEngine) -> None:
    """Очищает все таблицы, сбрасывая счётчики идентификаторов."""
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


# --------------------------------------------------------------------------- #
# Фабрики доменных сущностей
#
# Сделаны вручную, а не через polyfactory/factory_boy: генератор случайных
# значений не знает про CHECK-ограничения модели (``expires_at > started_at``,
# ``amount > 0``), про генерируемую колонку ``search_vector``, которую нельзя
# заполнять, и про ``lazy="raise"`` на связях. Явные значения к тому же
# делают проверку сумм и дат читаемой.
# --------------------------------------------------------------------------- #

#: Тип асинхронной фабрики: принимает переопределения, возвращает сущность.
Factory = Callable[..., Awaitable[Any]]


def build_user(**overrides: Any) -> User:
    """Собирает пользователя, не сохраняя его."""
    defaults: dict[str, Any] = {
        "telegram_id": TELEGRAM_ID,
        "username": "tester",
        "first_name": "Тест",
        "last_name": "Тестов",
        "language_code": "ru",
        "is_admin": False,
        "is_banned": False,
        "is_bot_blocked": False,
        "referral_code": User.generate_referral_code(),
    }
    defaults.update(overrides)
    return User(**defaults)


def build_subscription(user_id: int, **overrides: Any) -> Subscription:
    """Собирает подписку, не сохраняя её."""
    started_at = overrides.pop("started_at", FROZEN_NOW - timedelta(days=1))
    defaults: dict[str, Any] = {
        "user_id": user_id,
        "plan": SubscriptionPlan.PRO,
        "status": SubscriptionStatus.ACTIVE,
        "source": SubscriptionSource.PAYMENT,
        "started_at": started_at,
        "expires_at": started_at + timedelta(days=30),
        "auto_renew": False,
    }
    defaults.update(overrides)
    return Subscription(**defaults)


def build_payment(user_id: int, **overrides: Any) -> Payment:
    """Собирает счёт, не сохраняя его."""
    invoice_id = overrides.pop("invoice_id", Payment.generate_invoice_id())
    defaults: dict[str, Any] = {
        "user_id": user_id,
        "provider": PaymentProvider.TELEGRAM_STARS,
        "status": PaymentStatus.PENDING,
        "invoice_id": invoice_id,
        "idempotency_key": f"stars:{user_id}:{invoice_id}",
        "amount": Decimal(150),
        "currency": "XTR",
        "plan": SubscriptionPlan.PRO,
        "period_days": 30,
        "payload": {"option_id": "pro_1m"},
        "expires_at": FROZEN_NOW + timedelta(minutes=15),
    }
    defaults.update(overrides)
    return Payment(**defaults)


def build_post(**overrides: Any) -> Post:
    """Собирает новость, не сохраняя её.

    ``search_vector`` не задаётся намеренно: это генерируемая колонка,
    любая попытка записать её приведёт к ошибке PostgreSQL.
    """
    defaults: dict[str, Any] = {
        "channel_name": DEFAULT_CHANNEL,
        "message_id": 1,
        "post_time": FROZEN_NOW,
        "content": "Тестовая новость про релиз новой версии сервиса.",
        "media_urls": [],
        "status": PostStatus.NEW,
    }
    defaults.update(overrides)
    return Post(**defaults)


@pytest.fixture
def make_user(db_session: AsyncSession) -> Factory:
    """Создаёт пользователя в базе и возвращает его с проставленным ``id``.

    Изменения фиксируются, а не просто выталкиваются: проверяемый код
    работает в собственной транзакции и незафиксированных строк не увидит.
    """
    counter = iter(range(10_000))

    async def _make(**overrides: Any) -> User:
        overrides.setdefault("telegram_id", TELEGRAM_ID + next(counter))
        user = build_user(**overrides)
        db_session.add(user)
        await db_session.commit()
        return user

    return _make


@pytest.fixture
def make_subscription(db_session: AsyncSession) -> Factory:
    """Создаёт подписку в базе."""

    async def _make(user: User, **overrides: Any) -> Subscription:
        subscription = build_subscription(user.id, **overrides)
        db_session.add(subscription)
        await db_session.commit()
        return subscription

    return _make


@pytest.fixture
def make_payment(db_session: AsyncSession) -> Factory:
    """Создаёт счёт в базе."""

    async def _make(user: User, **overrides: Any) -> Payment:
        payment = build_payment(user.id, **overrides)
        db_session.add(payment)
        await db_session.commit()
        return payment

    return _make


@pytest.fixture
def make_post(db_session: AsyncSession) -> Factory:
    """Создаёт новость в базе."""
    counter = iter(range(1, 10_000))

    async def _make(**overrides: Any) -> Post:
        overrides.setdefault("message_id", next(counter))
        post = build_post(**overrides)
        db_session.add(post)
        await db_session.commit()
        return post

    return _make


@pytest_asyncio.fixture
async def user(make_user: Factory) -> User:
    """Пользователь по умолчанию с фиксированным ``telegram_id``."""
    return await make_user(telegram_id=TELEGRAM_ID)


# --------------------------------------------------------------------------- #
# Telegram: бот без сети
# --------------------------------------------------------------------------- #


def _bot_user() -> TelegramUser:
    """Учётная запись самого бота."""
    return TelegramUser(id=BOT_ID, is_bot=True, first_name="TestBot", username=BOT_USERNAME)


class MockedSession(BaseSession):
    """Сессия Bot API, которая никуда не ходит.

    Запросы не отбрасываются, а складываются в очередь: тест проверяет не
    только факт вызова, но и параметры — текст, клавиатуру, сумму счёта.
    Ответы отдаются в порядке добавления (FIFO), а если очередь пуста,
    подставляется правдоподобный результат по типу метода: заставлять тест
    описывать ответ на каждый ``callback.answer()`` — лишний шум.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: deque[TelegramMethod[Any]] = deque()
        self.responses: deque[Response[Any]] = deque()
        self.closed = False
        self._message_id = 1000

    # ------------------------------------------------------------ контракт
    async def close(self) -> None:
        """Закрывает сессию."""
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        """Записывает запрос и возвращает подготовленный ответ.

        Ответ проходит через штатный :meth:`BaseSession.check_response`,
        поэтому подготовленная ошибка превращается в настоящее исключение
        aiogram (``TelegramForbiddenError``, ``TelegramRetryAfter`` и так
        далее), а не в подделку.
        """
        self.requests.append(method)
        response = (
            self.responses.popleft() if self.responses else self._default_response(bot, method)
        )
        checked = self.check_response(
            bot=bot,
            method=method,
            status_code=response.error_code or 200,
            content=response.model_dump_json(),
        )
        return checked.result  # type: ignore[return-value]

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncIterator[bytes]:
        """Отдаёт фиктивное содержимое файла."""
        yield b"test-content"

    # -------------------------------------------------------------- помощь
    def add_response(self, response: Response[Any]) -> None:
        """Ставит готовый ответ в очередь."""
        self.responses.append(response)

    def pop_request(self) -> TelegramMethod[Any]:
        """Достаёт самый ранний необработанный запрос.

        :raises AssertionError: Запросов не было.
        """
        if not self.requests:
            raise AssertionError("Ожидался запрос к Bot API, но бот не сделал ни одного.")
        return self.requests.popleft()

    def requests_of(self, method: type[TelegramMethod[Any]]) -> list[TelegramMethod[Any]]:
        """Возвращает все запросы указанного типа."""
        return [request for request in self.requests if isinstance(request, method)]

    def _default_response(self, bot: Bot, method: TelegramMethod[TelegramType]) -> Response[Any]:
        """Строит ответ по типу возвращаемого значения метода."""
        returning = method.__returning__
        options = (
            get_args(returning)
            if get_origin(returning) is Union or isinstance(returning, types.UnionType)
            else (returning,)
        )

        result: Any
        if Message in options:
            result = self._build_message(bot, method)
        elif TelegramUser in options:
            result = _bot_user()
        elif bool in options:
            result = True
        else:
            raise AssertionError(
                f"Для метода {type(method).__name__} нет ответа по умолчанию "
                f"(возвращает {returning!r}). Задайте его через bot.add_result_for(...)."
            )

        return Response[returning](ok=True, result=result)  # type: ignore[valid-type]

    def _build_message(self, bot: Bot, method: TelegramMethod[Any]) -> Message:
        """Собирает правдоподобный ответ на отправку сообщения."""
        self._message_id += 1
        raw_chat_id = getattr(method, "chat_id", CHAT_ID)
        chat_id = raw_chat_id if isinstance(raw_chat_id, int) else CHAT_ID
        return Message(
            message_id=self._message_id,
            date=datetime.now(tz=timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            from_user=_bot_user(),
            text=getattr(method, "text", None) or getattr(method, "caption", None),
        ).as_(bot)


class MockedBot(Bot):
    """Настоящий :class:`aiogram.Bot` поверх :class:`MockedSession`.

    Именно настоящий, а не ``AsyncMock``: параметры вызова проходят
    валидацию pydantic, поэтому опечатка в имени поля или неверный тип
    клавиатуры падают в тесте, а не в проде.
    """

    session: MockedSession

    def __init__(self) -> None:
        super().__init__(
            token=BOT_TOKEN,
            session=MockedSession(),
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        # Подставляем «себя» заранее: обращение к bot.me() не должно
        # тратить подготовленный ответ из очереди.
        self._me = _bot_user()

    def add_result_for(
        self,
        method: type[TelegramMethod[Any]],
        *,
        ok: bool = True,
        result: Any = None,
        error_code: int = 200,
        description: str | None = None,
        retry_after: int | None = None,
        migrate_to_chat_id: int | None = None,
    ) -> None:
        """Ставит в очередь ответ (или ошибку) на следующий вызов Bot API.

        Пример — пользователь заблокировал бота::

            bot.add_result_for(
                SendMessage,
                ok=False,
                error_code=403,
                description="Forbidden: bot was blocked by the user",
            )
        """
        parameters = (
            ResponseParameters(retry_after=retry_after, migrate_to_chat_id=migrate_to_chat_id)
            if retry_after is not None or migrate_to_chat_id is not None
            else None
        )
        response = Response[method.__returning__](  # type: ignore[misc]
            ok=ok,
            result=result,
            error_code=error_code,
            description=description,
            parameters=parameters,
        )
        self.session.add_response(response)


@pytest_asyncio.fixture
async def bot() -> AsyncIterator[MockedBot]:
    """Бот с подменённым транспортом."""
    instance = MockedBot()
    try:
        yield instance
    finally:
        await instance.session.close()


@pytest.fixture
def bot_mock() -> AsyncMock:
    """Бот как ``AsyncMock`` — для проверок «вызвано столько-то раз».

    Применяется там, где предмет теста — сам факт и порядок обращений
    (например, соблюдение лимита исходящих в нотификаторе), а содержимое
    сообщения роли не играет.
    """
    mock = AsyncMock(spec=Bot)
    mock.id = BOT_ID
    return mock


@pytest.fixture
def telegram_user() -> TelegramUser:
    """Автор обновления."""
    return TelegramUser(
        id=TELEGRAM_ID,
        is_bot=False,
        first_name="Тест",
        last_name="Тестов",
        username="tester",
        language_code="ru",
    )


@pytest.fixture
def chat() -> Chat:
    """Личный чат с пользователем."""
    return Chat(id=CHAT_ID, type="private")


@pytest.fixture
def make_message(bot: MockedBot, telegram_user: TelegramUser, chat: Chat) -> Callable[..., Message]:
    """Строит входящее сообщение, привязанное к боту.

    Привязка обязательна: без неё ``message.answer(...)`` не знает, через
    какой бот отправлять ответ, и падает на обращении к контексту.
    """
    counter = iter(range(1, 10_000))

    def _make(text: str | None = "/start", **overrides: Any) -> Message:
        payload: dict[str, Any] = {
            "message_id": next(counter),
            "date": datetime.now(tz=timezone.utc),
            "chat": chat,
            "from_user": telegram_user,
            "text": text,
        }
        payload.update(overrides)
        return Message(**payload).as_(bot)

    return _make


@pytest.fixture
def make_callback(
    bot: MockedBot,
    telegram_user: TelegramUser,
    make_message: Callable[..., Message],
) -> Callable[..., CallbackQuery]:
    """Строит нажатие на инлайн-кнопку."""
    counter = iter(range(1, 10_000))

    def _make(data: str = "menu:plans", **overrides: Any) -> CallbackQuery:
        index = next(counter)
        payload: dict[str, Any] = {
            "id": f"cb-{index}",
            "from_user": telegram_user,
            "chat_instance": f"chat-instance-{index}",
            "data": data,
            "message": make_message(text="Сообщение с кнопкой"),
        }
        payload.update(overrides)
        return CallbackQuery(**payload).as_(bot)

    return _make


@pytest.fixture
def make_pre_checkout(
    bot: MockedBot,
    telegram_user: TelegramUser,
) -> Callable[..., PreCheckoutQuery]:
    """Строит запрос подтверждения оплаты."""
    counter = iter(range(1, 10_000))

    def _make(
        payload: str,
        *,
        total_amount: int = 150,
        currency: str = "XTR",
        **overrides: Any,
    ) -> PreCheckoutQuery:
        data: dict[str, Any] = {
            "id": f"pcq-{next(counter)}",
            "from_user": telegram_user,
            "currency": currency,
            "total_amount": total_amount,
            "invoice_payload": payload,
        }
        data.update(overrides)
        return PreCheckoutQuery(**data).as_(bot)

    return _make


@pytest.fixture
def make_successful_payment(make_message: Callable[..., Message]) -> Callable[..., Message]:
    """Строит сообщение об успешной оплате.

    Отдаётся именно сообщение, а не голый ``SuccessfulPayment``: хендлер
    получает от Telegram обновление целиком, и тест должен идти тем же
    путём.
    """

    def _make(
        payload: str,
        *,
        charge_id: str = "charge-1",
        total_amount: int = 150,
        currency: str = "XTR",
        **overrides: Any,
    ) -> Message:
        payment = SuccessfulPayment(
            currency=currency,
            total_amount=total_amount,
            invoice_payload=payload,
            telegram_payment_charge_id=charge_id,
            provider_payment_charge_id=f"provider-{charge_id}",
        )
        return make_message(text=None, successful_payment=payment, **overrides)

    return _make


@pytest.fixture
def make_update(bot: MockedBot) -> Callable[..., Update]:
    """Заворачивает событие в ``Update`` для прогона через диспетчер."""
    counter = iter(range(1, 10_000))

    def _make(event: TelegramObject) -> Update:
        field = {
            Message: "message",
            CallbackQuery: "callback_query",
            PreCheckoutQuery: "pre_checkout_query",
        }.get(type(event))
        if field is None:
            raise AssertionError(f"Неизвестный тип события: {type(event).__name__}")
        return Update(update_id=next(counter), **{field: event}).as_(bot)

    return _make


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[MemoryStorage]:
    """Хранилище состояний FSM в памяти процесса."""
    instance = MemoryStorage()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def fsm_context(storage: MemoryStorage) -> FSMContext:
    """Контекст FSM для пользователя по умолчанию."""
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=TELEGRAM_ID),
    )


@pytest.fixture
def dispatcher(storage: MemoryStorage, settings: Settings) -> Dispatcher:
    """Пустой диспетчер с данными уровня приложения.

    Роутеры и middleware тест подключает сам: так проверяется ровно тот
    набор слоёв, который важен, без влияния соседних.
    """
    instance = Dispatcher(storage=storage)
    instance["settings"] = settings
    return instance


@pytest.fixture
def handler_stub() -> AsyncMock:
    """Подставной хендлер для тестов middleware.

    Возвращает опознаваемое значение: тест отличает «middleware пропустил
    обновление» от «middleware вернул None и прервал обработку».
    """
    return AsyncMock(return_value="handler-called")


@pytest.fixture
def middleware_data(telegram_user: TelegramUser) -> dict[str, Any]:
    """Словарь контекста, который aiogram передаёт в middleware."""
    return {"event_from_user": telegram_user}


# --------------------------------------------------------------------------- #
# Ограничение частоты и Redis
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def limiter() -> AsyncIterator[InMemoryRateLimiter]:
    """Ограничитель в памяти процесса.

    Реализует и ``RateLimiter``, и ``KeyGuard``, поэтому годится и для
    политики анти-флуда, и для защиты от двойных нажатий.
    """
    instance = InMemoryRateLimiter()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def anti_flood_config(settings: Settings) -> AntiFloodConfig:
    """Настройки эскалации наказаний из тестового окружения."""
    config = settings.rate_limit
    return AntiFloodConfig(
        violations_before_mute=config.violations_before_mute,
        violation_window=config.violation_window,
        mute_durations=config.mute_durations,
        warn_cooldown=config.warn_cooldown,
        mute_level_ttl=config.mute_level_ttl,
    )


@pytest.fixture
def flood_policy(
    limiter: InMemoryRateLimiter,
    anti_flood_config: AntiFloodConfig,
) -> AntiFloodPolicy:
    """Политика анти-флуда поверх локального хранилища."""
    return AntiFloodPolicy(limiter=limiter, guard=limiter, config=anti_flood_config)


@pytest.fixture
def message_rule(settings: Settings) -> RateLimitRule:
    """Правило частоты для сообщений (лимит занижен тестовым окружением)."""
    config = settings.rate_limit
    return RateLimitRule(
        limit=config.message_limit,
        window=config.message_window,
        burst=config.message_burst,
        scope="message",
    )


@pytest.fixture
def callback_rule(settings: Settings) -> RateLimitRule:
    """Правило частоты для нажатий на кнопки."""
    config = settings.rate_limit
    return RateLimitRule(
        limit=config.callback_limit,
        window=config.callback_window,
        burst=config.callback_burst,
        scope="callback",
    )


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Any]:
    """Клиент Redis: настоящий при ``TEST_REDIS_URL``, иначе ``fakeredis``.

    Ограничитель считает жетоны Lua-скриптом на стороне сервера, поэтому
    подмена клиента ``AsyncMock`` проверяла бы только факт вызова ``eval``.
    Нужен экземпляр, действительно исполняющий скрипт.
    """
    url = os.getenv("TEST_REDIS_URL", "").strip()
    if url:
        from redis.asyncio import Redis
        from redis.exceptions import RedisError

        client = Redis.from_url(url, decode_responses=True)
        try:
            await client.ping()
        except (RedisError, OSError) as exc:
            await _close_redis(client)
            pytest.skip(f"Redis недоступен по адресу {url}: {exc}")

        await client.flushdb()
        try:
            yield client
        finally:
            await client.flushdb()
            await _close_redis(client)
        return

    fakeredis = pytest.importorskip(
        "fakeredis",
        reason="Нужен fakeredis[lua] либо заданный TEST_REDIS_URL.",
    )
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await _close_redis(client)


async def _close_redis(client: Any) -> None:
    """Закрывает клиент вместе с пулом соединений.

    Одного ``aclose()`` мало: пул переживает клиент и остаётся висеть
    открытым, засоряя вывод предупреждениями о незакрытых ресурсах.
    """
    await client.aclose()
    await client.connection_pool.disconnect()


@pytest_asyncio.fixture
async def redis_limiter(redis_client: Any) -> AsyncIterator[Any]:
    """Ограничитель частоты поверх Redis."""
    from services.ratelimit.redis_limiter import RedisRateLimiter

    instance = RedisRateLimiter(redis_client, prefix="test")
    try:
        yield instance
    finally:
        await instance.close()


# --------------------------------------------------------------------------- #
# Внешние сервисы и прикладные объекты
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_parser() -> AsyncMock:
    """Парсер каналов без обращений к t.me.

    ``spec`` обязателен: без него опечатка в имени метода превратилась бы
    в молча работающую заглушку.
    """
    mock = AsyncMock(spec=TelegramWebParser)
    mock.fetch_posts.return_value = []
    return mock


@pytest.fixture
def make_parsed_post() -> Callable[..., ParsedPost]:
    """Строит результат парсинга одного сообщения канала."""
    counter = iter(range(1, 10_000))

    def _make(text: str = "Свежая новость дня.", **overrides: Any) -> ParsedPost:
        payload: dict[str, Any] = {
            "message_id": next(counter),
            "post_time": FROZEN_NOW,
            "text": text,
            "media": (),
        }
        payload.update(overrides)
        return ParsedPost(**payload)

    return _make


@pytest.fixture
def make_media_item() -> Callable[..., MediaItem]:
    """Строит описание вложения поста."""

    def _make(url: str = "https://cdn4.telegram-cdn.org/file.jpg", type_: str = "photo") -> MediaItem:
        return MediaItem(type=type_, url=url)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def mock_notifier() -> AsyncMock:
    """Нотификатор, всегда сообщающий об успешной доставке.

    Тест, которому нужен отказ, переопределяет ``send.return_value`` или
    ``send.side_effect`` результатом из :func:`delivery_result`.
    """
    mock = AsyncMock(spec=TelegramNotifier)
    mock.send.return_value = DeliveryResult(TELEGRAM_ID, DeliveryStatus.DELIVERED)
    return mock


@pytest.fixture
def delivery_result() -> Callable[..., DeliveryResult]:
    """Строит результат доставки уведомления с нужным статусом."""

    def _make(
        status: str = DeliveryStatus.DELIVERED,
        telegram_id: int = TELEGRAM_ID,
    ) -> DeliveryResult:
        return DeliveryResult(telegram_id, status)

    return _make


@pytest.fixture
def billing(uow: UnitOfWork, settings: Settings) -> BillingService:
    """Сервис биллинга поверх транзакции теста."""
    return BillingService(
        uow,
        invoice_ttl=timedelta(minutes=settings.billing.invoice_ttl_minutes),
        max_pending=settings.billing.max_pending_invoices,
    )


@pytest.fixture
def trial(uow: UnitOfWork, settings: Settings) -> TrialService:
    """Сервис пробного периода поверх транзакции теста."""
    return TrialService(uow, settings.trial)


@pytest.fixture
def make_contact(telegram_user: TelegramUser) -> Callable[..., Contact]:
    """Строит контакт, присланный кнопкой «Поделиться номером».

    По умолчанию контакт принадлежит отправителю — именно так выглядит
    ответ на ``request_contact``. Тест на чужой номер переопределяет
    ``user_id``.
    """

    def _make(
        phone_number: str = "+79001234567",
        *,
        user_id: int | None = TELEGRAM_ID,
        first_name: str = "Тест",
    ) -> Contact:
        return Contact(
            phone_number=phone_number,
            first_name=first_name,
            user_id=user_id,
        )

    return _make


@pytest.fixture
def labeled_price() -> Callable[..., LabeledPrice]:
    """Строит позицию счёта — для сверки параметров ``answer_invoice``."""

    def _make(label: str = "Pro на месяц", amount: int = 150) -> LabeledPrice:
        return LabeledPrice(label=label, amount=amount)

    return _make


# --------------------------------------------------------------------------- #
# Управление временем
# --------------------------------------------------------------------------- #


@pytest.fixture
def frozen_time() -> Iterator[Any]:
    """Останавливает часы на :data:`FROZEN_NOW`.

    ``real_asyncio=True`` принципиален: freezegun подменяет
    ``time.monotonic``, а на нём построены таймеры цикла событий — без
    этого флага любой ``asyncio.sleep`` внутри теста завис бы навсегда.
    Локальным хранилищам ограничителя подмена, наоборот, нужна: они тоже
    считают по ``time.monotonic``.

    Использование::

        async def test_expiry(frozen_time):
            frozen_time.tick(timedelta(hours=25))
    """
    from freezegun import freeze_time

    try:
        freezer = freeze_time(FROZEN_NOW, real_asyncio=True)
    except TypeError:  # pragma: no cover - freezegun старше 1.5
        freezer = freeze_time(FROZEN_NOW)

    with freezer as frozen:
        yield frozen


@pytest.fixture
def now() -> datetime:
    """Фиксированный «сейчас» для кода, принимающего момент времени явно."""
    return FROZEN_NOW
