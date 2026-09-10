"""Трекинг переходов: короткие ссылки и журнал кликов."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.logger import get_logger
from db.base import Base
from db.mixins import IdMixin, TimestampMixin

if TYPE_CHECKING:
    from db.models.post import Post
    from db.models.user import User

logger = get_logger(__name__)

#: Длина токена короткой ссылки в символах.
LINK_TOKEN_LENGTH: Final[int] = 12

#: Максимальная длина целевого адреса — ограничение здравого смысла,
#: браузеры перестают работать заметно раньше.
MAX_TARGET_URL_LENGTH: Final[int] = 2048


class TrackedLink(Base, IdMixin, TimestampMixin):
    """Короткая ссылка, по переходам которой собирается статистика.

    Отдельная сущность, а не поле в журнале кликов: ссылка живёт дольше
    любого клика, у неё есть владелец, срок действия и агрегированный
    счётчик. Хранить целевой адрес в каждой строке журнала значило бы
    дублировать его миллионы раз.
    """

    __tablename__ = "tracked_links"

    #: Токен из URL. Генерируется :mod:`secrets`: перебираемый счётчик
    #: позволил бы читать чужую статистику простым инкрементом.
    token: Mapped[str] = mapped_column(String(32), nullable=False)

    target_url: Mapped[str] = mapped_column(Text, nullable=False)

    #: Пост, из которого ведёт ссылка. Ссылка может существовать и сама по
    #: себе — например, в рекламной подписи канала.
    post_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("posts.id", ondelete="SET NULL"),
        nullable=True,
    )
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Денормализованный счётчик переходов. Считать `COUNT(*)` по журналу
    #: на каждый показ статистики нельзя: журнал растёт быстрее всех
    #: остальных таблиц, а счётчик обновляется тем же `UPDATE`, что и
    #: вставка клика, в одной транзакции.
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    unique_clicks: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    post: Mapped[Post | None] = relationship("Post", lazy="raise")
    owner: Mapped[User | None] = relationship(
        "User", back_populates="tracked_links", foreign_keys=[owner_id], lazy="raise"
    )
    click_logs: Mapped[list[ClickLog]] = relationship(
        "ClickLog",
        back_populates="link",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("token", name="uq_tracked_links_token"),
        CheckConstraint("clicks >= 0", name="clicks_non_negative"),
        CheckConstraint("unique_clicks >= 0", name="unique_clicks_non_negative"),
        CheckConstraint("unique_clicks <= clicks", name="unique_clicks_within_total"),
        CheckConstraint("length(target_url) > 0", name="target_url_present"),
        Index("ix_tracked_links_post_id", "post_id"),
        Index("ix_tracked_links_owner_id_created_at", "owner_id", "created_at"),
        # Редирект ищет ссылку по токену и только среди действующих.
        Index(
            "ix_tracked_links_active_token",
            "token",
            postgresql_where=text("is_active"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<TrackedLink id={self.id} token={self.token!r} clicks={self.clicks}>"

    def is_available(self, moment: datetime) -> bool:
        """Действует ли ссылка в указанный момент.

        :param moment: Момент проверки (timezone-aware).
        :return: Можно ли выполнять переход.
        """
        if not self.is_active:
            return False
        return self.expires_at is None or moment < self.expires_at

    def register_click(self, *, unique: bool) -> None:
        """Учитывает переход в агрегатах ссылки.

        :param unique: Первый ли это переход данного посетителя.
        """
        self.clicks += 1
        if unique:
            self.unique_clicks += 1

    @property
    def conversion_rate(self) -> float:
        """Доля уникальных переходов среди всех.

        :return: Значение от 0 до 1; ноль при отсутствии переходов.
        """
        if self.clicks <= 0:
            return 0.0
        return self.unique_clicks / self.clicks

    @staticmethod
    def generate_token(length: int = LINK_TOKEN_LENGTH) -> str:
        """Генерирует токен короткой ссылки.

        :param length: Длина токена (не меньше 8 символов).
        :return: Строка из URL-безопасных символов.
        :raises ValueError: Запрошена слишком малая длина.
        """
        if length < 8:
            raise ValueError(f"Длина токена должна быть не меньше 8, получено: {length}")
        # token_urlsafe возвращает примерно 1.3 символа на байт — берём с
        # запасом и обрезаем до точной длины.
        return secrets.token_urlsafe(length)[:length]

    @staticmethod
    def validate_target_url(url: str) -> str:
        """Проверяет целевой адрес перед сохранением.

        Разрешены только ``http`` и ``https``: адреса вида ``javascript:``
        и ``data:`` превратили бы редирект в готовый вектор атаки на тех,
        кто по нему перейдёт.

        :param url: Целевой адрес.
        :return: Нормализованный адрес.
        :raises ValueError: Пустой, слишком длинный или небезопасный адрес.
        """
        value = url.strip()
        if not value:
            raise ValueError("Целевой адрес не может быть пустым.")
        if len(value) > MAX_TARGET_URL_LENGTH:
            raise ValueError(
                f"Целевой адрес длиннее {MAX_TARGET_URL_LENGTH} символов: {len(value)}."
            )
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError(f"Разрешены только адреса http и https, получено: {value[:64]!r}")
        return value


class ClickLog(Base, IdMixin):
    """Запись о переходе по короткой ссылке.

    Самая быстрорастущая таблица продукта, поэтому в ней нет ни одного
    лишнего поля и ни одного индекса сверх необходимого для отчётов.
    Отметок времени изменения тоже нет: строка неизменяема по смыслу —
    клик уже случился.

    При выходе на миллионы событий таблицу следует секционировать по
    ``clicked_at``: удаление старых данных превращается в ``DROP``
    секции вместо долгого ``DELETE``.
    """

    __tablename__ = "click_logs"

    link_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tracked_links.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Посетитель, если его удалось опознать. Клик приходит из браузера,
    #: где пользователь Telegram неизвестен, поэтому поле необязательное.
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    #: Отпечаток посетителя: HMAC от IP и User-Agent. Сам адрес не
    #: сохраняется — он персональные данные, а для подсчёта уникальных
    #: переходов достаточно необратимого отпечатка.
    visitor_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Первый ли переход этого посетителя по этой ссылке.
    is_unique: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    referer: Mapped[str | None] = mapped_column(String(255), nullable=True)

    link: Mapped[TrackedLink] = relationship(
        "TrackedLink", back_populates="click_logs", lazy="raise"
    )
    user: Mapped[User | None] = relationship("User", lazy="raise")

    __table_args__ = (
        # Уникальность посетителя в пределах ссылки: именно она делает
        # подсчёт уникальных переходов идемпотентным, а не счётчик в коде.
        UniqueConstraint("link_id", "visitor_hash", name="uq_click_logs_link_visitor"),
        Index("ix_click_logs_link_id_clicked_at", "link_id", "clicked_at"),
        Index("ix_click_logs_clicked_at", "clicked_at"),
        Index("ix_click_logs_user_id", "user_id", postgresql_where=text("user_id IS NOT NULL")),
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<ClickLog id={self.id} link_id={self.link_id} unique={self.is_unique}>"

    @staticmethod
    def build_visitor_hash(ip: str, user_agent: str | None, secret: str) -> str:
        """Считает отпечаток посетителя.

        HMAC, а не обычный хэш: всё пространство IPv4 перебирается за
        минуты, и SHA-256 от адреса не скрывает ничего.

        :param ip: IP-адрес посетителя.
        :param user_agent: Заголовок ``User-Agent``.
        :param secret: Серверный секрет.
        :return: Отпечаток в виде 64 hex-символов.
        :raises ValueError: Пустой адрес или пустой секрет.
        """
        address = ip.strip().lower()
        if not address:
            raise ValueError("IP-адрес посетителя не может быть пустым.")
        if not secret:
            raise ValueError("Секрет для вычисления отпечатка не задан.")

        payload = f"{address}:{(user_agent or '').strip()[:200]}"
        return hmac.new(
            key=secret.encode("utf-8"),
            msg=payload.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
