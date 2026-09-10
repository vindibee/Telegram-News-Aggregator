"""Отчёт по переходам для владельца ссылок."""

from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from aiogram.filters import Command
from aiogram import Router
from aiogram.types import Message

from core.config import Settings
from core.logger import get_logger
from db.models import User
from db.uow import UnitOfWork
from services.i18n import Translator
from services.tracker import AnalyticsReport, AnalyticsService
from tg_bot.flags import rate_limit
from tg_bot.keyboards import kb_to_channels

logger = get_logger(__name__)

router = Router(name="stats")

#: Сколько символов адреса показывать в подборке.
_MAX_TITLE = 40


@router.message(Command("stats"), **rate_limit(10, 60, scope="stats"))
async def cmd_stats(
    message: Message,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
) -> None:
    """Показывает статистику по трекинговым ссылкам."""
    if not settings.tracker.enabled:
        await message.answer(i18n("stats.disabled"))
        return

    report = await AnalyticsService(uow.links).build_report(user.id)
    await message.answer(
        _render(report, i18n),
        reply_markup=kb_to_channels(i18n),
        disable_web_page_preview=True,
    )


def _render(report: AnalyticsReport, i18n: Translator) -> str:
    """Собирает текст отчёта.

    :param report: Готовый отчёт.
    :param i18n: Локализатор.
    :return: HTML для отправки.
    """
    if report.is_empty:
        return i18n("stats.empty")

    lines = [
        i18n("stats.header"),
        "",
        i18n(
            "stats.totals",
            links=report.totals.links,
            clicks=report.totals.clicks,
            unique=report.totals.unique_clicks,
        ),
    ]

    if report.top:
        lines.append(i18n("stats.top_header"))
        for index, link in enumerate(report.top, start=1):
            lines.append(
                i18n(
                    "stats.item",
                    index=index,
                    clicks=link.clicks,
                    url=escape(link.target_url),
                    title=escape(_title(link.target_url)),
                )
            )

    return "\n".join(lines)


def _title(url: str) -> str:
    """Делает из адреса короткую подпись.

    Целиком адрес в списке нечитаем, а домена с началом пути достаточно,
    чтобы человек узнал свою ссылку.
    """
    parsed = urlparse(url)
    label = f"{parsed.hostname or url}{parsed.path or ''}".rstrip("/")
    if len(label) <= _MAX_TITLE:
        return label
    return f"{label[: _MAX_TITLE - 1]}…"
