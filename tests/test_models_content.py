"""Тесты моделей контент-конвейера: фильтры и отложенные публикации."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from db.enums import ChannelKind, KeywordKind, ScheduledPostStatus
from db.exceptions import InvalidStateTransitionError
from db.models import MAX_PUBLISH_ATTEMPTS, Post, ScheduledPost, User, UserChannel, UserKeyword
from tests.conftest import FROZEN_NOW, TELEGRAM_ID


def _keyword(user_id: int = 1, **overrides: object) -> UserKeyword:
    """Строит правило фильтра в состоянии, эквивалентном сохранённому."""
    defaults: dict[str, object] = {
        "user_id": user_id,
        "kind": KeywordKind.TRIGGER,
        "word": "python",
        "is_active": True,
    }
    defaults.update(overrides)
    return UserKeyword(**defaults)  # type: ignore[arg-type]


def _scheduled(**overrides: object) -> ScheduledPost:
    """Строит отложенную публикацию в состоянии сразу после вставки."""
    defaults: dict[str, object] = {
        "user_id": 1,
        "target_channel_id": 1,
        "post_id": 1,
        "publish_at": FROZEN_NOW,
        "status": ScheduledPostStatus.PENDING,
        "attempts": 0,
    }
    defaults.update(overrides)
    return ScheduledPost(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Ключевые слова
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Python", "python"),
        ("  МАШИННОЕ   обучение  ", "машинное обучение"),
        ("AI\n\tNews", "ai news"),
    ],
)
def test_normalize_lowercases_and_collapses_whitespace(raw: str, expected: str) -> None:
    # Без нормализации «Python», «python » и «  PYTHON» попали бы в базу
    # как три разных правила, а сравнение при фильтрации шло бы мимо индекса.
    assert UserKeyword.normalize(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\n", "x" * 65])
def test_normalize_rejects_empty_and_oversized_values(raw: str) -> None:
    with pytest.raises(ValueError):
        UserKeyword.normalize(raw)


def test_single_word_matches_only_on_word_boundaries() -> None:
    # Иначе «ии» срабатывало бы внутри «Гавайи», а «pro» — внутри «process».
    keyword = _keyword(word="ии")

    assert keyword.matches("Новости про ИИ и роботов"), "Отдельное слово должно находиться"
    assert not keyword.matches("Отдых на Гавайи"), "Совпадение внутри слова недопустимо"


def test_single_word_match_ignores_case_and_extra_whitespace() -> None:
    keyword = _keyword(word="python")

    assert keyword.matches("PYTHON  3.13   вышел"), "Регистр и пробелы не должны мешать"


def test_phrase_matches_as_substring() -> None:
    keyword = _keyword(word="машинное обучение")

    assert keyword.matches("Курс про машинное обучение стартует"), "Фраза должна находиться"
    assert not keyword.matches("Машинный перевод и обучение"), "Разорванная фраза — не совпадение"


def test_is_phrase_distinguishes_multiword_rules() -> None:
    assert _keyword(word="машинное обучение").is_phrase
    assert not _keyword(word="python").is_phrase


@pytest.mark.parametrize("content", ["", "   "])
def test_matches_returns_false_for_empty_content(content: str) -> None:
    assert not _keyword().matches(content)


@pytest.mark.db
async def test_same_word_in_both_roles_is_allowed_for_one_user(db_session, user: User) -> None:
    # Слово-триггер у одного человека вполне может быть стоп-словом
    # у другого — и даже у него самого в другой роли.
    db_session.add(_keyword(user.id, kind=KeywordKind.TRIGGER))
    db_session.add(_keyword(user.id, kind=KeywordKind.STOP))

    await db_session.commit()

    assert True, "Оба правила должны сохраниться"


@pytest.mark.db
async def test_duplicate_word_in_same_role_is_rejected_by_database(db_session, user: User) -> None:
    db_session.add(_keyword(user.id))
    await db_session.commit()

    db_session.add(_keyword(user.id))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_database_refuses_denormalized_word(db_session, user: User) -> None:
    # Последний рубеж: даже если прикладной код забудет normalize(),
    # значение в верхнем регистре в базу не попадёт.
    db_session.add(_keyword(user.id, word="Python"))

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_empty_word_is_rejected_by_database(db_session, user: User) -> None:
    db_session.add(_keyword(user.id, word=""))

    with pytest.raises(IntegrityError):
        await db_session.commit()


# --------------------------------------------------------------------------- #
# Отложенные публикации
# --------------------------------------------------------------------------- #


def test_is_due_only_for_pending_records_whose_time_has_come() -> None:
    scheduled = _scheduled(publish_at=FROZEN_NOW)

    assert scheduled.is_due(FROZEN_NOW), "Наступивший срок — пора публиковать"
    assert not scheduled.is_due(FROZEN_NOW - timedelta(minutes=1)), "До срока публиковать рано"

    scheduled.status = ScheduledPostStatus.CANCELLED
    assert not scheduled.is_due(FROZEN_NOW), "Отменённая запись из очереди выбывает"


def test_mark_published_records_message_and_is_idempotent() -> None:
    scheduled = _scheduled()

    assert scheduled.mark_published(555, FROZEN_NOW) is True
    assert scheduled.status is ScheduledPostStatus.PUBLISHED
    assert scheduled.message_id == 555
    assert scheduled.published_at == FROZEN_NOW
    assert scheduled.is_final, "Опубликованная запись — конечное состояние"

    assert scheduled.mark_published(555, FROZEN_NOW) is False, "Повтор не должен быть ошибкой"


def test_mark_published_rejects_non_positive_message_id() -> None:
    with pytest.raises(ValueError):
        _scheduled().mark_published(0, FROZEN_NOW)


def test_failed_attempt_keeps_record_in_queue_until_attempts_run_out() -> None:
    # Одна сетевая ошибка не должна навсегда выбрасывать пост из очереди.
    scheduled = _scheduled()

    for attempt in range(1, MAX_PUBLISH_ATTEMPTS):
        scheduled.mark_failed(f"сбой {attempt}")
        assert scheduled.status is ScheduledPostStatus.PENDING, (
            f"После {attempt} попытки запись должна остаться в очереди"
        )

    scheduled.mark_failed("последний сбой")

    assert scheduled.status is ScheduledPostStatus.FAILED, "Попытки исчерпаны — запись провалена"
    assert scheduled.attempts == MAX_PUBLISH_ATTEMPTS
    assert scheduled.is_exhausted


def test_mark_failed_truncates_long_error_text() -> None:
    scheduled = _scheduled()
    scheduled.mark_failed("x" * 900)

    assert len(scheduled.last_error) == 500


def test_reschedule_returns_failed_record_to_queue_and_resets_attempts() -> None:
    scheduled = _scheduled()
    for _ in range(MAX_PUBLISH_ATTEMPTS):
        scheduled.mark_failed("сбой")

    later = FROZEN_NOW + timedelta(hours=1)
    scheduled.reschedule(later)

    assert scheduled.status is ScheduledPostStatus.PENDING
    assert scheduled.publish_at == later
    assert scheduled.attempts == 0, "Перенос даёт записи новые попытки"


def test_published_record_cannot_be_rescheduled_or_cancelled() -> None:
    scheduled = _scheduled()
    scheduled.mark_published(555, FROZEN_NOW)

    with pytest.raises(InvalidStateTransitionError):
        scheduled.reschedule(FROZEN_NOW + timedelta(hours=1))

    with pytest.raises(InvalidStateTransitionError):
        scheduled.cancel()


def test_cancel_is_idempotent() -> None:
    scheduled = _scheduled()

    assert scheduled.cancel() is True
    assert scheduled.cancel() is False


@pytest.mark.db
async def test_same_post_cannot_be_scheduled_twice_into_one_channel(
    db_session,
    user: User,
) -> None:
    # Повторный тап по кнопке «Опубликовать» — обычное дело.
    channel = UserChannel(
        user_id=user.id,
        kind=ChannelKind.TARGET,
        chat_id=-100500,
        title="Цель",
        bot_is_admin=True,
    )
    post = Post(channel_name="habr_com", message_id=1, post_time=FROZEN_NOW, content="Текст")
    db_session.add_all([channel, post])
    await db_session.commit()

    db_session.add(
        _scheduled(user_id=user.id, target_channel_id=channel.id, post_id=post.id)
    )
    await db_session.commit()

    db_session.add(
        _scheduled(user_id=user.id, target_channel_id=channel.id, post_id=post.id)
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_published_status_without_message_id_is_rejected_by_database(
    db_session,
    user: User,
) -> None:
    channel = UserChannel(
        user_id=user.id, kind=ChannelKind.TARGET, chat_id=-100501, bot_is_admin=True
    )
    post = Post(channel_name="habr_com", message_id=2, post_time=FROZEN_NOW, content="Текст")
    db_session.add_all([channel, post])
    await db_session.commit()

    db_session.add(
        _scheduled(
            user_id=user.id,
            target_channel_id=channel.id,
            post_id=post.id,
            status=ScheduledPostStatus.PUBLISHED,
            published_at=FROZEN_NOW,
            message_id=None,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_post_in_the_queue_cannot_be_deleted(db_session, user: User) -> None:
    # RESTRICT: иначе воркер получил бы запись очереди без содержимого.
    channel = UserChannel(
        user_id=user.id, kind=ChannelKind.TARGET, chat_id=-100502, bot_is_admin=True
    )
    post = Post(channel_name="habr_com", message_id=3, post_time=FROZEN_NOW, content="Текст")
    db_session.add_all([channel, post])
    await db_session.commit()

    db_session.add(_scheduled(user_id=user.id, target_channel_id=channel.id, post_id=post.id))
    await db_session.commit()

    await db_session.delete(post)

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.db
async def test_post_keeps_existing_when_its_source_channel_is_removed(
    db_session,
    make_user,
) -> None:
    # Пост — общий архив: он переживает отключение канала конкретным
    # пользователем, иначе дедупликация теряла бы оригиналы.
    owner = await make_user(telegram_id=TELEGRAM_ID + 5)
    channel = UserChannel(user_id=owner.id, kind=ChannelKind.SOURCE, username="habr_com")
    db_session.add(channel)
    await db_session.commit()

    post = Post(
        channel_name="habr_com",
        message_id=4,
        post_time=FROZEN_NOW,
        content="Текст",
        source_channel_id=channel.id,
    )
    db_session.add(post)
    await db_session.commit()

    await db_session.delete(channel)
    await db_session.commit()

    survivor = await db_session.get(Post, post.id)
    assert survivor is not None, "Пост не должен исчезать вместе с каналом"
    await db_session.refresh(survivor)
    assert survivor.source_channel_id is None, "Ссылка на удалённый канал обнуляется"
