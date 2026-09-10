"""Точка входа фонового воркера.

Отдельный процесс, а не поток внутри бота. Причины практические: у
воркера свой профиль нагрузки и свои требования к перезапуску, его можно
масштабировать независимо, а падение фоновой задачи не должно ронять
обработку сообщений. Схему БД воркер не мигрирует — это делает бот.

Запуск: ``python -m worker``
"""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import suppress

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from core.config import ConfigError, Settings, load_settings
from core.logger import get_logger, setup_logging
from db.database import Database, DatabaseNotReadyError
from db.uow import UnitOfWorkFactory
from services.i18n import TranslationManager
from services.tracker import ClickCounter
from services.notifier import TelegramNotifier
from services.ratelimit.base import RateLimitBackend
from services.ratelimit.factory import build_backend
from worker.runner import TaskRunner
from worker.tasks import (
    ClickFlushTask,
    ExpiryNotificationTask,
    PublishScheduledPostsTask,
    StaleInvoiceCleanupTask,
    SubscriptionExpirationTask,
)

logger = get_logger(__name__)


def build_runner(
    settings: Settings,
    database: Database,
    bot: Bot,
    notifier: TelegramNotifier,
    counter: ClickCounter,
) -> TaskRunner:
    """Собирает планировщик со всеми задачами.

    Каталоги переводов загружаются один раз и передаются задачам:
    уведомления уходят на языке получателя, а читать файлы в каждой
    задаче отдельно незачем.
    """
    uow_factory = UnitOfWorkFactory(database.session_factory)
    translations = TranslationManager.from_directory()
    return TaskRunner(
        [
            ExpiryNotificationTask(uow_factory, notifier, settings, translations),
            SubscriptionExpirationTask(uow_factory, notifier, settings, translations),
            StaleInvoiceCleanupTask(uow_factory, settings),
            PublishScheduledPostsTask(uow_factory, bot, notifier, settings, translations),
            ClickFlushTask(uow_factory, counter, settings),
        ]
    )


def _build_redis(settings: Settings) -> object | None:
    """Создаёт клиент Redis для очереди переходов.

    Без Redis переходы не буферизуются вовсе: редирект-сервер их
    просто не примет, и переносить будет нечего.
    """
    if not settings.redis.enabled:
        logger.info("REDIS_URL не задан: перенос переходов отключён.")
        return None

    from redis.asyncio import Redis

    return Redis.from_url(settings.redis.url, decode_responses=True)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Переводит SIGTERM и SIGINT в событие остановки.

    ``docker stop`` присылает SIGTERM: без обработчика процесс был бы убит
    через десять секунд посреди транзакции.
    """
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler для SIGTERM —
            # там остановка приходит через KeyboardInterrupt.
            logger.debug("Обработчик %s недоступен на этой платформе", signal_name)


async def run() -> None:
    """Поднимает воркер и работает до сигнала остановки."""
    settings = load_settings()
    setup_logging(settings.log_level)

    logger.info("Инициализация воркера…")
    database = Database(settings.db)
    limiter: RateLimitBackend = build_backend(settings.redis)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    notifier = TelegramNotifier(bot, limiter)

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    # Проверка до создания планировщика: задачи ходят в базу по расписанию,
    # и без неё первая ошибка всплыла бы только через интервал и лишь в логе
    # задачи. Останавливать здесь ещё нечего, поэтому она вне try.
    try:
        await database.check_ready()
    except DatabaseNotReadyError:
        await bot.session.close()
        await limiter.close()
        await database.dispose()
        raise

    counter = ClickCounter(_build_redis(settings), prefix=settings.redis.prefix)
    runner = build_runner(settings, database, bot, notifier, counter)
    runner.schedule()

    try:
        await runner.start()
        logger.info("Воркер запущен, ожидаю расписание.")
        await stop_event.wait()
        logger.info("Получен сигнал остановки.")
    finally:
        await runner.stop()
        await bot.session.close()
        await limiter.close()
        await database.dispose()
        logger.info("Воркер остановлен.")


def main() -> int:
    """CLI-обёртка с понятными кодами возврата."""
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
        logger.info("Воркер остановлен пользователем.")
    except Exception as exc:  # noqa: BLE001 - последний рубеж перед падением процесса
        setup_logging("INFO")
        logger.critical("Фатальная ошибка воркера: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        sys.exit(main())
