"""Хендлеры поиска по архиву новостей.

Запрос принимается через FSM, потому что поисковая строка — свободный
ввод: она не помещается в ``callback_data``, где всего 64 байта.

Там же, в состоянии, запрос и остаётся между страницами выдачи: кнопки
«Вперёд» и «Назад» несут только номер страницы. Класть запрос в
``callback_data`` было бы нельзя не только из-за размера — эти данные
приходят от клиента, и подменённая строка вела бы к чужой выдаче под
видом перелистывания.

Поиск — возможность платного тарифа, поэтому доступ проверяется в обоих
входах: и при вводе запроса, и при перелистывании. Подписка успевает
закончиться между страницами.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from core.config import Settings
from core.logger import get_logger
from db.models import User
from db.uow import UnitOfWork
from services.i18n import Translator
from services.search import SearchPage, SearchQueryError, SearchService
from tg_bot.callbacks import ACTION_SEARCH, MenuCB, SearchPageCB
from tg_bot.flags import rate_limit
from tg_bot.keyboards import kb_search_results, kb_subscription
from tg_bot.states import SearchSG
from tg_bot.utils import get_message, safe_edit_text

logger = get_logger(__name__)

router = Router(name="search")

#: Ключ, под которым запрос хранится в состоянии FSM.
_QUERY_KEY = "search_query"


@router.message(Command("search"), **rate_limit(10, 60, scope="search"))
async def cmd_search(
    message: Message,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Запрашивает поисковую строку."""
    if not await _has_access(user, uow):
        await _deny(message, i18n)
        await state.clear()
        return

    await state.set_state(SearchSG.waiting_for_query)
    await message.answer(i18n("search.prompt"))


@router.callback_query(MenuCB.filter(F.action == ACTION_SEARCH))
async def open_search(
    callback: CallbackQuery,
    user: User,
    uow: UnitOfWork,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Открывает поиск по кнопке."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    if not await _has_access(user, uow):
        await _deny(target, i18n)
        await state.clear()
        return

    await state.set_state(SearchSG.waiting_for_query)
    await safe_edit_text(target, i18n("search.prompt"))


@router.message(SearchSG.waiting_for_query, Command("cancel"))
async def cancel_search(message: Message, i18n: Translator, state: FSMContext) -> None:
    """Прерывает ввод запроса."""
    await state.clear()
    await message.answer(i18n("cabinet.cancelled"))


@router.message(SearchSG.waiting_for_query, F.text)
async def receive_query(
    message: Message,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Выполняет поиск по присланному запросу."""
    if not await _has_access(user, uow):
        await _deny(message, i18n)
        await state.clear()
        return

    service = SearchService(uow.posts)
    try:
        page = await service.search(message.text or "", page=0)
    except SearchQueryError as exc:
        # Состояние не сбрасывается: человек уточнит запрос и пришлёт снова.
        await message.answer(i18n(exc.key))
        return

    # Запрос переживает страницы, поэтому остаётся в состоянии, а само
    # состояние сохраняется: перелистывание не должно требовать /search.
    await state.update_data(**{_QUERY_KEY: page.query})
    await message.answer(
        _render(page, settings, i18n),
        reply_markup=kb_search_results(
            page.page, i18n, has_prev=page.has_prev, has_next=page.has_next
        ),
        disable_web_page_preview=True,
    )


@router.callback_query(SearchPageCB.filter())
async def turn_page(
    callback: CallbackQuery,
    callback_data: SearchPageCB,
    user: User,
    uow: UnitOfWork,
    settings: Settings,
    i18n: Translator,
    state: FSMContext,
) -> None:
    """Показывает соседнюю страницу выдачи."""
    await callback.answer()
    target = get_message(callback)
    if target is None:
        return

    if not await _has_access(user, uow):
        await _deny(target, i18n)
        await state.clear()
        return

    data = await state.get_data()
    query = data.get(_QUERY_KEY)
    if not query:
        # Состояние потеряно: бот перезапускался с хранилищем в памяти
        # либо диалог давно закончился.
        logger.info("Перелистывание без сохранённого запроса: user_id=%s", user.id)
        await safe_edit_text(target, i18n("search.expired"))
        return

    service = SearchService(uow.posts)
    try:
        page = await service.search(query, page=callback_data.page)
    except SearchQueryError as exc:
        await safe_edit_text(target, i18n(exc.key))
        return

    await safe_edit_text(
        target,
        _render(page, settings, i18n),
        kb_search_results(page.page, i18n, has_prev=page.has_prev, has_next=page.has_next),
    )


async def _has_access(user: User, uow: UnitOfWork) -> bool:
    """Проверяет, открыт ли пользователю поиск по архиву.

    Поиск заявлен возможностью платного тарифа, поэтому доступ решается
    наличием действующей подписки — в том числе пробной.

    :param user: Пользователь.
    :param uow: Единица работы.
    :return: ``True``, если доступ есть.
    """
    subscription = await uow.subscriptions.get_live(user.id)
    return subscription is not None


async def _deny(message: Message, i18n: Translator) -> None:
    """Сообщает, что поиск доступен по подписке."""
    await message.answer(i18n("search.locked"), reply_markup=kb_subscription(False, i18n))


def _render(page: SearchPage, settings: Settings, i18n: Translator) -> str:
    """Собирает текст страницы выдачи.

    :param page: Страница результатов.
    :param settings: Настройки приложения (нужна таймзона показа).
    :param i18n: Локализатор.
    :return: Готовый HTML.
    """
    if page.is_empty:
        return i18n("search.nothing", query=escape(page.query))

    lines = [i18n("search.results", query=escape(page.query), page=page.page + 1), ""]
    offset = page.page * len(page.results)

    for index, result in enumerate(page.results, start=1):
        moment = datetime.fromisoformat(result.post_time_iso).astimezone(
            settings.display_timezone
        )
        lines.append(
            i18n(
                "search.item",
                index=offset + index,
                url=escape(result.source_url),
                channel=escape(result.channel_name),
                date=moment.strftime("%d.%m.%Y %H:%M"),
                # Фрагмент уже экранирован сервисом: повторное
                # экранирование превратило бы подсветку в текст.
                snippet=result.snippet,
            )
        )
        lines.append("")

    return "\n".join(lines).strip()
