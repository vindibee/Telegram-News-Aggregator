"""Отложенные публикации в целевые каналы."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import FINAL_SCHEDULED_STATUSES, ScheduledPostStatus, pg_enum
from db.exceptions import InvalidStateTransitionError
from db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from db.models.channel import UserChannel
    from db.models.post import Post
    from db.models.user import User

logger = get_logger(__name__)

#: Сколько раз повторять публикацию при временных сбоях.
MAX_PUBLISH_ATTEMPTS: Final[int] = 3

#: Допустимые переходы состояния публикации.
_TRANSITIONS: dict[ScheduledPostStatus, frozenset[ScheduledPostStatus]] = {
    ScheduledPostStatus.PENDING: frozenset(
        {
            ScheduledPostStatus.PUBLISHED,
            ScheduledPostStatus.FAILED,
            ScheduledPostStatus.CANCELLED,
        }
    ),
    # Из ошибки можно вернуться в очередь: сбой публикации чаще всего
    # временный (flood control, канал недоступен минуту).
    ScheduledPostStatus.FAILED: frozenset(
        {ScheduledPostStatus.PENDING, ScheduledPostStatus.CANCELLED}
    ),
    ScheduledPostStatus.PUBLISHED: frozenset(),
    ScheduledPostStatus.CANCELLED: frozenset(),
}


class ScheduledPost(Base, IdMixin, TimestampMixin):
    """Запланированная отправка поста в канал пользователя.

    Очередь разбирается воркером через ``FOR UPDATE SKIP LOCKED`` по
    индексу ``(status, publish_at)``, поэтому несколько реплик не
    опубликуют одну запись дважды.

    Идентификатор отправленного сообщения сохраняется: без него нельзя ни
    отредактировать публикацию, ни удалить её, ни отличить «отправлено»
    от «кажется, отправлено».
    """

    __tablename__ = "scheduled_posts"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    post_id: Mapped[int] = mapped_column(
        BigInteger,
        # RESTRICT: пост нельзя удалить, пока он стоит в очереди на
        # публикацию — иначе воркер получил бы запись без содержимого.
        ForeignKey("posts.id", ondelete="RESTRICT"),
        nullable=False,
    )

    publish_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[ScheduledPostStatus] = mapped_column(
        pg_enum(ScheduledPostStatus, "scheduled_post_status"),
        nullable=False,
        default=ScheduledPostStatus.PENDING,
        server_default=ScheduledPostStatus.PENDING.value,
    )

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: ``message_id`` опубликованного сообщения в целевом канале.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Подпись, добавляемая к посту при публикации (реклама, дисклеймер).
    caption: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="scheduled_posts", lazy="raise")
    target_channel: Mapped[UserChannel] = relationship("UserChannel", lazy="raise")
    post: Mapped[Post] = relationship("Post", lazy="raise")

    __table_args__ = (
        # Один и тот же пост не планируется в один канал дважды: повторный
        # тап по кнопке «Опубликовать» — обычное дело.
        UniqueConstraint("target_channel_id", "post_id", name="uq_scheduled_posts_channel_post"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "status <> 'published' OR (published_at IS NOT NULL AND message_id IS NOT NULL)",
            name="published_has_message",
        ),
        # Основной запрос воркера: «что пора публиковать». Частичный индекс
        # покрывает только очередь и не растёт вместе с архивом.
        Index(
            "ix_scheduled_posts_queue",
            "publish_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_scheduled_posts_user_id_publish_at", "user_id", "publish_at"),
        Index("ix_scheduled_posts_target_channel_id", "target_channel_id"),
        Index("ix_scheduled_posts_post_id", "post_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<ScheduledPost id={self.id} post_id={self.post_id} "
            f"channel_id={self.target_channel_id} status={self.status}>"
        )

    @property
    def is_final(self) -> bool:
        """Достигнуто ли состояние, из которого нет переходов."""
        return self.status in FINAL_SCHEDULED_STATUSES

    @property
    def is_exhausted(self) -> bool:
        """Исчерпаны ли попытки публикации."""
        return self.attempts >= MAX_PUBLISH_ATTEMPTS

    def is_due(self, moment: datetime) -> bool:
        """Пора ли публиковать.

        :param moment: Момент проверки (timezone-aware).
        :return: ``True``, если запись ждёт публикации и срок наступил.
        """
        return self.status is ScheduledPostStatus.PENDING and self.publish_at <= moment

    def _transition_to(self, target: ScheduledPostStatus) -> None:
        """Переводит запись в новое состояние.

        :param target: Целевое состояние.
        :raises InvalidStateTransitionError: Переход запрещён правилами.
        """
        if target not in _TRANSITIONS[self.status]:
            logger.warning(
                "Запрещённый переход публикации id=%s: %s -> %s", self.id, self.status, target
            )
            raise InvalidStateTransitionError("ScheduledPost", self.status, target)
        self.status = target

    def mark_published(self, message_id: int, moment: datetime) -> bool:
        """Фиксирует успешную публикацию.

        Повторный вызов безопасен и возвращает ``False``: воркер может
        получить подтверждение отправки после того, как уже записал его.

        :param message_id: Идентификатор сообщения в канале.
        :param moment: Момент публикации (timezone-aware).
        :return: ``True``, если состояние изменилось этим вызовом.
        :raises ValueError: Некорректный идентификатор сообщения.
        :raises InvalidStateTransitionError: Публикация была отменена.
        """
        if message_id <= 0:
            raise ValueError(f"Идентификатор сообщения должен быть положительным: {message_id}")
        if self.status is ScheduledPostStatus.PUBLISHED:
            return False

        self._transition_to(ScheduledPostStatus.PUBLISHED)
        self.message_id = message_id
        self.published_at = moment
        self.last_error = None
        logger.info("Публикация id=%s отправлена, message_id=%s", self.id, message_id)
        return True

    def mark_failed(self, reason: str) -> None:
        """Учитывает неудачную попытку публикации.

        Счётчик увеличивается всегда, а состояние меняется только когда
        попытки исчерпаны: иначе одна сетевая ошибка навсегда убирала бы
        запись из очереди.

        :param reason: Текст ошибки.
        """
        self.attempts += 1
        self.last_error = reason.strip()[:500]

        if self.is_exhausted and self.status is not ScheduledPostStatus.FAILED:
            self._transition_to(ScheduledPostStatus.FAILED)
            logger.error(
                "Публикация id=%s провалена после %d попыток: %s",
                self.id, self.attempts, self.last_error,
            )
            return

        logger.warning(
            "Попытка %d публикации id=%s не удалась: %s", self.attempts, self.id, self.last_error
        )

    def reschedule(self, publish_at: datetime) -> None:
        """Возвращает запись в очередь на новое время.

        :param publish_at: Новый момент публикации (timezone-aware).
        :raises InvalidStateTransitionError: Запись уже опубликована или отменена.
        """
        if self.status is not ScheduledPostStatus.PENDING:
            self._transition_to(ScheduledPostStatus.PENDING)
        self.publish_at = publish_at
        self.attempts = 0
        logger.info("Публикация id=%s перенесена на %s", self.id, publish_at)

    def cancel(self) -> bool:
        """Отменяет запланированную публикацию.

        :return: ``True``, если состояние изменилось этим вызовом.
        :raises InvalidStateTransitionError: Публикация уже отправлена.
        """
        if self.status is ScheduledPostStatus.CANCELLED:
            return False

        self._transition_to(ScheduledPostStatus.CANCELLED)
        logger.info("Публикация id=%s отменена", self.id)
        return True
