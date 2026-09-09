"""Middleware-слой бота."""

from tg_bot.middlewares.dependencies import DependenciesMiddleware
from tg_bot.middlewares.single_flight import SingleFlightMiddleware
from tg_bot.middlewares.throttling import ThrottlingMiddleware
from tg_bot.middlewares.user_context import UserContextMiddleware

__all__ = [
    "DependenciesMiddleware",
    "SingleFlightMiddleware",
    "ThrottlingMiddleware",
    "UserContextMiddleware",
]
