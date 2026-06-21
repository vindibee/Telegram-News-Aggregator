from datetime import datatime
from sqlalchemy import String, Text, DateTime, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

class Base(DeclarativeBase):
    pass

class NewsPost(Base):
    __tablename__ = 'new post'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    post_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=True)
    # Используем JSONB для эффективного хранения массивов медиа в PostgreSQL
    media_urls: Mapped[dict | list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        # Гарантируем, что мы не сохраним один и тот же пост дважды
        UniqueConstraint('channel_name', 'post_time', name='uix_channel_time'),
    )