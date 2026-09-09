"""Модель новостного поста: полнотекстовый поиск и дедупликация."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import PostStatus, pg_enum
from db.mixins import IdMixin, TimestampMixin

logger = get_logger(__name__)

#: Конфигурация текстового поиска PostgreSQL (включает стемминг).
FTS_CONFIG: Final[str] = "russian"

#: Число band-колонок для поиска кандидатов на дубликат (LSH).
SIMHASH_BANDS: Final[int] = 4


class Post(Base, IdMixin, TimestampMixin):
    """Новость, полученная из публичного канала.

    Модель обслуживает три сценария сразу:

    * выдачу последних записей канала (индекс по ``channel_name, post_time``);
    * полнотекстовый поиск (генерируемая колонка ``search_vector`` + GIN);
    * дедупликацию (точное совпадение по ``content_hash`` и поиск похожих
      по band-колонкам simhash).
    """

    __tablename__ = "posts"

    channel_name: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    post_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    media_urls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    status: Mapped[PostStatus] = mapped_column(
        pg_enum(PostStatus, "post_status"),
        nullable=False,
        default=PostStatus.NEW,
        server_default=PostStatus.NEW.value,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Ссылка на канонический пост, если эта запись признана дубликатом.
    duplicate_of_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("posts.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: SHA-256 нормализованного текста — мгновенно ловит точные перепечатки.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Simhash в виде знакового 64-битного целого (BIGINT не хранит
    #: беззнаковые значения, преобразование делает services.fingerprint).
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: Четыре 16-битных среза simhash. Совпадение хотя бы одного среза —
    #: дешёвый индексируемый способ отобрать кандидатов перед точным
    #: расчётом расстояния Хэмминга (схема LSH by banding).
    simhash_band_0: Mapped[int | None] = mapped_column(Integer, nullable=True)
    simhash_band_1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    simhash_band_2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    simhash_band_3: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Генерируемая колонка: PostgreSQL пересчитывает её сам при любом
    #: изменении content. Триггеры для этого не нужны, а рассинхронизация
    #: индекса с текстом становится невозможной.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{FTS_CONFIG}', coalesce(content, ''))", persisted=True),
        nullable=False,
    )

    duplicate_of: Mapped[Post | None] = relationship(
        "Post",
        remote_side="Post.id",
        back_populates="duplicates",
        lazy="raise",
    )
    duplicates: Mapped[list[Post]] = relationship(
        "Post",
        back_populates="duplicate_of",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("channel_name", "message_id", name="uq_posts_channel_message"),
        CheckConstraint(
            "duplicate_of_id IS NULL OR duplicate_of_id <> id",
            name="no_self_duplicate",
        ),
        Index("ix_posts_channel_name_post_time", "channel_name", post_time.desc()),
        Index("ix_posts_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_posts_content_hash", "content_hash"),
        Index("ix_posts_duplicate_of_id", "duplicate_of_id"),
        Index("ix_posts_status_post_time", "status", post_time.desc()),
        # Частичные индексы: band-колонки заполнены только у постов с текстом,
        # и индексировать NULL-строки смысла нет.
        Index(
            "ix_posts_simhash_band_0",
            "simhash_band_0",
            postgresql_where=text("simhash_band_0 IS NOT NULL"),
        ),
        Index(
            "ix_posts_simhash_band_1",
            "simhash_band_1",
            postgresql_where=text("simhash_band_1 IS NOT NULL"),
        ),
        Index(
            "ix_posts_simhash_band_2",
            "simhash_band_2",
            postgresql_where=text("simhash_band_2 IS NOT NULL"),
        ),
        Index(
            "ix_posts_simhash_band_3",
            "simhash_band_3",
            postgresql_where=text("simhash_band_3 IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return (
            f"<Post id={self.id} channel={self.channel_name!r} "
            f"message_id={self.message_id} status={self.status}>"
        )

    @property
    def is_duplicate(self) -> bool:
        """Признана ли запись дубликатом другой."""
        return self.duplicate_of_id is not None

    @property
    def has_media(self) -> bool:
        """Есть ли у записи медиавложения."""
        return bool(self.media_urls)

    @property
    def source_url(self) -> str:
        """Ссылка на исходное сообщение в Telegram."""
        return f"https://t.me/{self.channel_name}/{self.message_id}"

    def preview(self, limit: int = 80) -> str:
        """Короткое превью текста одной строкой.

        :param limit: Максимальная длина результата (не меньше 4 символов).
        :return: Текст без переносов строк, при необходимости с многоточием.
        :raises ValueError: Слишком малый лимит.
        """
        if limit < 4:
            raise ValueError(f"Лимит превью должен быть не меньше 4, получено: {limit}")

        normalized = " ".join((self.content or "").split())
        if not normalized:
            return "медиа без текста" if self.has_media else "пустая запись"
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"

    def mark_duplicate_of(self, canonical: Post) -> None:
        """Помечает запись дубликатом канонической.

        :param canonical: Каноническая запись (сама не должна быть дубликатом,
            иначе образуются цепочки, ломающие подсчёт кластеров).
        :raises ValueError: Попытка сослаться на саму себя, на запись без ``id``
            или на другой дубликат.
        """
        if canonical.id is None:
            raise ValueError("Каноническая запись должна быть сохранена в БД (id отсутствует).")
        if canonical.id == self.id:
            raise ValueError(f"Запись id={self.id} не может быть дубликатом самой себя.")
        if canonical.duplicate_of_id is not None:
            raise ValueError(
                f"Запись id={canonical.id} сама является дубликатом "
                f"id={canonical.duplicate_of_id}; цепочки дубликатов запрещены."
            )

        self.duplicate_of_id = canonical.id
        self.status = PostStatus.DUPLICATE
        logger.info("Запись id=%s помечена дубликатом id=%s", self.id, canonical.id)

    def mark_published(self, moment: datetime) -> bool:
        """Отмечает публикацию записи подписчикам.

        :param moment: Момент публикации (timezone-aware).
        :return: ``False``, если запись уже была опубликована ранее.
        """
        if self.status is PostStatus.PUBLISHED:
            logger.debug("Запись id=%s уже опубликована в %s", self.id, self.published_at)
            return False

        if self.status is PostStatus.DUPLICATE:
            logger.warning("Попытка опубликовать дубликат id=%s отклонена", self.id)
            return False

        self.status = PostStatus.PUBLISHED
        self.published_at = moment
        logger.info("Запись id=%s опубликована", self.id)
        return True
