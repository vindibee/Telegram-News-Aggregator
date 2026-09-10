"""Хендлеры бота.

Слой намеренно «тонкий»: разбор пользовательского ввода, вызов сервиса и
делегирование отрисовки. Ни HTTP, ни SQL здесь нет.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from core.config import Settings
from core.logger import get_logger
from db.models import Post, User
from services.i18n import Translator
from services.news_service import NewsService
from services.referrals import ReferralService, parse_referral_payload
from services.ratelimit.base import RateLimiter, RateLimitRule
from db.uow import UnitOfWork
from tg_bot.callbacks import ACTION_CHANNELS, ChannelCB, MenuCB, PostCB, RefreshCB
from tg_bot.flags import no_single_flight, rate_limit
from tg_bot.handlers.promo import notify_referrer
from tg_bot.keyboards import kb_channels, kb_posts, kb_to_channels
from tg_bot.utils import get_message, safe_edit_text
from tg_bot.views import PostRenderer

logger = get_logger(__name__)

router = Router(name="news")



@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Приветствие, список каналов и разбор реферальной ссылки.

    Реферальная нагрузка обрабатывается здесь, а не отдельной
    командой: у Telegram один вход по ссылке — ``/start`` с
    полезной нагрузкой, и другого места для неё просто нет.

    Приветствие показывается в любом случае, даже если код оказался
    чужим или уже использованным: человек пришёл пользоваться
    ботом, а не разбираться в чужой реферальной ссылке.
    """
    code = parse_referral_payload(command.args)
    if code is not None:
        await _apply_referral(message, bot, code, user=user, uow=uow,
                              settings=settings, i18n=i18n)

    await message.answer(
        i18n("start.greeting"), reply_markup=kb_channels(settings.channels, i18n)
    )


async def _apply_referral(
    message: Message,
    bot: Bot,
    code: str,
    *,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Начисляет реферальный бонус и уведомляет обе стороны.

    Молчаливые исходы намеренны: «код не найден» и «вы уже пришли по
    чужой ссылке» интересны только тому, кто пытается накрутить
    программу. Обычному человеку сообщать не о чем — он просто
    открыл бота.
    """
    service = ReferralService(uow, bonus_days=settings.admin.referral_bonus_days)
    result = await service.apply_code(
        user=user, code=code, now=datetime.now(tz=timezone.utc)
    )

    if not result.granted:
        return

    await message.answer(
        i18n("referral.welcome", days=i18n.plural("units.days", result.days))
    )
    if result.referrer is not None:
        await notify_referrer(
            bot, referrer=result.referrer, days=result.days, i18n=i18n
        )


@router.message(Command("help"))
async def cmd_help(message: Message, i18n: Translator) -> None:
    """Краткая справка по боту."""
    await message.answer(i18n("help.text"), reply_markup=kb_to_channels(i18n))


@router.callback_query(MenuCB.filter(F.action == ACTION_CHANNELS), **no_single_flight())
async def show_channels(
    callback: CallbackQuery,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Возврат к списку каналов."""
    await callback.answer()
    message = get_message(callback)
    if message is None:
        return
    await safe_edit_text(
        message, i18n("channels.choose"), kb_channels(settings.channels, i18n)
    )


@router.callback_query(ChannelCB.filter())
async def show_channel(
    callback: CallbackQuery,
    callback_data: ChannelCB,
    service: NewsService,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Список сохранённых постов канала; при пустой базе — первичный парсинг."""
    await callback.answer()

    message = get_message(callback)
    if message is None:
        return

    username = await _resolve_channel(callback, settings, i18n, callback_data.username)
    if username is None:
        return

    posts = await service.get_posts(username)
    if not posts:
        await _refresh_channel(message, service, settings, i18n, username)
        return

    await _show_posts(
        message, settings, i18n, username, posts, header=i18n("channels.saved")
    )


# Обновление канала дорогое (сетевой парсинг), поэтому лимит строже общего.
@router.callback_query(RefreshCB.filter(), **rate_limit(3, 60, scope="refresh_button"))
async def refresh_channel(
    callback: CallbackQuery,
    callback_data: RefreshCB,
    service: NewsService,
    settings: Settings,
    i18n: Translator,
    limiter: RateLimiter,
) -> None:
    """Принудительное обновление канала с ограничением частоты.

    Общий троттлинг в middleware считает нажатия суммарно, а здесь лимит
    нужен на пару «пользователь + канал»: обновив один канал, пользователь
    не должен ждать, чтобы обновить другой.
    """
    message = get_message(callback)
    if message is None:
        await callback.answer(i18n("common.stale"), show_alert=True)
        return

    username = await _resolve_channel(callback, settings, i18n, callback_data.username)
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
            i18n(
                "channels.cooldown",
                seconds=i18n.plural("units.seconds", decision.retry_after_seconds),
            ),
            show_alert=True,
        )
        return

    await callback.answer(i18n("channels.refreshing"))
    await _refresh_channel(message, service, settings, i18n, username)


@router.callback_query(PostCB.filter())
async def show_post(
    callback: CallbackQuery,
    callback_data: PostCB,
    service: NewsService,
    renderer: PostRenderer,
    i18n: Translator,
) -> None:
    """Карточка конкретного поста с медиа."""
    await callback.answer()

    message = get_message(callback)
    if message is None:
        return

    post = await service.get_post(callback_data.id)
    if post is None:
        await safe_edit_text(message, i18n("post.not_found"), kb_to_channels(i18n))
        return

    await renderer.render(message, post, i18n)


@router.callback_query()
async def unknown_callback(callback: CallbackQuery, i18n: Translator) -> None:
    """Кнопка из устаревшей версии интерфейса."""
    logger.info("Неизвестный callback: %r", callback.data)
    await callback.answer(i18n("common.stale"), show_alert=True)


async def _resolve_channel(
    callback: CallbackQuery,
    settings: Settings,
    i18n: Translator,
    username: str,
) -> str | None:
    """Сверяет канал с белым списком.

    Значение приходит от клиента и не может быть доверенным: без проверки
    им можно было бы заставить бота обратиться к произвольному адресу.
    """
    channel = settings.channel_by_username(username)
    if channel is None:
        logger.warning("Запрошен канал вне белого списка: %r", username)
        await callback.answer(i18n("channels.unknown"), show_alert=True)
        return None
    return channel.username


async def _refresh_channel(
    message: Message,
    service: NewsService,
    settings: Settings,
    i18n: Translator,
    username: str,
) -> None:
    """Парсит канал и показывает обновлённый список постов."""
    await safe_edit_text(message, i18n("channels.loading", channel=escape(username)))

    result = await service.refresh(username)
    await _show_posts(
        message,
        settings,
        i18n,
        username,
        result.posts,
        header=i18n("channels.updated", posts=i18n.plural("units.posts", result.added)),
    )


async def _show_posts(
    message: Message,
    settings: Settings,
    i18n: Translator,
    username: str,
    posts: Sequence[Post],
    header: str,
) -> None:
    """Единая точка отрисовки списка постов (DRY для всех сценариев)."""
    if not posts:
        await safe_edit_text(
            message,
            i18n("channels.empty", channel=escape(username)),
            kb_to_channels(i18n),
        )
        return

    text = i18n("channels.pick_post", header=header, channel=escape(username))
    await safe_edit_text(
        message, text, kb_posts(posts, username, settings.display_timezone, i18n)
    )
