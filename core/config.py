import os
from dotenv import load_dotenv

## Загрузка переменных из .env файла

load_dotenv(".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле!")

# Данные для БД
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")
DB_NAME = os.getenv("DB_NAME", "news_db")
DB_HOST = os.getenv("DB_HOST", "db")

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"

# Настройки парсера
PARSE_COOLDOWN = 60
MAX_POSTS = 10
MAX_TEXT = 4096
MAX_CAPTION = 1024

# Список каналов nen
CHANNELS = [
    {"label": "🎮 Игромания", "username": "igromania"},
    {"label": "📱 Wylsacom Red", "username": "wylsared"},
    {"label": "🌍 Новости Одесса", "username": "our_odessa"},
    {"label": "🚀 Хабр", "username": "habr_com"},
    {"label": "📊 РБК", "username": "rbc_news"},
    {"label": "⚡️ Mash", "username": "breakingmash"},
    {"label": "🧠 ПостНаука", "username": "postnauka"},
    {"label": "🍿 Кинопоиск", "username": "kinopoisk"},
    {"label": "📰 Лентач", "username": "lentachold"},
    {"label": "IT Музей", "username": "computer_history"},
]

