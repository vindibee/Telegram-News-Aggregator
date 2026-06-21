from aiogram.filters.callback_data import CallbackData

class ChannelCB(CallbackData, prefix="ch"):
    username: str

class ParseCB(CallbackData, prefix="parse"):
    username: str

class PostCB(CallbackData, prefix="post"):
    id: int
    channel: str