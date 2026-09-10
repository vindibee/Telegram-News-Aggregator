"""add ukrainian to user_language

Revision ID: a1c73f0b48d2
Revises: e954b677d8e2
Create Date: 2026-09-10 10:15:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a1c73f0b48d2"
down_revision: str | None = "e954b677d8e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет значение ``uk`` в тип ``user_language``.

    Alembic не отслеживает состав ENUM автоматически: автогенерация видит
    только колонки, поэтому новые значения перечислений всегда пишутся
    руками.

    ``IF NOT EXISTS`` делает шаг идемпотентным — миграцию могли частично
    применить на одном из стендов.
    """
    op.execute("ALTER TYPE user_language ADD VALUE IF NOT EXISTS 'uk'")


def downgrade() -> None:
    """Возвращает тип к прежнему составу значений.

    PostgreSQL не умеет удалять значение из ENUM, поэтому тип
    пересоздаётся: новый тип рядом, перенос колонки, удаление старого.
    Строки с ``uk`` перед этим переводятся на язык по умолчанию — иначе
    приведение типа упадёт на первом же таком пользователе.
    """
    op.execute("UPDATE users SET language = 'ru' WHERE language = 'uk'")
    op.execute("ALTER TYPE user_language RENAME TO user_language_old")
    op.execute("CREATE TYPE user_language AS ENUM ('ru', 'en')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN language DROP DEFAULT, "
        "ALTER COLUMN language TYPE user_language "
        "USING language::text::user_language, "
        "ALTER COLUMN language SET DEFAULT 'ru'"
    )
    op.execute("DROP TYPE user_language_old")
