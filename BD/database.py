from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from core.config import DATABASE_URL
from db.models import Base

# Создаем движок (engine). echo=False скрывает сырые SQL запросы в консоли.
engine = create_async_engine(DATABASE_URL, echo=False)

# Создаем фабрику сессий
async_session_maker = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)

async def init_models():
    """Инициализация таблиц. В реальных проектах заменяется на Alembic migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)