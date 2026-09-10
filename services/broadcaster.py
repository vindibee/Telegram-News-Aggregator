"""Массовая рассылка сообщений подписчикам.

Три ограничения определяют устройство этого модуля.

Первое — лимит Bot API: примерно тридцать сообщений в секунду на бота
целиком. Превышение отзывается flood control не на рассылку, а на всего
бота, включая ответы в диалогах. Поэтому жетоны берутся из того же ведра,
что и обычные уведомления с публикациями: своё ведро у рассылки означало
бы, что суммарная скорость никем не ограничена.

Второе — размер базы. Список получателей не помещается в память целиком и
не должен: адресаты читаются страницами по курсору, а не одним запросом.
Соединение с базой при этом не удерживается на всё время рассылки —
страница читается, транзакция закрывается, и только потом идут отправки.

Третье — часть адресатов недоступна навсегда: бот заблокирован, чат
удалён, аккаунт деактивирован. Такие получатели помечаются в базе и
выпадают из следующих рассылок; без этого каждая новая рассылка тратила бы
на них жетоны общего лимита.

Состояние рассылки живёт в памяти процесса. Перезапуск бота её прерывает,
и продолжить с середины нельзя — для этого понадобилась бы таблица с
курсором, а её стоимость оправдана только при по-настоящему больших базах.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.logger import get_logger
from db.enums import BroadcastAudience
from db.repositories.user import Recipient
from db.uow import UnitOfWorkFactory
from services.notifier import TelegramNotifier

logger = get_logger(__name__)

#: Верхняя граница ожидания при flood control. Дольше ждать нет смысла:
#: рассылка не срочная, но и висеть на одном получателе полчаса не должна.
_MAX_FLOOD_WAIT: Final[float] = 60.0

#: Сколько раз повторять отправку одному получателю при временных сбоях.
_MAX_ATTEMPTS: Final[int] = 3

#: Через сколько обработанных получателей сообщать о прогрессе.
_PROGRESS_EVERY: Final[int] = 50

#: Сколько идентификаторов накапливать перед пометкой в базе.
_BLOCKED_FLUSH_SIZE: Final[int] = 100

#: Признаки навсегда недоступного адресата в тексте ошибки 400.
#:
#: Telegram отдаёт «чата нет» именно четырёхсотым кодом, а не 404,
#: поэтому ``TelegramNotFound`` здесь не срабатывает. Без разбора
#: текста такие получатели считались бы временным сбоем и попадали
#: бы в каждую следующую рассылку, тратя жетоны общего лимита.
_UNREACHABLE_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "chat not found",
        "user is deactivated",
        "peer_id_invalid",
        "user not found",
    }
)


class Delivery(StrEnum):
    """Итог отправки одному получателю."""

    #: Сообщение доставлено.
    SENT = "sent"
    #: Получатель недоступен навсегда — бот заблокирован либо чата нет.
    UNREACHABLE = "unreachable"
    #: Временный или неустранимый сбой; получатель остаётся в базе активным.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BroadcastContent:
    """Что именно рассылается.

    Сообщение не пересобирается, а копируется из исходного: администратор
    отправляет боту то, что хочет разослать, а ``copy_message`` переносит
    текст, медиа, подписи и форматирование как есть. Пересборка на нашей
    стороне означала бы отдельную ветку под каждый тип вложения и потерю
    оформления на первом же нестандартном случае.

    Копия, а не пересылка: у пересланного сообщения виден источник, то
    есть личный чат администратора.
    """

    #: Чат, где лежит образец (личный чат администратора).
    source_chat_id: int
    #: Сообщение-образец.
    source_message_id: int
    #: Кнопки под сообщением. У копии своя разметка — исходная не переносится.
    reply_markup: InlineKeyboardMarkup | None = None


@dataclass(slots=True)
class BroadcastReport:
    """Итог рассылки.

    Обновляется по ходу работы, поэтому пригоден и как отчёт о прогрессе.
    """

    audience: BroadcastAudience
    total: int
    started_at: datetime
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    finished_at: datetime | None = None
    cancelled: bool = False

    @property
    def processed(self) -> int:
        """Сколько получателей уже обработано."""
        return self.sent + self.blocked + self.failed

    @property
    def is_finished(self) -> bool:
        """Завершена ли рассылка."""
        return self.finished_at is not None

    @property
    def duration(self) -> float:
        """Длительность в секундах на текущий момент."""
        end = self.finished_at or datetime.now(tz=timezone.utc)
        return max((end - self.started_at).total_seconds(), 0.0)

    @property
    def rate(self) -> float:
        """Фактическая скорость отправки, сообщений в секунду."""
        elapsed = self.duration
        return self.processed / elapsed if elapsed > 0 else 0.0


#: Обратный вызов для отчёта о прогрессе.
ProgressCallback = Callable[[BroadcastReport], Awaitable[None]]


@dataclass(slots=True)
class _BlockedBatch:
    """Накопитель идентификаторов недоступных получателей."""

    ids: list[int] = field(default_factory=list)


class Broadcaster:
    """Рассылает сообщение выбранной аудитории.

    Экземпляр рассчитан на одну рассылку за раз: параллельные рассылки
    делили бы одно ведро жетонов и просто мешали бы друг другу, не
    ускоряя ни одну. Ограничение проверяется явно.
    """

    def __init__(
        self,
        bot: Bot,
        uow_factory: UnitOfWorkFactory,
        notifier: TelegramNotifier,
        *,
        workers: int = 8,
        page_size: int = 500,
    ) -> None:
        """
        :param bot: Клиент Bot API.
        :param uow_factory: Фабрика единиц работы — на каждую страницу своя.
        :param notifier: Владелец общего ведра исходящих сообщений.
        :param workers: Сколько отправок идёт одновременно.
        :param page_size: Размер страницы выборки адресатов.
        :raises ValueError: Некорректные параметры.
        """
        if workers < 1:
            raise ValueError(f"Число отправщиков должно быть не меньше 1, получено: {workers}")
        if page_size < 1:
            raise ValueError(f"Размер страницы должен быть не меньше 1, получено: {page_size}")

        self._bot = bot
        self._uow_factory = uow_factory
        self._notifier = notifier
        self._workers = workers
        self._page_size = page_size
        self._running = False
        self._stop: asyncio.Event | None = None

    @property
    def is_running(self) -> bool:
        """Идёт ли рассылка прямо сейчас."""
        return self._running

    def request_stop(self) -> bool:
        """Просит текущую рассылку остановиться.

        Остановка мягкая: начатые отправки доводятся до конца, новые не
        начинаются. Обрывать их принудительно нельзя — часть сообщений
        уже ушла бы в Telegram, и отчёт разошёлся бы с реальностью.

        :return: ``True``, если было что останавливать.
        """
        if not self._running or self._stop is None:
            return False
        self._stop.set()
        logger.info("Запрошена остановка рассылки")
        return True

    async def count_audience(self, audience: BroadcastAudience, now: datetime) -> int:
        """Считает получателей, не начиная рассылку.

        :param audience: Целевая группа.
        :param now: Момент выборки (timezone-aware).
        :return: Количество адресатов.
        """
        async with self._uow_factory() as uow:
            return await uow.users.count_audience(audience, now)

    async def run(
        self,
        content: BroadcastContent,
        audience: BroadcastAudience,
        *,
        now: datetime,
        on_progress: ProgressCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> BroadcastReport:
        """Выполняет рассылку.

        :param content: Что рассылать.
        :param audience: Кому рассылать.
        :param now: Момент старта (timezone-aware); по нему отбирается аудитория.
        :param on_progress: Обратный вызов для отчёта о прогрессе.
        :param cancel: Событие остановки — рассылка прекращается на ближайшем получателе.
        :return: Итоговый отчёт.
        :raises RuntimeError: Рассылка уже идёт.
        """
        if self._running:
            raise RuntimeError("Рассылка уже выполняется.")

        total = await self.count_audience(audience, now)
        report = BroadcastReport(
            audience=audience, total=total, started_at=datetime.now(tz=timezone.utc)
        )

        if total == 0:
            report.finished_at = datetime.now(tz=timezone.utc)
            logger.info("Рассылка %s: получателей нет", audience)
            return report

        self._running = True
        stop = cancel or asyncio.Event()
        self._stop = stop
        # Очередь ограничена: без предела продюсер вычитал бы всю базу в
        # память, ради чего постраничная выборка и затевалась.
        queue: asyncio.Queue[Recipient | None] = asyncio.Queue(maxsize=self._page_size)
        blocked = _BlockedBatch()
        lock = asyncio.Lock()

        logger.info(
            "Рассылка %s начата: получателей %d, отправщиков %d",
            audience, total, self._workers,
        )

        producer = asyncio.create_task(
            self._produce(queue, audience, now, stop), name="broadcast-producer"
        )
        consumers = [
            asyncio.create_task(
                self._consume(queue, content, report, blocked, lock, stop, on_progress),
                name=f"broadcast-worker-{index}",
            )
            for index in range(self._workers)
        ]

        try:
            await asyncio.gather(producer, *consumers)
        finally:
            self._running = False
            self._stop = None
            # Остаток помечается всегда, даже после отмены или сбоя:
            # получатели, ответившие 403, недоступны независимо от того,
            # чем закончилась рассылка.
            await self._flush_blocked(blocked, lock)
            report.finished_at = datetime.now(tz=timezone.utc)
            report.cancelled = stop.is_set() and report.processed < report.total

        logger.info(
            "Рассылка %s завершена: отправлено %d, заблокировали %d, ошибок %d "
            "за %.1f с (%.1f сообщ./с)%s",
            audience, report.sent, report.blocked, report.failed,
            report.duration, report.rate,
            ", остановлена вручную" if report.cancelled else "",
        )

        if on_progress is not None:
            await self._report(on_progress, report)

        return report

    # ------------------------------------------------------------- внутреннее
    async def _produce(
        self,
        queue: asyncio.Queue[Recipient | None],
        audience: BroadcastAudience,
        now: datetime,
        stop: asyncio.Event,
    ) -> None:
        """Читает адресатов страницами и складывает их в очередь.

        Транзакция открывается на каждую страницу и сразу закрывается:
        держать соединение открытым всю рассылку значит занимать место в
        пуле, которое нужно живым запросам пользователей.
        """
        after_id = 0
        try:
            while not stop.is_set():
                async with self._uow_factory() as uow:
                    page = await uow.users.fetch_audience_page(
                        audience, now=now, after_id=after_id, limit=self._page_size
                    )

                if not page:
                    break

                for recipient in page:
                    if stop.is_set():
                        break
                    await queue.put(recipient)

                after_id = page[-1].id
                if len(page) < self._page_size:
                    break
        except Exception:
            logger.exception("Сбой выборки адресатов рассылки, отправка прекращена")
            stop.set()
        finally:
            # Стоп-метка каждому отправщику: иначе они останутся ждать
            # очередь, которая больше не пополнится.
            for _ in range(self._workers):
                await queue.put(None)

    async def _consume(
        self,
        queue: asyncio.Queue[Recipient | None],
        content: BroadcastContent,
        report: BroadcastReport,
        blocked: _BlockedBatch,
        lock: asyncio.Lock,
        stop: asyncio.Event,
        on_progress: ProgressCallback | None,
    ) -> None:
        """Забирает адресатов из очереди и отправляет им сообщение.

        Тело цикла защищено от любых исключений намеренно: упавший
        отправщик перестал бы вычерпывать очередь, продюсер навсегда завис
        бы на попытке положить в неё следующего получателя, и рассылка
        остановилась бы без единого сообщения об ошибке.
        """
        while True:
            recipient = await queue.get()
            if recipient is None:
                return

            if stop.is_set():
                # Очередь всё равно нужно вычерпать до стоп-метки, иначе
                # продюсер зависнет на put() в заполненную очередь.
                continue

            try:
                outcome = await self._deliver(content, recipient, stop)
            except Exception:
                logger.exception("Непредвиденный сбой отправки %s", recipient.telegram_id)
                outcome = Delivery.FAILED

            if outcome is Delivery.SENT:
                report.sent += 1
            elif outcome is Delivery.UNREACHABLE:
                report.blocked += 1
                async with lock:
                    blocked.ids.append(recipient.id)
                    should_flush = len(blocked.ids) >= _BLOCKED_FLUSH_SIZE
                if should_flush:
                    await self._flush_blocked(blocked, lock)
            else:
                report.failed += 1

            if on_progress is not None and report.processed % _PROGRESS_EVERY == 0:
                await self._report(on_progress, report)

    async def _deliver(
        self,
        content: BroadcastContent,
        recipient: Recipient,
        stop: asyncio.Event,
    ) -> Delivery:
        """Отправляет сообщение одному получателю.

        Ошибки Telegram не выпускаются наружу: сбой у одного адресата не
        должен останавливать рассылку остальным.

        :param content: Что отправляем.
        :param recipient: Кому отправляем.
        :param stop: Событие остановки рассылки.
        :return: Итог доставки.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            # Жетон берётся перед каждой попыткой, включая повторные:
            # повтор — такое же обращение к Bot API, как и первая отправка.
            await self._notifier.reserve_slot()

            try:
                await self._bot.copy_message(
                    chat_id=recipient.telegram_id,
                    from_chat_id=content.source_chat_id,
                    message_id=content.source_message_id,
                    reply_markup=content.reply_markup,
                )
            except TelegramForbiddenError:
                logger.debug("Получатель %s заблокировал бота", recipient.telegram_id)
                return Delivery.UNREACHABLE
            except TelegramNotFound:
                # Чат не найден: аккаунт удалён или диалог с ботом никогда
                # не начинался. Повторять так же бесполезно, как и при 403.
                logger.debug("Чат получателя %s не найден", recipient.telegram_id)
                return Delivery.UNREACHABLE
            except TelegramRetryAfter as exc:
                delay = min(float(exc.retry_after), _MAX_FLOOD_WAIT)
                logger.warning(
                    "Flood control при рассылке (попытка %d из %d): пауза %.1f с",
                    attempt, _MAX_ATTEMPTS, delay,
                )
                await self._sleep_or_stop(delay, stop)
                if stop.is_set():
                    return Delivery.FAILED
            except TelegramBadRequest as exc:
                if _is_unreachable(exc):
                    logger.debug(
                        "Получатель %s недоступен: %s", recipient.telegram_id, exc
                    )
                    return Delivery.UNREACHABLE
                # Отклонено разбором запроса: пересобирать нечего, у всех
                # получателей сообщение одно и то же.
                logger.error("Рассылка отклонена Telegram для %s: %s", recipient.telegram_id, exc)
                return Delivery.FAILED
            except TelegramAPIError as exc:
                logger.warning(
                    "Ошибка отправки %s (попытка %d из %d): %s",
                    recipient.telegram_id, attempt, _MAX_ATTEMPTS, exc,
                )
                if attempt == _MAX_ATTEMPTS:
                    return Delivery.FAILED
                await self._sleep_or_stop(min(2 ** (attempt - 1), 5), stop)
                if stop.is_set():
                    return Delivery.FAILED
            else:
                return Delivery.SENT

        return Delivery.FAILED

    @staticmethod
    async def _sleep_or_stop(delay: float, stop: asyncio.Event) -> None:
        """Ждёт паузу, но прерывается по команде остановки.

        Простой ``sleep`` заставил бы отмену ждать до минуты на каждом
        отправщике, попавшем под flood control.
        """
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return

    async def _flush_blocked(self, blocked: _BlockedBatch, lock: asyncio.Lock) -> None:
        """Переносит накопленные идентификаторы недоступных в базу."""
        async with lock:
            pending = blocked.ids
            blocked.ids = []

        if not pending:
            return

        try:
            async with self._uow_factory() as uow:
                await uow.users.mark_bot_blocked_bulk(pending)
                await uow.commit()
        except Exception:
            # Пометка — оптимизация, а не смысл рассылки: её потеря стоит
            # лишних попыток в следующий раз, но ронять рассылку не должна.
            logger.exception("Не удалось пометить %d недоступных получателей", len(pending))

    @staticmethod
    async def _report(on_progress: ProgressCallback, report: BroadcastReport) -> None:
        """Вызывает обратный вызов прогресса, гася его ошибки."""
        try:
            await on_progress(report)
        except Exception:
            logger.exception("Обратный вызов прогресса рассылки завершился ошибкой")


def _is_unreachable(exc: TelegramBadRequest) -> bool:
    """Означает ли ошибка 400, что адресата больше не существует.

    :param exc: Ошибка от Bot API.
    :return: Повторять отправку этому получателю бессмысленно.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _UNREACHABLE_MARKERS)


def parse_buttons(raw: str) -> InlineKeyboardMarkup | None:
    """Разбирает кнопки, заданные администратором построчно.

    Формат одной строки — ``Текст | https://example.com``. Разделителем
    выбрана вертикальная черта, а не дефис: дефис часто встречается и в
    подписи кнопки, и в адресе.

    :param raw: Ввод администратора.
    :return: Клавиатура либо ``None``, если кнопок нет.
    :raises ValueError: Строка не соответствует формату.
    """
    rows: list[list[InlineKeyboardButton]] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        if "|" not in line:
            raise ValueError(f"Строка «{line}» не содержит разделителя «|».")

        title, _, url = line.partition("|")
        title, url = title.strip(), url.strip()

        if not title:
            raise ValueError(f"В строке «{line}» пустая подпись кнопки.")
        if not url.startswith(("http://", "https://", "tg://")):
            raise ValueError(f"Адрес «{url}» должен начинаться с http://, https:// или tg://.")

        rows.append([InlineKeyboardButton(text=title, url=url)])

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def audience_from_value(value: str) -> BroadcastAudience:
    """Восстанавливает аудиторию из строки callback-данных.

    :param value: Значение перечисления.
    :return: Аудитория.
    :raises ValueError: Неизвестное значение.
    """
    try:
        return BroadcastAudience(value)
    except ValueError as exc:
        raise ValueError(f"Неизвестная аудитория рассылки: {value!r}") from exc


__all__ = [
    "BroadcastContent",
    "BroadcastReport",
    "Broadcaster",
    "Delivery",
    "ProgressCallback",
    "audience_from_value",
    "parse_buttons",
]
