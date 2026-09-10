"""Репозиторий пользовательских фильтров: триггеры и стоп-слова."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import delete as sql_delete, func, select
from sqlalchemy.dialects.postgresql import insert

from core.logger import get_logger
from db.enums import KeywordKind
from db.models import UserKeyword
from db.repositories.base import BaseRepository, handle_db_errors

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class KeywordAddResult:
    """Итог добавления списка слов."""

    added: Sequence[str]
    skipped: Sequence[str]

    @property
    def has_additions(self) -> bool:
        """Появилось ли хоть одно новое правило."""
        return bool(self.added)


class KeywordRepository(BaseRepository[UserKeyword]):
    """Доступ к словам фильтра.

    Слова добавляются пачкой: человек присылает их одной строкой через
    запятую, и вставлять по одному значило бы столько же обращений к базе,
    сколько слов в сообщении. Повторы отсекает уникальный индекс через
    ``ON CONFLICT DO NOTHING`` — предварительная проверка «а нет ли уже
    такого» оставляла бы окно гонки и всё равно требовала бы запроса.
    """

    model: ClassVar[type[UserKeyword]] = UserKeyword

    @handle_db_errors
    async def list_for_user(
        self,
        user_id: int,
        kind: KeywordKind | None = None,
        *,
        only_active: bool = False,
    ) -> Sequence[UserKeyword]:
        """Возвращает правила фильтра пользователя.

        :param user_id: Владелец.
        :param kind: Ограничение по роли; ``None`` — оба вида.
        :param only_active: Возвращать только включённые правила.
        :return: Правила, отсортированные по слову.
        """
        stmt = select(UserKeyword).where(UserKeyword.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(UserKeyword.kind == kind)
        if only_active:
            stmt = stmt.where(UserKeyword.is_active.is_(True))
        stmt = stmt.order_by(UserKeyword.word)
        return (await self._session.execute(stmt)).scalars().all()

    @handle_db_errors
    async def count_for_user(self, user_id: int, kind: KeywordKind | None = None) -> int:
        """Считает правила пользователя."""
        stmt = select(func.count()).select_from(UserKeyword).where(UserKeyword.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(UserKeyword.kind == kind)
        return int(await self._session.scalar(stmt) or 0)

    @handle_db_errors
    async def add_many(
        self,
        user_id: int,
        kind: KeywordKind,
        words: Iterable[str],
    ) -> KeywordAddResult:
        """Добавляет слова, пропуская уже существующие.

        Слова обязаны прийти нормализованными: за это отвечает
        :meth:`UserKeyword.normalize`, и то же требование продублировано
        CHECK-ограничением в базе.

        :param user_id: Владелец правил.
        :param kind: Роль слов — триггеры или стоп-слова.
        :param words: Нормализованные слова.
        :return: Что добавилось и что было пропущено как дубль.
        """
        unique = list(dict.fromkeys(words))
        if not unique:
            return KeywordAddResult(added=(), skipped=())

        stmt = (
            insert(UserKeyword)
            .values([{"user_id": user_id, "kind": kind, "word": word} for word in unique])
            .on_conflict_do_nothing(index_elements=["user_id", "kind", "word"])
            .returning(UserKeyword.word)
        )
        inserted = set((await self._session.execute(stmt)).scalars().all())

        added = [word for word in unique if word in inserted]
        skipped = [word for word in unique if word not in inserted]

        logger.info(
            "Пользователь %s добавил %d слов (%s), пропущено дублей: %d",
            user_id, len(added), kind.value, len(skipped),
        )
        return KeywordAddResult(added=added, skipped=skipped)

    @handle_db_errors
    async def remove(self, user_id: int, keyword_id: int) -> bool:
        """Удаляет одно правило пользователя.

        Владелец входит в условие: идентификатор приходит от клиента, и
        без проверки чужое правило удалялось бы по подобранному номеру.

        :param user_id: Владелец правила.
        :param keyword_id: Идентификатор правила.
        :return: ``True``, если правило было удалено.
        """
        stmt = sql_delete(UserKeyword).where(
            UserKeyword.id == keyword_id,
            UserKeyword.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)

    @handle_db_errors
    async def clear(self, user_id: int, kind: KeywordKind | None = None) -> int:
        """Удаляет правила пользователя.

        :param user_id: Владелец правил.
        :param kind: Ограничение по роли; ``None`` — очистить всё.
        :return: Количество удалённых правил.
        """
        stmt = sql_delete(UserKeyword).where(UserKeyword.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(UserKeyword.kind == kind)

        result = await self._session.execute(stmt)
        removed = int(result.rowcount or 0)
        logger.info(
            "Очищен фильтр пользователя %s (%s): удалено %d",
            user_id, kind.value if kind else "все", removed,
        )
        return removed
