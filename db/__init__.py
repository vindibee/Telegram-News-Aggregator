"""Слой доступа к данным: модели, репозитории и инфраструктура подключения."""

from db.base import Base
from db.database import Database
from db.models import NewsPost
from db.repo import NewsPostData, NewsRepo, RepositoryError

__all__ = [
    "Base",
    "Database",
    "NewsPost",
    "NewsPostData",
    "NewsRepo",
    "RepositoryError",
]
