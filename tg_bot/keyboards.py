from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from tg_bot.callbacks import ChannelCB, ParseCB, PostCB


def kb_channels(channels: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ch in channels:
        builder.button(text=ch["label"], callback_data=ChannelCB(username=ch["username"]))
    builder.adjust(2)  # По 2 кнопки в ряд
    return builder.as_markup()


def kb_posts(posts: list, username: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in posts:
        # Формируем короткое превью текста для кнопки
        preview = (p.content or "").replace("\n", " ").strip()
        preview = preview[:20] + ("…" if len(preview) > 20 else "")
        label = f"📅 {p.post_time.strftime('%d.%m %H:%M')} | {preview}"

        builder.button(text=label, callback_data=PostCB(id=p.id, channel=username))

    # Кнопки управления
    builder.button(text="🔄 Обновить", callback_data=ParseCB(username=username))
    builder.button(text="◀️ К каналам", callback_data="to_list")
    builder.adjust(1)  # По 1 кнопке в ряд
    return builder.as_markup()


def kb_back(username: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад к постам", callback_data=ChannelCB(username=username))
    return builder.as_markup()