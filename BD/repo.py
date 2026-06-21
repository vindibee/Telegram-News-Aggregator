from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from db.models import NewsPost
from datetime import datetime


class NewsRepo:
    """Паттерн Репозиторий. Инкапсулирует все запросы к БД."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_post(self, channel: str, p_time: datetime, content: str, media: list) -> bool:
        # insert().on_conflict_do_nothing() - безопасный способ избежать дубликатов
        stmt = insert(NewsPost).values(
            channel_name=channel,
            post_time=p_time,
            content=content,
            media_urls=media
        ).on_conflict_do_nothing(index_elements=['channel_name', 'post_time'])

        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # Вернет True, если пост был добавлен

    async def get_recent_posts(self, channel: str, limit: int = 10):
        stmt = select(NewsPost).where(NewsPost.channel_name == channel) \
            .order_by(NewsPost.post_time.desc()) \
            .limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_post_by_id(self, post_id: int):
        return await self.session.get(NewsPost, post_id)