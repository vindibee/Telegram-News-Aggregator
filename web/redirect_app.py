"""Редирект-сервер трекинговых ссылок.

Эндпоинт лежит на горячем пути: по короткой ссылке идёт живой человек, и
всё, что происходит до отправки ответа, он ждёт. Поэтому здесь нет ни
одной записи в PostgreSQL — адрес назначения берётся из кэша Redis, а
переход отправляется в очередь, которую разбирает фоновая задача.

Промах кэша означает один запрос к базе и прогрев: короткие ссылки живут
долго, а популярны из них единицы, и после первого перехода дальнейшие
идут уже из памяти.

Ответ — ``307 Temporary Redirect``, а не ``301``. Постоянный редирект
браузеры и клиенты кэшируют навсегда: второй переход по ссылке до нас
просто не дошёл бы, и статистика показала бы один клик вместо сотни. По
той же причине ответ помечается как некэшируемый.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Final

from aiohttp import web

from core.config import Settings
from core.logger import get_logger
from db.models import ClickLog
from db.repositories.tracking import ClickEvent
from db.uow import UnitOfWorkFactory
from services.tracker import ClickCounter

logger = get_logger(__name__)

#: Ключи зависимостей в приложении aiohttp.
UOW_KEY: Final[web.AppKey[UnitOfWorkFactory]] = web.AppKey("redirect_uow")
COUNTER_KEY: Final[web.AppKey[ClickCounter]] = web.AppKey("click_counter")
SETTINGS_KEY: Final[web.AppKey[Settings]] = web.AppKey("redirect_settings")

#: Куда отправлять посетителя, если ссылки нет или она выключена.
FALLBACK_URL: Final[str] = "https://t.me"

#: Допустимый вид токена. Алфавит тот же, что у ``secrets.token_urlsafe``:
#: буквы, цифры, дефис и подчёркивание. Ограничение отсекает перебор до
#: обращения к базе — настоящий токен короткий, всё длиннее сканер.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


async def handle_redirect(request: web.Request) -> web.Response:
    """Перенаправляет посетителя по короткой ссылке.

    :param request: HTTP-запрос.
    :raises web.HTTPTemporaryRedirect: Ссылка найдена — переход по адресу.
    :raises web.HTTPNotFound: Ссылки нет либо она больше не действует.
    """
    token = request.match_info.get("short_code", "")
    if not _TOKEN_RE.match(token):
        # Всё, что не похоже на наш токен, — перебор: тратить на него
        # запрос к базе незачем.
        raise web.HTTPNotFound(text="Ссылка не найдена")

    counter = request.app[COUNTER_KEY]
    target = await counter.cached_target(token)

    if target is None:
        target = await _resolve(request, token)
        if target is None:
            raise web.HTTPNotFound(text="Ссылка не найдена или больше не действует")
        await counter.cache_target(token, target)

    await counter.record(_build_event(request, token))

    redirect = web.HTTPTemporaryRedirect(location=target)
    # Без этого браузер закэширует сам редирект и следующий переход
    # пройдёт мимо счётчика.
    redirect.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    # Именно raise, а не return: возврат объекта исключения aiohttp считает
    # устаревшим способом и предупреждает об этом.
    raise redirect


async def _resolve(request: web.Request, token: str) -> str | None:
    """Достаёт адрес назначения из базы.

    :param request: HTTP-запрос.
    :param token: Токен ссылки.
    :return: Адрес назначения либо ``None``, если ссылка недоступна.
    """
    uow_factory = request.app[UOW_KEY]
    now = datetime.now(tz=timezone.utc)

    try:
        async with uow_factory() as uow:
            link = await uow.links.get_by_token(token)
            if link is None:
                logger.info("Переход по неизвестному токену %r", token)
                return None
            if not link.is_available(now):
                logger.info("Переход по выключенной или просроченной ссылке %s", token)
                return None
            return link.target_url
    except Exception:  # noqa: BLE001 - посетитель не должен видеть трассировку
        logger.exception("Сбой поиска ссылки %s", token)
        return None


def _build_event(request: web.Request, token: str) -> ClickEvent:
    """Собирает событие перехода.

    Адрес посетителя не сохраняется: в базу идёт только необратимый
    отпечаток, по которому считаются уникальные переходы. Секретом служит
    тот же ключ, что и для отпечатков пробного периода — отдельный
    заводить незачем, а без секрета всё пространство IPv4 перебирается за
    минуты.
    """
    settings = request.app[SETTINGS_KEY]
    ip = _client_ip(request)
    visitor_hash: str | None = None

    if ip and settings.trial.fingerprint_secret:
        try:
            visitor_hash = ClickLog.build_visitor_hash(
                ip, request.headers.get("User-Agent"), settings.trial.fingerprint_secret
            )
        except ValueError:
            visitor_hash = None

    return ClickEvent(
        token=token,
        clicked_at=datetime.now(tz=timezone.utc),
        visitor_hash=visitor_hash,
        referer=request.headers.get("Referer"),
    )


def _client_ip(request: web.Request) -> str | None:
    """Определяет адрес посетителя с учётом обратного прокси.

    ``X-Forwarded-For`` заполняет прокси, и доверять ему можно ровно
    настолько, насколько закрыт прямой доступ к порту. Берётся первый
    адрес цепочки — он ближе всего к настоящему клиенту.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.remote


async def handle_link_health(request: web.Request) -> web.Response:
    """Показывает, сколько переходов ждёт переноса в базу."""
    counter = request.app[COUNTER_KEY]
    return web.json_response({"status": "ok", "pending_clicks": await counter.pending()})


def setup_redirect_routes(
    app: web.Application,
    *,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    counter: ClickCounter,
) -> web.Application:
    """Добавляет редирект-маршруты в существующее приложение.

    Отдельный сервер под редиректы не поднимается: приложение уже слушает
    порт ради вебхуков платежей, и второй процесс ради двух маршрутов
    усложнил бы развёртывание без выигрыша.

    :param app: Приложение aiohttp.
    :param settings: Настройки приложения.
    :param uow_factory: Фабрика единиц работы.
    :param counter: Буфер переходов.
    :return: То же приложение с добавленными маршрутами.
    """
    app[SETTINGS_KEY] = settings
    app[UOW_KEY] = uow_factory
    app[COUNTER_KEY] = counter

    app.router.add_get("/r/{short_code}", handle_redirect)
    app.router.add_get("/health/links", handle_link_health)
    logger.info("Редирект коротких ссылок доступен по /r/{short_code}")
    return app
