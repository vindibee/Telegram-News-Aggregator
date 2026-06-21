import io
import asyncio
from html import escape
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InputMediaPhoto, InputMediaVideo, BufferedInputFile
from aiogram.filters import CommandStart

from core.config import CHANNELS, MAX_TEXT, MAX_CAPTION
from core.logger import logger
from db.repo import NewsRepo
from services.parser import parse_channel
from tg_bot.callbacks import ChannelCB, ParseCB, PostCB
from tg_bot.keyboards import kb_channels, kb_posts, kb_back

router = Router()

# Хранилище кулдаунов (в памяти)
_cooldowns = {}


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Выберите канал для чтения:", reply_markup=kb_channels(CHANNELS))


@router.callback_query(F.data == "to_list")
async def back_to_channels(callback: CallbackQuery):
    await callback.message.edit_text("Выберите канал:", reply_markup=kb_channels(CHANNELS))


@router.callback_query(ChannelCB.filter())
async def show_channel(callback: CallbackQuery, callback_data: ChannelCB, repo: NewsRepo, http_session):
    """Отображение списка постов канала. Если пусто - запускает парсинг."""
    username = callback_data.username
    posts = await repo.get_recent_posts(username)

    if not posts:
        await handle_parsing(callback, username, repo, http_session)
        return

    await callback.message.edit_text(
        f"📋 Новости <b>@{escape(username)}</b>:",
        reply_markup=kb_posts(posts, username)
    )


@router.callback_query(ParseCB.filter())
async def force_parse(callback: CallbackQuery, callback_data: ParseCB, repo: NewsRepo, http_session):
    """Принудительный запуск парсинга по кнопке 'Обновить'."""
    username = callback_data.username
    # Простая реализация антиспама
    import time
    user_id = callback.from_user.id
    if user_id in _cooldowns and time.time() - _cooldowns[user_id] < 60:
        await callback.answer("⏳ Подождите минуту перед обновлением.", show_alert=True)
        return
    _cooldowns[user_id] = time.time()

    await handle_parsing(callback, username, repo, http_session)


async def handle_parsing(callback: CallbackQuery, username: str, repo: NewsRepo, http_session):
    """Общая логика парсинга и сохранения в БД."""
    await callback.message.edit_text(f"🔄 Парсинг @{username}...")

    posts, error = await parse_channel(http_session, username)
    if error:
        await callback.message.edit_text(f"❌ {error}", reply_markup=kb_channels(CHANNELS))
        return

    added = 0
    for p in posts:
        if await repo.save_post(username, p["post_time"], p["text"], p["media"]):
            added += 1

    db_posts = await repo.get_recent_posts(username)
    await callback.message.edit_text(
        f"✅ Обновлено! Новых: <b>{added}</b>.\n\nВыберите пост:",
        reply_markup=kb_posts(db_posts, username)
    )


@router.callback_query(PostCB.filter())
async def show_post(callback: CallbackQuery, callback_data: PostCB, repo: NewsRepo, http_session):
    """Отображение конкретного поста с медиафайлами."""
    await callback.answer()
    post = await repo.get_post_by_id(callback_data.id)

    if not post:
        await callback.message.edit_text("❌ Пост не найден.")
        return

    content = escape(post.content or "")
    text_full = content[:MAX_TEXT] or "<i>Нет текста</i>"
    cap_full = content[:MAX_CAPTION] or ""
    media_list = post.media_urls

    # Отправляем заголовок поста
    dt_str = post.post_time.strftime("%d.%m.%Y %H:%M")
    await callback.message.edit_text(f"📍 Пост от <b>{dt_str}</b> | @{escape(post.channel_name)}")

    # Логика отправки медиа
    if not media_list:
        await callback.message.answer(text_full)
        await callback.message.answer("Вернуться к списку?", reply_markup=kb_back(post.channel_name))
        return

    # Загружаем медиа в память
    async def fetch_media(url):
        try:
            async with http_session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception:
            pass
        return None

    tasks = [fetch_media(m["url"]) for m in media_list[:10]]  # Максимум 10 для Telegram альбома
    files = await asyncio.gather(*tasks)

    valid_media = [(media_list[i], files[i]) for i in range(len(files)) if files[i]]

    if not valid_media:
        await callback.message.answer(f"⚠️ Медиа недоступны\n\n{text_full}")
    elif len(valid_media) == 1:
        meta, data = valid_media[0]
        f = BufferedInputFile(data, filename="media")
        if meta["type"] == "photo":
            await callback.message.answer_photo(f, caption=cap_full)
        else:
            await callback.message.answer_video(f, caption=cap_full)
    else:
        # Отправка медиагруппы (альбома)
        group = []
        for i, (meta, data) in enumerate(valid_media):
            caption = cap_full if i == 0 else ""
            f = BufferedInputFile(data, filename=f"media_{i}")
            if meta["type"] == "photo":
                group.append(InputMediaPhoto(media=f, caption=caption))
            else:
                group.append(InputMediaVideo(media=f, caption=caption))
        try:
            await callback.message.answer_media_group(group)
        except Exception as e:
            logger.error(f"Media group error: {e}")
            await callback.message.answer(f"⚠️ Ошибка отправки медиа\n\n{text_full}")

    await callback.message.answer("Вернуться к списку?", reply_markup=kb_back(post.channel_name))