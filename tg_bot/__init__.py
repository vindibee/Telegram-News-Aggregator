"""Слой Telegram: хендлеры, клавиатуры, middleware и отрисовка."""

from tg_bot.errors import register_error_handlers
from tg_bot.handlers import router
from tg_bot.middlewares import DependenciesMiddleware
from tg_bot.views import PostRenderer

__all__ = [
    "DependenciesMiddleware",
    "PostRenderer",
    "register_error_handlers",
    "router",
]
