"""Тесты личного кабинета: разбор ввода, права бота, репозиторий фильтра."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from aiogram.types import Chat, ChatMemberAdministrator, ChatMemberMember, User as TelegramUser

from db.enums import KeywordKind
from db.models import User
from tg_bot.handlers.cabinet import (
    MAX_KEYWORD_LENGTH,
    _check_bot_rights,
    _extract_channel_reference,
    _parse_keywords,
    _parse_kind,
)


def _admin(*, can_post: bool) -> ChatMemberAdministrator:
    """Строит администратора канала с заданным правом публикации."""
    return ChatMemberAdministrator(
        status="administrator",
        user=TelegramUser(id=42, is_bot=True, first_name="bot"),
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=True,
        can_restrict_members=True,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_send_welcome_messages=False,
        can_post_messages=can_post,
    )


# --------------------------------------------------------------------------- #
# Разбор ссылки на целевой канал
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [("@my_channel", "@my_channel"), ("t.me/my_channel", "@my_channel"), ("my_channel", "@my_channel")],
)
def test_channel_reference_accepts_any_form_of_name(make_message, text: str, expected: str) -> None:
    assert _extract_channel_reference(make_message(text)) == expected


def test_channel_reference_prefers_forwarded_origin(make_message) -> None:
    # У приватного канала имени нет вовсе, и сослаться на него можно
    # только пересланным сообщением.
    forwarded = make_message("любой текст", forward_from_chat=Chat(id=-1001234, type="channel"))

    assert _extract_channel_reference(forwarded) == -1001234


@pytest.mark.parametrize("text", ["", "   ", "не ссылка!!!", "ab"])
def test_channel_reference_rejects_garbage(make_message, text: str) -> None:
    assert _extract_channel_reference(make_message(text)) is None


# --------------------------------------------------------------------------- #
# Проверка прав бота
# --------------------------------------------------------------------------- #


async def test_rights_confirmed_for_admin_with_posting(bot) -> None:
    bot.add_result_for(GetChatMember, result=_admin(can_post=True))

    assert await _check_bot_rights(bot, -1001) is True


async def test_admin_without_posting_right_is_refused(bot) -> None:
    # Администратор без права публикации — обычное дело, и отличать его
    # от «не администратора» важно: подсказки разные.
    bot.add_result_for(GetChatMember, result=_admin(can_post=False))

    assert await _check_bot_rights(bot, -1001) == "cabinet.targets.cannot_post"


async def test_plain_member_is_refused(bot) -> None:
    bot.add_result_for(
        GetChatMember,
        result=ChatMemberMember(
            status="member", user=TelegramUser(id=42, is_bot=True, first_name="bot")
        ),
    )

    assert await _check_bot_rights(bot, -1001) == "cabinet.targets.not_admin"


async def test_api_failure_is_treated_as_missing_rights(bot) -> None:
    # «Не смогли спросить» и «прав нет» для пользователя одно и то же:
    # публиковать бот всё равно не сможет.
    bot.add_result_for(
        GetChatMember, ok=False, error_code=400, description="Bad Request: chat not found"
    )

    assert await _check_bot_rights(bot, -1001) == "cabinet.targets.not_admin"


# --------------------------------------------------------------------------- #
# Разбор списка слов
# --------------------------------------------------------------------------- #


def test_keywords_are_normalized_and_deduplicated() -> None:
    words, invalid = _parse_keywords("Python, ПИТОН , python,  машинное   обучение ")

    assert invalid is None
    assert words == ["python", "питон", "машинное обучение"], (
        "Слова должны нормализоваться и терять дубли, сохраняя порядок ввода"
    )


@pytest.mark.parametrize("raw", ["", "   ", " , , "])
def test_empty_input_yields_no_words(raw: str) -> None:
    words, invalid = _parse_keywords(raw)

    assert words == []
    assert invalid is None, "Пустой ввод — не ошибка формата, а отсутствие слов"


def test_oversized_word_is_reported_back_to_the_user() -> None:
    long_word = "x" * (MAX_KEYWORD_LENGTH + 1)

    words, invalid = _parse_keywords(f"python, {long_word}")

    assert words == []
    assert invalid == long_word, "Пользователь должен увидеть, какое именно слово не подошло"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("trigger", KeywordKind.TRIGGER), ("stop", KeywordKind.STOP), ("что-то", None)],
)
def test_kind_is_parsed_from_callback_data(raw: str, expected: KeywordKind | None) -> None:
    assert _parse_kind(raw) is expected


# --------------------------------------------------------------------------- #
# Репозиторий фильтра
# --------------------------------------------------------------------------- #


@pytest.mark.db
async def test_add_many_reports_added_and_skipped(uow, user: User) -> None:
    first = await uow.keywords.add_many(user.id, KeywordKind.TRIGGER, ["python", "релиз"])
    second = await uow.keywords.add_many(user.id, KeywordKind.TRIGGER, ["python", "aiogram"])

    assert list(first.added) == ["python", "релиз"]
    assert list(second.added) == ["aiogram"]
    assert list(second.skipped) == ["python"], "Повтор должен отсекаться, а не падать"


@pytest.mark.db
async def test_same_word_lives_in_both_roles(uow, user: User) -> None:
    await uow.keywords.add_many(user.id, KeywordKind.TRIGGER, ["реклама"])
    result = await uow.keywords.add_many(user.id, KeywordKind.STOP, ["реклама"])

    assert result.has_additions, "Роль входит в ключ: слово может быть и триггером, и стоп-словом"


@pytest.mark.db
async def test_clear_removes_only_requested_kind(uow, user: User) -> None:
    await uow.keywords.add_many(user.id, KeywordKind.TRIGGER, ["python", "релиз"])
    await uow.keywords.add_many(user.id, KeywordKind.STOP, ["реклама"])

    removed = await uow.keywords.clear(user.id, KeywordKind.TRIGGER)

    assert removed == 2
    assert await uow.keywords.count_for_user(user.id, KeywordKind.STOP) == 1
    assert await uow.keywords.count_for_user(user.id, KeywordKind.TRIGGER) == 0


@pytest.mark.db
async def test_remove_ignores_foreign_keyword(uow, user: User, make_user) -> None:
    stranger = await make_user(telegram_id=user.telegram_id + 1)
    await uow.keywords.add_many(user.id, KeywordKind.TRIGGER, ["python"])
    mine = (await uow.keywords.list_for_user(user.id))[0]

    assert await uow.keywords.remove(stranger.id, mine.id) is False
    assert await uow.keywords.remove(user.id, mine.id) is True


@pytest.mark.db
async def test_add_many_with_empty_iterable_is_a_noop(uow, user: User) -> None:
    result = await uow.keywords.add_many(user.id, KeywordKind.TRIGGER, [])

    assert not result.has_additions
    assert await uow.keywords.count_for_user(user.id) == 0
