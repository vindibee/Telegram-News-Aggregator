"""Точка входа: композиционный корень приложения.

Здесь и только здесь создаются «долгоживущие» объекты (бот, HTTP-сессия,
пул соединений с БД) и связываются между собой. Все остальные модули
получают зависимости извне и не создают их сами.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage

from core.config import ConfigError, Settings, load_settings
from core.logger import get_logger, setup_logging
from db.database import Database, DatabaseNotReadyError
from db.uow import UnitOfWorkFactory
from services.billing import CryptoBotClient
from services.dedup import DedupConfig
from services.dedup_index import DedupIndex, build_dedup_index
from services.media import MediaDownloader
from services.notifier import TelegramNotifier
from services.tracker import ClickCounter
from services.parser import TelegramWebParser
from services.ratelimit.base import RateLimitBackend
from services.i18n import LanguageCache, TranslationManager, build_language_cache
from services.ratelimit.factory import build_backend, build_policy, build_rules
from tg_bot.errors import register_error_handlers
from tg_bot.handlers import router
from tg_bot.middlewares import (
    DependenciesMiddleware,
    I18nMiddleware,
    SingleFlightMiddleware,
    ThrottlingMiddleware,
    UserContextMiddleware,
)
from tg_bot.views import PostRenderer
from web import build_web_app, start_web_app
from web.redirect_app import setup_redirect_routes

logger = get_logger(__name__)

#: Верхняя граница одновременных TCP-соединений к t.me и CDN Telegram.
_CONNECTION_LIMIT = 30


def build_http_session(settings: Settings) -> aiohttp.ClientSession:
    """Создаёт общую HTTP-сессию.

    Одна сессия на всё приложение — это переиспользование соединений и
    единые таймауты; создание сессии на каждый запрос убивало бы keep-alive.
    """
    connector = aiohttp.TCPConnector(limit=_CONNECTION_LIMIT, ttl_dns_cache=300)
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=settings.parser.media_timeout),
        headers={
            "User-Agent": settings.parser.user_agent,
            "Accept-Language": "ru,en;q=0.9",
        },
    )


def build_dedup_config(settings: Settings) -> DedupConfig:
    """Переносит настройки дедупликации из окружения в параметры сервиса."""
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


def _build_redis_client(settings: Settings) -> object | None:
    """Создаёт клиент Redis для очереди переходов.

    Отдельный клиент, а не общий с ограничителем частоты: очередь
    переходов может расти пачками, и делить с ней пул соединений
    анти-флуда, от которого зависит отзывчивость бота, не стоит.
    """
    if not settings.redis.enabled:
        return None

    from redis.asyncio import Redis

    return Redis.from_url(settings.redis.url, decode_responses=True)


def build_crypto_client(
    settings: Settings,
    http_session: aiohttp.ClientSession,
) -> CryptoBotClient | None:
    """Создаёт клиент CryptoBot, если оплата криптовалютой настроена.

    Отсутствие токена — не ошибка: бот вполне работает на одних звёздах,
    и требовать регистрации в стороннем сервисе ради локального запуска
    было бы неуместно.

    :param settings: Настройки приложения.
    :param http_session: Общая HTTP-сессия.
    :return: Клиент либо ``None``.
    """
    if not settings.crypto.enabled:
        logger.info("CRYPTO_BOT_TOKEN не задан: оплата криптовалютой отключена.")
        return None

    logger.info("Оплата криптовалютой включена (%s).", settings.crypto.api_url)
    return CryptoBotClient(
        http_session,
        settings.crypto.token,
        api_url=settings.crypto.api_url,
        timeout=settings.crypto.request_timeout,
    )


def build_fsm_storage(settings: Settings) -> BaseStorage:
    """Создаёт хранилище состояний FSM.

    В памяти состояние теряется при перезапуске и не разделяется между
    репликами, поэтому в продакшене используется Redis.
    """
    if not settings.redis.enabled:
        logger.warning("REDIS_URL не задан: состояния FSM хранятся в памяти процесса.")
        return MemoryStorage()

    from aiogram.fsm.storage.redis import RedisStorage

    logger.info("Состояния FSM хранятся в Redis.")
    return RedisStorage.from_url(settings.redis.url)


def build_dispatcher(
    settings: Settings,
    database: Database,
    http_session: aiohttp.ClientSession,
    limiter: RateLimitBackend,
    storage: BaseStorage,
    translations: TranslationManager,
    language_cache: LanguageCache,
    dedup_index: DedupIndex,
    crypto_client: CryptoBotClient | None = None,
) -> Dispatcher:
    """Собирает диспетчер со всеми зависимостями, middleware и хендлерами."""
    parser = TelegramWebParser(http_session, settings.parser)
    downloader = MediaDownloader(http_session, settings.parser)
    rules = build_rules(settings.rate_limit)
    policy = build_policy(limiter, settings.rate_limit)

    dispatcher = Dispatcher(storage=storage)
    # Данные уровня приложения доступны хендлерам как обычные аргументы.
    dispatcher["settings"] = settings
    dispatcher["renderer"] = PostRenderer(downloader, settings.display_timezone)
    dispatcher["limiter"] = limiter
    dispatcher["translations"] = translations
    dispatcher["language_cache"] = language_cache

    # outer_middleware срабатывает до фильтров, поэтому сессия БД доступна и им.
    dispatcher.update.outer_middleware(
        DependenciesMiddleware(
            UnitOfWorkFactory(database.session_factory),
            parser,
            settings.parser,
            invoice_ttl=timedelta(minutes=settings.billing.invoice_ttl_minutes),
            dedup_config=build_dedup_config(settings),
            dedup_index=dedup_index,
            trial_config=settings.trial,
            crypto_client=crypto_client,
            crypto_invoice_ttl=timedelta(minutes=settings.crypto.invoice_ttl_minutes),
        )
    )
    # Строго после зависимостей: регистрация пользователя работает в уже
    # открытой транзакции.
    dispatcher.update.outer_middleware(UserContextMiddleware())
    # После пользовательского контекста: язык берётся из уже загруженной
    # строки, а в базу приходится идти только при промахе кэша у тех
    # обновлений, которые до регистрации не доходят.
    dispatcher.update.outer_middleware(I18nMiddleware(translations, language_cache))

    if settings.rate_limit.enabled:
        # Внутренние middleware наблюдателей: только здесь известен выбранный
        # хендлер, а значит и его флаги с индивидуальным лимитом.
        dispatcher.message.middleware(ThrottlingMiddleware(policy, rules.message))
        dispatcher.callback_query.middleware(ThrottlingMiddleware(policy, rules.callback))
        # Защита от двойных нажатий ставится после троттлинга: блокировку
        # имеет смысл брать только для запроса, который реально исполнится.
        dispatcher.callback_query.middleware(
            SingleFlightMiddleware(limiter, ttl=settings.rate_limit.single_flight_ttl)
        )
    else:
        logger.warning("Ограничение частоты отключено настройкой RATE_LIMIT_ENABLED.")

    register_error_handlers(dispatcher)
    dispatcher.include_router(router)
    return dispatcher


async def run() -> None:
    """Поднимает приложение и корректно освобождает ресурсы при остановке."""
    settings = load_settings()
    setup_logging(settings.log_level)

    logger.info("Инициализация приложения…")
    database = Database(settings.db)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    http_session = build_http_session(settings)
    limiter = build_backend(settings.redis)
    storage = build_fsm_storage(settings)
    # Каталоги читаются на старте: ошибка в файле перевода должна
    # ронять запуск, а не всплывать в чате у пользователя.
    translations = TranslationManager.from_directory()
    language_cache = build_language_cache(settings.redis)
    dedup_index = build_dedup_index(settings.redis, settings.dedup.lookback_hours)
    click_counter = ClickCounter(_build_redis_client(settings), prefix=settings.redis.prefix)
    crypto_client = build_crypto_client(settings, http_session)

    try:
        # Схема БД разворачивается миграциями Alembic ("alembic upgrade head"),
        # а не приложением: приложение, меняющее схему на старте, ломает
        # деплой при нескольких репликах и не даёт откатиться.
        # Схема проверяется до старта поллинга: иначе бот «успешно»
        # запустится с недоступной базой и будет отвечать ошибкой на
        # каждое сообщение, а причина останется невидимой.
        await database.check_ready()

        dispatcher = build_dispatcher(
            settings,
            database,
            http_session,
            limiter,
            storage,
            translations,
            language_cache,
            dedup_index,
            crypto_client,
        )

        # HTTP-сервер поднимается, если он кому-то нужен: приёмнику
        # вебхуков оплаты или редиректу коротких ссылок. Открывать порт
        # «на всякий случай» незачем.
        if crypto_client is not None or settings.tracker.enabled:
            uow_factory = UnitOfWorkFactory(database.session_factory)
            web_app = build_web_app(
                settings=settings,
                uow_factory=uow_factory,
                notifier=TelegramNotifier(bot, limiter),
                translations=translations,
            )
            if settings.tracker.enabled:
                setup_redirect_routes(
                    web_app,
                    settings=settings,
                    uow_factory=uow_factory,
                    counter=click_counter,
                )
            web_runner = await start_web_app(web_app, settings.crypto)

        me = await bot.get_me()
        logger.info("Бот @%s запущен и готов к работе.", me.username)

        # Накопленные за простой апдейты не обрабатываем: они уже неактуальны.
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        logger.info("Остановка: освобождаю ресурсы…")
        if web_runner is not None:
            await web_runner.cleanup()
        await http_session.close()
        await bot.session.close()
        await limiter.close()
        await language_cache.close()
        await dedup_index.close()
        await storage.close()
        await database.dispose()
        logger.info("Приложение остановлено.")


def main() -> int:
    """CLI-обёртка: превращает исключения запуска в понятный код возврата."""
    try:
        asyncio.run(run())
    except ConfigError as exc:
        setup_logging("INFO")
        logger.critical("Ошибка конфигурации: %s", exc)
        return 2
    except DatabaseNotReadyError as exc:
        setup_logging("INFO")
        logger.critical("База данных не готова: %s", exc)
        return 3
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
    except Exception as exc:  # noqa: BLE001 - последний рубеж перед падением процесса
        setup_logging("INFO")
        logger.critical("Фатальная ошибка: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
