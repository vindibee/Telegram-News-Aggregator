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