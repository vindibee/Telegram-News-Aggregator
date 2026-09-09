# Telegram News Aggregator

Telegram-бот, который собирает записи публичных каналов через веб-превью
`t.me/s/<channel>`, сохраняет их в PostgreSQL и показывает пользователю
с текстом и медиа.

## Архитектура

Слои разделены по ответственности, зависимости направлены строго вниз:

```
main.py                 композиционный корень: создаёт бота, БД, HTTP-сессию
└── tg_bot/             слой Telegram
    ├── handlers.py     разбор ввода и вызов сервисов (без SQL и HTTP)
    ├── views.py        отрисовка карточки поста
    ├── keyboards.py    инлайн-клавиатуры
    ├── callbacks.py    типизированные callback_data
    ├── middlewares.py  сессия БД и сборка зависимостей на каждый апдейт
    ├── errors.py       глобальная обработка исключений
    └── utils.py        безопасное редактирование, нарезка текста, ретраи
└── services/           прикладной слой
    ├── parser.py       парсинг HTML канала
    ├── media.py        загрузка вложений с лимитом размера
    ├── news_service.py сценарии «показать» и «обновить»
    └── cooldown.py     антиспам
└── db/                 доступ к данным
    ├── models.py       ORM-модели (SQLAlchemy 2.0)
    ├── repo.py         репозиторий: все запросы к БД
    └── database.py     движок и фабрика сессий
└── core/               конфигурация и логирование
```

## Запуск в Docker

```bash
cp .env.example .env          # укажите BOT_TOKEN и пароль БД
docker compose -f Docker/docker-compose.yml up -d --build
docker compose -f Docker/docker-compose.yml logs -f bot
```

## Локальный запуск

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                             # DB_HOST=localhost
python main.py
```

Нужен доступный PostgreSQL: таблицы создаются автоматически при старте.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `BOT_TOKEN` | — (обязательна) | Токен бота от @BotFather |
| `DB_USER` / `DB_PASS` / `DB_NAME` | `postgres` / `postgres` / `news_db` | Доступ к PostgreSQL |
| `DB_HOST` / `DB_PORT` | `db` / `5432` | Адрес БД (в compose переопределяется на `db`) |
| `LOG_LEVEL` | `INFO` | Уровень логирования |
| `DISPLAY_TZ` | `UTC` | Таймзона отображения дат |
| `PARSE_COOLDOWN` | `60` | Пауза между обновлениями одного канала, секунды |
| `MAX_POSTS` | `10` | Сколько постов показывать |
| `REQUEST_TIMEOUT` / `MEDIA_TIMEOUT` | `15` / `30` | Таймауты HTTP, секунды |
| `MAX_MEDIA_BYTES` | `20971520` | Лимит размера одного вложения |
| `DB_ECHO` | `false` | Печать SQL в лог |

## Замечания по эксплуатации

* Схема создаётся через `Base.metadata.create_all`. Для боевой эксплуатации
  с изменяющейся схемой подключите Alembic.
* Кулдаун хранится в памяти процесса: при запуске нескольких реплик бота
  замените `CooldownStorage` на реализацию поверх Redis.
* Бот работает только с каналами из белого списка в `core/config.py` —
  произвольные адреса из callback_data не принимаются.
