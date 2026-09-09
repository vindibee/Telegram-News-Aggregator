"""ORM-модели приложения."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class NewsPost(Base):
    """Сохранённый пост публичного Telegram-канала."""

    __tablename__ = "news_posts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Идентификатор сообщения внутри канала — единственный по-настоящему
    # надёжный ключ дедупликации (время публикации может совпадать у разных постов).
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    post_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # JSONB эффективнее JSON в PostgreSQL: бинарное хранение + индексируемость.
    media_urls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Гарантия идемпотентности парсинга: один и тот же пост не сохранится дважды.
        UniqueConstraint("channel_name", "message_id", name="uq_news_posts_channel_message"),
        # Покрывающий индекс под основной запрос «последние посты канала».
        Index("ix_news_posts_channel_time", "channel_name", post_time.desc()),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<NewsPost id={self.id} channel={self.channel_name!r} message_id={self.message_id}>"
