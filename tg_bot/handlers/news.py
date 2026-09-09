"""Хендлеры бота.

Слой намеренно «тонкий»: разбор пользовательского ввода, вызов сервиса и
делегирование отрисовки. Ни HTTP, ни SQL здесь нет.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from core.config import Settings
from core.logger import get_logger
from db.models import Post
from services.news_service import NewsService
from services.ratelimit.base import RateLimiter, RateLimitRule
from tg_bot.callbacks import ACTION_CHANNELS, ChannelCB, MenuCB, PostCB, RefreshCB
from tg_bot.flags import no_single_flight, rate_limit
from tg_bot.keyboards import kb_channels, kb_posts, kb_to_channels
from tg_bot.utils import get_message, safe_edit_text
from tg_bot.views import PostRenderer

logger = get_logger(__name__)

router = Router(name="news")

_GREETING = (
    "👋 <b>Агрегатор новостей Telegram</b>\n\n"
    "Выберите канал — я покажу последние записи и сохраню их в базу."
)
_HELP = (
    "ℹ️ <b>Как пользоваться</b>\n\n"
    "/start — список каналов\n"
    "/help — эта справка\n\n"
    "В списке постов кнопка «🔄 Обновить» подтягивает свежие записи из канала."
)
_CHOOSE_CHANNEL = "📡 Выберите канал:"
_UNKNOWN_CHANNEL = "Этот канал больше не поддерживается."
_STALE_MESSAGE = "Сообщение устарело, отправьте /start."


@router.message(CommandStart())
async def cmd_start(message: Message, settings: Settings) -> None:
    """Приветствие и список каналов."""
    await message.answer(_GREETING, reply_markup=kb_channels(settings.channels))


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Краткая справка по боту."""
    await message.answer(_HELP, reply_markup=kb_to_channels())


@router.callback_query(MenuCB.filter(F.action == ACTION_CHANNELS), **no_single_flight())
async def show_channels(callback: CallbackQuery, settings: Settings) -> None:
    """Возврат к списку каналов."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return
    await safe_edit_text(message, _CHOOSE_CHANNEL, kb_channels(settings.channels))


@router.callback_query(ChannelCB.filter())
async def show_channel(
    callback: CallbackQuery,
    callback_data: ChannelCB,
    service: NewsService,
    settings: Settings,
) -> None:
    """Список сохранённых постов канала; при пустой базе — первичный парсинг."""
    await callback.answer()

    message = get_message(callback)
    if message is None:
        return

    username = await _resolve_channel(callback, settings, callback_data.username)
    if username is None:
        return

    posts = await service.get_posts(username)
    if not posts:
        await _refresh_channel(message, service, settings, username)
        return

    await _show_posts(message, settings, username, posts, header="📋 Записи")


# Обновление канала дорогое (сетевой парсинг), поэтому лимит строже общего.
@router.callback_query(RefreshCB.filter(), **rate_limit(3, 60, scope="refresh_button"))
async def refresh_channel(
    callback: CallbackQuery,
    callback_data: RefreshCB,
    service: NewsService,
    settings: Settings,
    limiter: RateLimiter,
) -> None:
    """Принудительное обновление канала с ограничением частоты.

    Общий троттлинг в middleware считает нажатия суммарно, а здесь лимит
    нужен на пару «пользователь + канал»: обновив один канал, пользователь
    не должен ждать, чтобы обновить другой.
    """
    message = get_message(callback)
    if message is None:
        await callback.answer(_STALE_MESSAGE, show_alert=True)
        return

    username = await _resolve_channel(callback, settings, callback_data.username)
    if username is None:
        return

    rule = RateLimitRule(
        limit=settings.rate_limit.refresh_limit,
        window=settings.rate_limit.refresh_window,
        scope="refresh_channel",
    )
    decision = await limiter.acquire(f"{callback.from_user.id}:{username}", rule)
    if not decision.allowed:
        await callback.answer(
            f"⏳ Обновление канала доступно через {decision.retry_after_seconds} с.",
            show_alert=True,
        )
        return

    await callback.answer("Обновляю…")
    await _refresh_channel(message, service, settings, username)


@router.callback_query(PostCB.filter())
async def show_post(
    callback: CallbackQuery,
    callback_data: PostCB,
    service: NewsService,
    renderer: PostRenderer,
) -> None:
    """Карточка конкретного поста с медиа."""
    await callback.answer()

    message = get_message(callback)
    if message is None:
        return

    post = await service.get_post(callback_data.id)
    if post is None:
        await safe_edit_text(message, "❌ Пост не найден.", kb_to_channels())
        return

    await renderer.render(message, post)


@router.callback_query()
async def unknown_callback(callback: CallbackQuery) -> None:
    """Кнопка из устаревшей версии интерфейса."""
    logger.info("Неизвестный callback: %r", callback.data)
    await callback.answer(_STALE_MESSAGE, show_alert=True)


async def _resolve_channel(
    callback: CallbackQuery,
    settings: Settings,
    username: str,
) -> str | None:
    """Сверяет канал с белым списком.

    Значение приходит от клиента и не может быть доверенным: без проверки
    им можно было бы заставить бота обратиться к произвольному адресу.
    """
    channel = settings.channel_by_username(username)
    if channel is None:
        logger.warning("Запрошен канал вне белого списка: %r", username)
        await callback.answer(_UNKNOWN_CHANNEL, show_alert=True)
        return None
    return channel.username


async def _refresh_channel(
    message: Message,
    service: NewsService,
    settings: Settings,
    username: str,
) -> None:
    """Парсит канал и показывает обновлённый список постов."""
    await safe_edit_text(message, f"🔄 Загружаю записи @{escape(username)}…")

    result = await service.refresh(username)
    await _show_posts(
        message,
        settings,
        username,
        result.posts,
        header=f"✅ Обновлено, новых записей: <b>{result.added}</b>",
    )


async def _show_posts(
    message: Message,
    settings: Settings,
    username: str,
    posts: Sequence[Post],
    header: str,
) -> None:
    """Единая точка отрисовки списка постов (DRY для всех сценариев)."""
    if not posts:
        await safe_edit_text(
            message,
            f"📭 Для @{escape(username)} пока нет сохранённых записей.",
            kb_to_channels(),
        )
        return

    text = f"{header} · <b>@{escape(username)}</b>\n\nВыберите запись:"
    await safe_edit_text(message, text, kb_posts(posts, username, settings.display_timezone))
