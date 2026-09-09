# syntax=docker/dockerfile:1
# Контекст сборки — корень репозитория (см. docker-compose.yml).
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости — отдельным слоем: пересобираются только при смене requirements.txt.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Исходный код приложения.
COPY main.py ./
COPY core ./core
COPY db ./db
COPY services ./services
COPY tg_bot ./tg_bot

# Процесс не должен работать от root: компрометация парсера не даёт прав в контейнере.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-u", "main.py"]
