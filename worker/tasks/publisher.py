"""Публикация отложенных новостей в целевые каналы.

Задача разбирает очередь ``scheduled_posts`` и отправляет записи в каналы
пользователей. Три решения определяют её устройство.

**Отправка идёт вне транзакции захвата.** Держать открытой транзакцию,
пока бот ходит в Telegram, значит удерживать блокировки строк на время
сетевого обмена: при flood control это секунды, а при недоступности —
минуты. Поэтому очередь захватывается, транзакция закрывается, и уже
затем выполняются отправки; результат записывается отдельной транзакцией.

**Статус меняется после факта, а не до него.** Пометить записи
«опубликовано» заранее было бы удобнее для идемпотентности, но упавший
между пометкой и отправкой воркер потерял бы публикацию навсегда.
Повторная отправка в худшем случае даёт дубль в канале, потеря — молчание
там, где пользователь ждал публикацию.

**Подписка проверяется на момент публикации, а не постановки в очередь.**
Между ними проходят часы, и подписка успевает закончиться. Публиковать по
истёкшей подписке — раздавать платную функцию бесплатно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from core.config import Settings
from core.logger import get_logger
from db.models import ScheduledPost
from db.uow import UnitOfWorkFactory
from services.i18n import TranslationManager
from services.notifier import TelegramNotifier
from worker.tasks.base import PeriodicTask, TaskResult

logger = get_logger(__name__)

#: Верхняя граница ожидания при flood control в пределах одного прогона.
_MAX_FLOOD_WAIT: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class _PublishJob:
    """Снимок записи очереди, пригодный для работы вне транзакции.

    Сущности ORM после закрытия сессии обращаться к связям не дают —
    у них ``lazy="raise"``, — поэтому всё нужное копируется значениями.
    """

    scheduled_id: int
    user_id: int
    chat_id: int
    channel_title: str
    channel_id: int
    source_channel: str
    source_message_id: int
    content: str
    caption: str | None


class PublishScheduledPostsTask(PeriodicTask):
    """Отправляет отложенные публикации, которым настало время."""

    name = "scheduled_post_publisher"

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        bot: Bot,
        notifier: TelegramNotifier,
        settings: Settings,
        translations: TranslationManager | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._bot = bot
        self._notifier = notifier
        self._settings = settings
        self._translations = translations or TranslationManager.from_directory()
        self._batch_size = settings.worker.batch_size
        self.interval = float(settings.worker.publish_interval)
        #: Каналы, отключённые в текущем проходе.
        self._just_disabled: set[int] = set()

    async def run(self) -> TaskResult:
        """Выполняет один проход по очереди публикаций."""
        now = datetime.now(tz=timezone.utc)

        jobs, cancelled = await self._claim(now)
        if not jobs:
            return TaskResult(details={"cancelled": cancelled}) if cancelled else TaskResult()

        published = 0
        failed = 0
        # Канал, потерявший права посреди прохода, не нужно долбить
        # остальными публикациями: очередь в него уже снята, а каждая
        # попытка — ещё один запрос к Telegram с заведомым отказом.
        disabled: set[int] = set()

        for job in jobs:
            if job.channel_id in disabled:
                logger.info(
                    "Публикация id=%s пропущена: канал id=%s отключён в этом же проходе",
                    job.scheduled_id, job.channel_id,
                )
                failed += 1
                continue

            outcome = await self._publish(job)
            if outcome:
                published += 1
            else:
                failed += 1
                if job.channel_id in self._just_disabled:
                    disabled.add(job.channel_id)
                    self._just_disabled.discard(job.channel_id)

        result = TaskResult(
            processed=len(jobs),
            succeeded=published,
            failed=failed,
            details={"cancelled": cancelled},
        )
        logger.info("Отложенные публикации: %s", result.describe())
        return result

    async def _claim(self, now: datetime) -> tuple[list[_PublishJob], int]:
        """Забирает очередь и отсеивает записи без действующей подписки.

        :param now: Текущий момент (timezone-aware).
        :return: Пара «задания к отправке, число отменённых записей».
        """
        jobs: list[_PublishJob] = []
        cancelled = 0

        async with self._uow_factory() as uow:
            claimed = await uow.scheduled.claim_due(now=now, limit=self._batch_size)

            for entry in claimed:
                channel = entry.target_channel
                post = entry.post

                if not await self._is_allowed(uow, entry):
                    entry.cancel()
                    entry.last_error = "Подписка не действует"
                    cancelled += 1
                    continue

                if not channel.is_publishable or channel.chat_id is None:
                    # Именно отмена, а не неудачная попытка: канал выключен
                    # или прав нет, и повторять это ещё дважды бессмысленно —
                    # состояние не изменится само.
                    entry.cancel()
                    entry.last_error = "Канал отключён или бот больше не администратор"
                    cancelled += 1
                    continue

                jobs.append(
                    _PublishJob(
                        scheduled_id=entry.id,
                        user_id=entry.user_id,
                        chat_id=channel.chat_id,
                        channel_title=channel.display_name,
                        channel_id=channel.id,
                        source_channel=post.channel_name,
                        source_message_id=post.message_id,
                        content=post.content,
                        caption=entry.caption,
                    )
                )

            await uow.commit()

        return jobs, cancelled

    async def _is_allowed(self, uow, entry: ScheduledPost) -> bool:
        """Проверяет, действует ли подписка владельца публикации."""
        subscription = await uow.subscriptions.get_live(entry.user_id)
        return subscription is not None

    async def _publish(self, job: _PublishJob) -> bool:
        """Публикует одну запись и сохраняет результат.

        :param job: Задание на публикацию.
        :return: ``True``, если запись отправлена.
        """
        # Лимит исходящих у бота общий, поэтому жетон берётся из того же
        # ведра, что и уведомления: своё ведро у каждой задачи означало бы
        # превышение суммарной скорости.
        await self._notifier.reserve_slot()

        try:
            message_id = await self._send(job)
        except TelegramRetryAfter as exc:
            # Flood control: запись остаётся в очереди и уйдёт следующим
            # проходом — ждать здесь значит задерживать остальные.
            delay = min(float(exc.retry_after), _MAX_FLOOD_WAIT)
            logger.warning(
                "Flood control при публикации id=%s: повтор через %.0f с",
                job.scheduled_id, delay,
            )
            await self._record_failure(job, f"Flood control, повтор через {delay:.0f} с")
            return False
        except TelegramForbiddenError as exc:
            # Бота выгнали из администраторов или удалили из канала.
            logger.warning("Публикация id=%s: доступ к каналу потерян: %s", job.scheduled_id, exc)
            await self._disable_channel(job, str(exc))
            return False
        except TelegramBadRequest as exc:
            logger.error("Публикация id=%s отклонена Telegram: %s", job.scheduled_id, exc)
            await self._record_failure(job, str(exc))
            return False
        except TelegramAPIError as exc:
            logger.warning("Публикация id=%s не удалась: %s", job.scheduled_id, exc)
            await self._record_failure(job, str(exc))
            return False

        async with self._uow_factory() as uow:
            entry = await uow.scheduled.get_by_id(job.scheduled_id)
            if entry is not None:
                entry.mark_published(message_id, datetime.now(tz=timezone.utc))
            await uow.channels.mark_published(job.channel_id, datetime.now(tz=timezone.utc))
            await uow.commit()

        logger.info(
            "Публикация id=%s отправлена в %s (message_id=%s)",
            job.scheduled_id, job.channel_title, message_id,
        )
        return True

    async def _send(self, job: _PublishJob) -> int:
        """Отправляет запись в канал.

        Сначала пробуется ``copy_message``: он переносит исходное
        оформление и медиа как есть, без пересборки альбома на нашей
        стороне. Но копирование возможно лишь пока бот видит исходное
        сообщение — канал может стать приватным, а запись удалить.
        Поэтому при отказе используется сохранённый текст: опубликовать
        новость без исходного оформления лучше, чем не опубликовать.

        :param job: Задание на публикацию.
        :return: Идентификатор отправленного сообщения.
        """
        try:
            copied = await self._bot.copy_message(
                chat_id=job.chat_id,
                from_chat_id=f"@{job.source_channel}",
                message_id=job.source_message_id,
                caption=job.caption,
            )
            return copied.message_id
        except TelegramBadRequest as exc:
            logger.info(
                "Копирование записи %s/%s не удалось (%s), отправляю текстом",
                job.source_channel, job.source_message_id, exc,
            )

        text = job.content or f"https://t.me/{job.source_channel}/{job.source_message_id}"
        if job.caption:
            text = f"{text}\n\n{job.caption}"

        sent = await self._bot.send_message(chat_id=job.chat_id, text=text)
        return sent.message_id

    async def _record_failure(self, job: _PublishJob, reason: str) -> None:
        """Учитывает неудачную попытку публикации."""
        async with self._uow_factory() as uow:
            entry = await uow.scheduled.get_by_id(job.scheduled_id)
            if entry is not None:
                entry.mark_failed(reason)
            await uow.commit()

    async def _disable_channel(self, job: _PublishJob, reason: str) -> None:
        """Отключает канал и снимает очередь, ведущую в него.

        Оставлять очередь в канал, куда бот больше не может писать, значит
        копить неудачные попытки на каждом проходе.
        """
        async with self._uow_factory() as uow:
            await uow.channels.set_bot_admin(job.channel_id, is_admin=False)
            # Потеря прав — не временный сбой: повторять её ещё дважды
            # бессмысленно, поэтому вся очередь в канал, включая текущую
            # запись, отменяется одним запросом.
            cancelled = await uow.scheduled.cancel_for_channel(
                job.channel_id, "Бот больше не администратор канала"
            )
            entry = await uow.scheduled.get_by_id(job.scheduled_id)
            if entry is not None:
                # У записи, на которой всё вскрылось, сохраняем точную
                # причину от Telegram — она полезнее общей формулировки.
                entry.last_error = reason[:500]
            await uow.commit()

        self._just_disabled.add(job.channel_id)
        logger.warning(
            "Канал id=%s помечен без прав, отменено публикаций: %d", job.channel_id, cancelled
        )
