"""Каналы пользователя: источники контента и цели публикации."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import ChannelKind, pg_enum
from db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from db.models.user import User

logger = get_logger(__name__)

#: Правила Telegram для публичного имени канала.
_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class UserChannel(Base, IdMixin, TimestampMixin):
    """Канал, подключённый пользователем.

    Одна таблица на источники и цели, а не две: набор полей у них
    совпадает полностью, а роль различается одним значением. Две почти
    одинаковые таблицы означали бы дублирование индексов, ограничений и
    репозиторного кода — ровно то, против чего направлен DRY.

    Роль входит в ключ уникальности: один и тот же канал пользователь
    вправе и читать, и публиковать в него.
    """

    __tablename__ = "user_channels"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[ChannelKind] = mapped_column(
        pg_enum(ChannelKind, "channel_kind"),
        nullable=False,
    )

    #: Публичное имя без «@». Для приватных каналов отсутствует.
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Числовой идентификатор чата. Обязателен для целей публикации:
    #: отправлять сообщения по имени ненадёжно — канал могут переименовать,
    #: а идентификатор неизменен.
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    title: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")

    #: Выключенный канал сохраняется, но не обрабатывается: пользователь
    #: часто отключает источник временно, и терять историю постов из-за
    #: этого не нужно.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    #: Подтверждено ли, что бот администратор целевого канала. Право
    #: проверяется обращением к Bot API и кэшируется здесь: спрашивать его
    #: перед каждой публикацией — лишний round-trip на каждый пост.
    #: Для источников не применимо и остаётся ложью.
    bot_is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: Последняя ошибка обработки — показывается пользователю, чтобы канал
    #: не «молчал» без объяснений.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship("User", back_populates="channels", lazy="raise")

    __table_args__ = (
        # Два ключа уникальности вместо одного: в PostgreSQL NULL не равен
        # NULL, поэтому ограничение по колонке с пропусками не помешало бы
        # добавить один и тот же приватный канал дважды.
        UniqueConstraint("user_id", "kind", "username", name="uq_user_channels_username"),
        UniqueConstraint("user_id", "kind", "chat_id", name="uq_user_channels_chat_id"),
        CheckConstraint(
            "username IS NOT NULL OR chat_id IS NOT NULL",
            name="identifier_present",
        ),
        CheckConstraint(
            "kind <> 'target' OR chat_id IS NOT NULL",
            name="target_requires_chat_id",
        ),
        # Основной запрос планировщика: «активные каналы такой-то роли».
        Index(
            "ix_user_channels_active",
            "user_id",
            "kind",
            postgresql_where=text("is_active"),
        ),
        Index("ix_user_channels_username", "username"),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<UserChannel id={self.id} user_id={self.user_id} "
            f"kind={self.kind} username={self.username!r}>"
        )

    @property
    def display_name(self) -> str:
        """Название для показа пользователю."""
        if self.title:
            return self.title
        if self.username:
            return f"@{self.username}"
        return f"id{self.chat_id}"

    @property
    def is_publishable(self) -> bool:
        """Готов ли канал принимать публикации."""
        return (
            self.kind is ChannelKind.TARGET
            and self.is_active
            and self.bot_is_admin
            and self.chat_id is not None
        )

    def mark_synced(self, moment: datetime) -> None:
        """Отмечает успешное чтение источника и снимает прошлую ошибку.

        :param moment: Момент синхронизации (timezone-aware).
        """
        self.last_synced_at = moment
        self.last_error = None

    def mark_published(self, moment: datetime) -> None:
        """Отмечает успешную публикацию в целевой канал.

        :param moment: Момент публикации (timezone-aware).
        """
        self.last_published_at = moment
        self.last_error = None

    def mark_failed(self, reason: str) -> None:
        """Сохраняет причину сбоя обработки канала.

        Текст обрезается: сообщения об ошибках Bot API бывают длиной в
        абзац, а колонка нужна для короткой подсказки в интерфейсе.

        :param reason: Текст ошибки.
        """
        self.last_error = reason.strip()[:500]
        logger.warning("Канал id=%s помечен ошибкой: %s", self.id, self.last_error)

    def deactivate(self, reason: str | None = None) -> None:
        """Выключает канал, сохраняя его в списке пользователя.

        :param reason: Причина отключения.
        """
        self.is_active = False
        if reason:
            self.mark_failed(reason)
        logger.info("Канал id=%s отключён", self.id)

    @staticmethod
    def normalize_username(raw: str) -> str:
        """Приводит имя канала к каноническому виду.

        Пользователь вводит канал как угодно: со «@», ссылкой ``t.me/name``
        или ``https://t.me/name``. В базе имя должно храниться в одном
        виде, иначе ограничение уникальности не сработает.

        :param raw: Пользовательский ввод.
        :return: Имя без «@» и префиксов.
        :raises ValueError: Значение не похоже на имя канала Telegram.
        """
        value = raw.strip()
        for prefix in ("https://", "http://"):
            if value.lower().startswith(prefix):
                value = value[len(prefix):]
        for prefix in ("t.me/", "telegram.me/", "telegram.dog/"):
            if value.lower().startswith(prefix):
                value = value[len(prefix):]
        value = value.lstrip("@").split("/", 1)[0].split("?", 1)[0]

        if not _USERNAME_RE.match(value):
            raise ValueError(
                f"Некорректное имя канала: {raw!r}. Ожидается 5–32 символа: "
                "латиница, цифры и подчёркивание."
            )
        return value
