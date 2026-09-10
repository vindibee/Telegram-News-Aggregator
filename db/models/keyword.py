"""Пользовательские фильтры новостей: триггеры и стоп-слова."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.enums import KeywordKind, pg_enum
from db.mixins import IdMixin

if TYPE_CHECKING:
    from db.models.user import User

logger = get_logger(__name__)

#: Максимальная длина ключевого слова или фразы.
MAX_KEYWORD_LENGTH: Final[int] = 64

#: Схлопывание любых пробельных последовательностей в один пробел.
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


class UserKeyword(Base, IdMixin):
    """Слово или фраза, по которым пользователь фильтрует поток новостей.

    Триггеры и стоп-слова лежат в одной таблице: различаются они одним
    значением ``kind``, а читаются всегда вместе — фильтр применяет обе
    группы за один запрос. Две таблицы означали бы два индекса и два
    запроса ради одного бита информации.

    Слово хранится нормализованным. Иначе ограничение уникальности не
    работает («Python», «python » и «  PYTHON» прошли бы как три разных),
    а сравнение при фильтрации пришлось бы делать через ``lower()`` по
    всей таблице, то есть без индекса.
    """

    __tablename__ = "user_keywords"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[KeywordKind] = mapped_column(
        pg_enum(KeywordKind, "keyword_kind"),
        nullable=False,
    )

    #: Нормализованное значение: нижний регистр, без лишних пробелов.
    word: Mapped[str] = mapped_column(String(MAX_KEYWORD_LENGTH), nullable=False)

    #: Выключенное слово сохраняется, но не участвует в фильтрации:
    #: пользователи регулярно отключают правила «на время» и не должны
    #: терять их формулировки.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship("User", back_populates="keywords", lazy="raise")

    __table_args__ = (
        # Одно и то же слово в одной роли — бессмысленный дубль. При этом
        # слово вправе быть и триггером, и стоп-словом у разных людей,
        # поэтому роль и владелец входят в ключ.
        UniqueConstraint("user_id", "kind", "word", name="uq_user_keywords_word"),
        CheckConstraint("length(word) > 0", name="word_not_empty"),
        CheckConstraint("word = lower(word)", name="word_normalized"),
        # Фильтрация читает активные правила пользователя целиком.
        Index(
            "ix_user_keywords_active",
            "user_id",
            "kind",
            postgresql_where=text("is_active"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<UserKeyword id={self.id} user_id={self.user_id} kind={self.kind} word={self.word!r}>"

    @property
    def is_phrase(self) -> bool:
        """Состоит ли правило из нескольких слов."""
        return " " in self.word

    def matches(self, content: str) -> bool:
        """Проверяет, встречается ли правило в тексте.

        Для одиночного слова совпадение ищется по границам слова: без
        этого «ИИ» срабатывало бы внутри «Гавайи», а «pro» — внутри
        «process». Для фразы достаточно вхождения подстроки: границы у
        многословного выражения и так заданы им самим.

        Полноценная морфология здесь не нужна и была бы вредна: поиск по
        архиву использует стемминг PostgreSQL, а пользовательский фильтр
        должен вести себя предсказуемо и объяснимо.

        :param content: Текст новости.
        :return: ``True``, если правило срабатывает.
        """
        if not content or not self.word:
            return False

        haystack = _WHITESPACE_RE.sub(" ", content.lower())
        if self.is_phrase:
            return self.word in haystack

        return re.search(rf"(?<!\w){re.escape(self.word)}(?!\w)", haystack) is not None

    @staticmethod
    def normalize(raw: str) -> str:
        """Приводит пользовательский ввод к каноническому виду.

        :param raw: Ввод пользователя.
        :return: Слово в нижнем регистре без лишних пробелов.
        :raises ValueError: Пустое или слишком длинное значение.
        """
        value = _WHITESPACE_RE.sub(" ", raw.strip().lower())
        if not value:
            raise ValueError("Ключевое слово не может быть пустым.")
        if len(value) > MAX_KEYWORD_LENGTH:
            raise ValueError(
                f"Ключевое слово длиннее {MAX_KEYWORD_LENGTH} символов: {len(value)}."
            )
        return value
