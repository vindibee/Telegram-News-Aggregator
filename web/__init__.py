"""HTTP-вход приложения: приём вебхуков платёжных провайдеров."""

from web.webhook import build_web_app, handle_cryptobot_webhook, start_web_app

__all__ = ["build_web_app", "handle_cryptobot_webhook", "start_web_app"]
