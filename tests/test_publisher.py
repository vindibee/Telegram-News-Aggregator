"""Тесты планировщика публикаций и отзыва доступа по истёкшей подписке."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import CopyMessage, SendMessage
from aiogram.types import MessageId

from core.config import Settings
from db.enums import ChannelKind, ScheduledPostStatus, SubscriptionStatus
from db.models import Post, ScheduledPost, User, UserChannel
from services.i18n import TranslationManager
from worker.tasks import PublishScheduledPostsTask, SubscriptionExpirationTask
from tests.conftest import FROZEN_NOW, TELEGRAM_ID

pytestmark = pytest.mark.db


@pytest.fixture
def translations() -> TranslationManager:
    """Каталоги переводов проекта."""
    return TranslationManager.from_directory()


async def _prepare(
    db_session,
    user: User,
    *,
    publish_at=FROZEN_NOW,
    bot_is_admin: bool = True,
    with_subscription: bool = True,
    message_id: int = 501,
) -> ScheduledPost:
    """Готовит канал, пост и запись очереди."""
    if with_subscription:
        from tests.conftest import build_subscription

        db_session.add(
            build_subscription(
                user.id,
                status=SubscriptionStatus.ACTIVE,
                started_at=FROZEN_NOW - timedelta(days=1),
            )
        )

    # Канал у пользователя один: уникальность (user_id, kind, chat_id) не
    # даст создать второй такой же, а тестам нужны две записи очереди.
    from sqlalchemy import select

    channel = (
        await db_session.execute(
            select(UserChannel).where(
                UserChannel.user_id == user.id, UserChannel.kind == ChannelKind.TARGET
            )
        )
    ).scalar_one_or_none()

    if channel is None:
        channel = UserChannel(
            user_id=user.id,
            kind=ChannelKind.TARGET,
            chat_id=-100500,
            title="Мой канал",
            bot_is_admin=bot_is_admin,
            is_active=True,
        )
        db_session.add(channel)

    post = Post(
        channel_name="habr_com",
        message_id=message_id,
        post_time=FROZEN_NOW,
        content="Текст новости",
    )
    db_session.add(post)
    await db_session.commit()

    entry = ScheduledPost(
        user_id=user.id,
        target_channel_id=channel.id,
        post_id=post.id,
        publish_at=publish_at,
        status=ScheduledPostStatus.PENDING,
        attempts=0,
    )
    db_session.add(entry)
    await db_session.commit()
    return entry


def _task(uow_factory, bot, settings: Settings, translations: TranslationManager):
    """Собирает задачу публикации с подменённым нотификатором."""
    notifier = AsyncMock()
    notifier.reserve_slot = AsyncMock()
    return PublishScheduledPostsTask(uow_factory, bot, notifier, settings, translations)


# --------------------------------------------------------------------------- #
# Публикация
# --------------------------------------------------------------------------- #


async def test_due_post_is_copied_to_the_channel(
    uow_factory, db_session, user: User, bot, settings, translations, frozen_time
) -> None:
    entry = await _prepare(db_session, user)
    bot.add_result_for(CopyMessage, result=MessageId(message_id=999))

    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.succeeded == 1, f"Публикация не отправлена: {result.details}"
    copied = bot.session.requests_of(CopyMessage)
    assert copied, "Ожидался вызов copy_message"
    assert copied[0].chat_id == -100500

    async with uow_factory() as uow:
        stored = await uow.scheduled.get_by_id(entry.id)
        assert stored.status is ScheduledPostStatus.PUBLISHED
        assert stored.message_id == 999, "Идентификатор сообщения обязан сохраняться"


async def test_post_before_its_time_is_not_touched(
    uow_factory, db_session, user: User, bot, settings, translations, frozen_time
) -> None:
    entry = await _prepare(db_session, user, publish_at=FROZEN_NOW + timedelta(hours=1))

    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.is_empty, "До назначенного времени очередь трогать нельзя"
    async with uow_factory() as uow:
        stored = await uow.scheduled.get_by_id(entry.id)
        assert stored.status is ScheduledPostStatus.PENDING


async def test_copy_failure_falls_back_to_plain_text(
    uow_factory, db_session, user: User, bot, settings, translations, frozen_time
) -> None:
    # Исходное сообщение могли удалить, а канал закрыть. Опубликовать
    # новость без оформления лучше, чем не опубликовать вовсе.
    entry = await _prepare(db_session, user)
    bot.add_result_for(
        CopyMessage, ok=False, error_code=400, description="Bad Request: message to copy not found"
    )

    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.succeeded == 1
    assert bot.session.requests_of(SendMessage), "Должна была уйти текстовая отправка"

    async with uow_factory() as uow:
        stored = await uow.scheduled.get_by_id(entry.id)
        assert stored.status is ScheduledPostStatus.PUBLISHED


async def test_lost_admin_rights_disable_channel_and_cancel_queue(
    uow_factory, db_session, user: User, bot, settings, translations, frozen_time
) -> None:
    # Бота выгнали из администраторов: очередь в этот канал будет только
    # копить неудачные попытки, поэтому она снимается целиком.
    entry = await _prepare(db_session, user)
    second = await _prepare(db_session, user, message_id=502, with_subscription=False)
    bot.add_result_for(
        CopyMessage, ok=False, error_code=403, description="Forbidden: bot was kicked"
    )

    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.failed >= 1
    async with uow_factory() as uow:
        first = await uow.scheduled.get_by_id(entry.id)
        other = await uow.scheduled.get_by_id(second.id)
        channel = await uow.channels.get_by_id(entry.target_channel_id)

    # Потеря прав необратима сама по себе, поэтому отменяются обе записи,
    # включая ту, на которой это вскрылось.
    assert first.status is ScheduledPostStatus.CANCELLED
    assert other.status is ScheduledPostStatus.CANCELLED, "Очередь в канал должна сниматься"
    assert channel.bot_is_admin is False, "Права нужно пометить утраченными"


async def test_flood_control_leaves_post_in_the_queue(
    uow_factory, db_session, user: User, bot, settings, translations, frozen_time
) -> None:
    # Ждать здесь — задерживать остальные публикации; запись уйдёт
    # следующим проходом.
    entry = await _prepare(db_session, user)
    bot.add_result_for(
        CopyMessage, ok=False, error_code=429, description="Too Many Requests", retry_after=5
    )

    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.failed == 1
    async with uow_factory() as uow:
        stored = await uow.scheduled.get_by_id(entry.id)

    assert stored.status is ScheduledPostStatus.PENDING, "Запись должна остаться в очереди"
    assert stored.attempts == 1, "Попытка должна быть учтена"


async def test_expired_subscription_cancels_publication(
    uow_factory, db_session, user: User, bot, settings, translations, frozen_time
) -> None:
    # Между постановкой в очередь и публикацией проходят часы, и подписка
    # успевает закончиться. Публиковать по ней — раздавать платное даром.
    entry = await _prepare(db_session, user, with_subscription=False)

    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.details["cancelled"] == 1
    assert not bot.session.requests_of(CopyMessage), "Отправки быть не должно"

    async with uow_factory() as uow:
        stored = await uow.scheduled.get_by_id(entry.id)
    assert stored.status is ScheduledPostStatus.CANCELLED


async def test_channel_without_rights_is_skipped(
    uow_factory, db_session, user: User, bot, settings, translations, frozen_time
) -> None:
    entry = await _prepare(db_session, user, bot_is_admin=False)

    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.details["cancelled"] == 1
    assert not bot.session.requests_of(CopyMessage)

    async with uow_factory() as uow:
        stored = await uow.scheduled.get_by_id(entry.id)
    assert stored.status is ScheduledPostStatus.CANCELLED, (
        "Выключенный канал не должен копить неудачные попытки"
    )


async def test_empty_queue_reports_no_work(
    uow_factory, bot, settings, translations, frozen_time
) -> None:
    result = await _task(uow_factory, bot, settings, translations).run()

    assert result.is_empty
    assert result.describe() == "нет работы"


# --------------------------------------------------------------------------- #
# Отзыв доступа по истёкшей подписке
# --------------------------------------------------------------------------- #


async def test_expiration_disables_target_channels_and_cancels_queue(
    uow_factory,
    db_session,
    make_user,
    bot,
    settings,
    translations,
    mock_notifier,
    frozen_time,
) -> None:
    owner = await make_user(telegram_id=TELEGRAM_ID + 300)
    from tests.conftest import build_subscription

    db_session.add(
        build_subscription(
            owner.id,
            status=SubscriptionStatus.ACTIVE,
            started_at=FROZEN_NOW - timedelta(days=40),
            expires_at=FROZEN_NOW - timedelta(hours=1),
        )
    )
    source = UserChannel(user_id=owner.id, kind=ChannelKind.SOURCE, username="habr_com")
    target = UserChannel(
        user_id=owner.id,
        kind=ChannelKind.TARGET,
        chat_id=-100777,
        bot_is_admin=True,
        is_active=True,
    )
    post = Post(channel_name="habr_com", message_id=601, post_time=FROZEN_NOW, content="Текст")
    db_session.add_all([source, target, post])
    await db_session.commit()

    db_session.add(
        ScheduledPost(
            user_id=owner.id,
            target_channel_id=target.id,
            post_id=post.id,
            publish_at=FROZEN_NOW + timedelta(hours=2),
            status=ScheduledPostStatus.PENDING,
            attempts=0,
        )
    )
    await db_session.commit()

    task = SubscriptionExpirationTask(uow_factory, mock_notifier, settings, translations)
    result = await task.run()

    assert result.processed == 1
    assert result.details["suspended_channels"] == 1

    async with uow_factory() as uow:
        targets = await uow.channels.list_for_user(owner.id, ChannelKind.TARGET, only_active=True)
        sources = await uow.channels.list_for_user(owner.id, ChannelKind.SOURCE, only_active=True)
        queued = await uow.scheduled.count_pending(owner.id)

    assert targets == [], "Цели публикации должны отключиться"
    assert len(sources) == 1, "Источники остаются: чтение доступно и без подписки"
    assert queued == 0, "Очередь публикаций должна сниматься"


async def test_expiry_notice_carries_a_renewal_button(
    uow_factory,
    db_session,
    make_user,
    settings,
    translations,
    mock_notifier,
    frozen_time,
) -> None:
    from worker.tasks import ExpiryNotificationTask
    from tests.conftest import build_subscription

    owner = await make_user(telegram_id=TELEGRAM_ID + 301)
    db_session.add(
        build_subscription(
            owner.id,
            status=SubscriptionStatus.ACTIVE,
            started_at=FROZEN_NOW - timedelta(days=29),
            expires_at=FROZEN_NOW + timedelta(hours=12),
        )
    )
    await db_session.commit()

    task = ExpiryNotificationTask(uow_factory, mock_notifier, settings, translations)
    result = await task.run()

    assert result.succeeded == 1
    markup = mock_notifier.send.await_args.kwargs.get("reply_markup")
    assert markup is not None, "Уведомление обязано нести кнопку продления"
    assert markup.inline_keyboard[0][0].callback_data == "menu:plans"
