"""Тесты репозиторного слоя: базовые операции, каналы и связи пользователя."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.orm import selectinload

from db.enums import ChannelKind, Language, SubscriptionStatus
from db.models import Subscription, User, UserChannel
from db.repositories import ChannelRepository, RepositoryError
from tests.conftest import FROZEN_NOW, TELEGRAM_ID

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------- #
# BaseRepository
# --------------------------------------------------------------------------- #


async def test_get_by_id_returns_entity_and_none_for_missing(uow, user: User) -> None:
    assert (await uow.users.get_by_id(user.id)) is not None
    assert (await uow.users.get_by_id(10_000_000)) is None, "Отсутствующий ключ даёт None"


async def test_get_all_filters_orders_and_limits(uow, make_user) -> None:
    for index in range(5):
        await make_user(telegram_id=TELEGRAM_ID + 100 + index, username=f"user{index}")

    page = await uow.users.get_all(
        User.username.is_not(None),
        order_by=User.telegram_id.desc(),
        limit=2,
    )

    assert len(page) == 2, "Лимит должен ограничивать выборку"
    assert page[0].telegram_id > page[1].telegram_id, "Порядок сортировки не применён"


async def test_get_all_offset_skips_leading_rows(uow, make_user) -> None:
    for index in range(3):
        await make_user(telegram_id=TELEGRAM_ID + 200 + index)

    first = await uow.users.get_all(order_by=User.telegram_id, limit=1)
    second = await uow.users.get_all(order_by=User.telegram_id, limit=1, offset=1)

    assert first[0].id != second[0].id, "Смещение должно пропускать записи"


async def test_update_changes_fields_and_returns_fresh_entity(uow, user: User) -> None:
    updated = await uow.users.update(user.id, username="renamed", language=Language.EN)

    assert updated is not None
    assert updated.username == "renamed"
    assert updated.language is Language.EN, "Объект должен вернуться уже с новыми значениями"


async def test_update_of_missing_row_returns_none(uow) -> None:
    assert await uow.users.update(10_000_000, username="ghost") is None


async def test_update_without_fields_is_rejected(uow, user: User) -> None:
    with pytest.raises(ValueError):
        await uow.users.update(user.id)


async def test_repository_errors_are_translated_to_repository_error(uow, user: User) -> None:
    # Ошибка драйвера не должна протекать наружу сырой: слой обязан
    # отдавать своё исключение, иначе прикладной код начнёт ловить
    # SQLAlchemyError и знать о драйвере.
    with pytest.raises(RepositoryError):
        await uow.users.update(user.id, несуществующее_поле="x")


# --------------------------------------------------------------------------- #
# UserRepository
# --------------------------------------------------------------------------- #


async def test_get_with_subscription_eager_loads_live_subscription(
    uow,
    db_session,
    make_user,
    make_subscription,
) -> None:
    owner = await make_user(telegram_id=TELEGRAM_ID + 11)
    await make_subscription(owner, status=SubscriptionStatus.ACTIVE)

    loaded = await uow.users.get_with_subscription(owner.telegram_id)

    assert loaded is not None
    # Обращение к связи не должно приводить к запросу: lazy="raise" всё
    # ещё в силе, и если бы selectinload не сработал, тест упал бы здесь.
    assert len(loaded.subscriptions) == 1
    assert loaded.subscriptions[0].status is SubscriptionStatus.ACTIVE


async def test_get_with_subscription_skips_expired_ones(
    uow,
    make_user,
    make_subscription,
) -> None:
    owner = await make_user(telegram_id=TELEGRAM_ID + 12)
    await make_subscription(owner, status=SubscriptionStatus.EXPIRED)

    loaded = await uow.users.get_with_subscription(owner.telegram_id)

    assert loaded is not None
    assert loaded.subscriptions == [], "Истёкшие подписки не относятся к действующим"


async def test_get_with_subscription_returns_none_for_unknown_user(uow) -> None:
    assert await uow.users.get_with_subscription(-1) is None


async def test_set_referrer_links_once_and_never_overwrites(uow, make_user) -> None:
    invited = await make_user(telegram_id=TELEGRAM_ID + 21)
    first = await make_user(telegram_id=TELEGRAM_ID + 22)
    second = await make_user(telegram_id=TELEGRAM_ID + 23)

    assert await uow.users.set_referrer(invited.id, first.id) is True
    assert await uow.users.set_referrer(invited.id, second.id) is False, (
        "Сменить пригласившего задним числом нельзя"
    )

    stored = await uow.users.get_by_id(invited.id)
    await uow.session.refresh(stored)
    assert stored.referred_by_id == first.id


async def test_set_referrer_rejects_self_invitation(uow, user: User) -> None:
    with pytest.raises(ValueError):
        await uow.users.set_referrer(user.id, user.id)


async def test_set_language_persists_choice(uow, user: User) -> None:
    await uow.users.set_language(user.id, Language.UK)

    stored = await uow.users.get_by_id(user.id)
    await uow.session.refresh(stored)
    assert stored.language is Language.UK


# --------------------------------------------------------------------------- #
# ChannelRepository
# --------------------------------------------------------------------------- #


async def test_connect_creates_channel_once_and_is_idempotent(uow, user: User) -> None:
    first = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com", title="Хабр"
    )
    second = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )

    assert first.created is True
    assert second.already_connected, "Повторное подключение не должно плодить дубли"
    assert second.channel.id == first.channel.id


async def test_connect_reenables_previously_disabled_channel(uow, user: User) -> None:
    # Человек, добавляющий канал заново, ожидает, что тот заработает.
    created = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )
    await uow.channels.set_active(user.id, created.channel.id, active=False)

    again = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )

    assert again.channel.is_active, "Отключённый канал должен включиться заново"


async def test_connect_allows_same_channel_in_both_roles(uow, user: User) -> None:
    source = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )
    target = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.TARGET, username="habr_com", chat_id=-100500
    )

    assert source.channel.id != target.channel.id, "Роль входит в ключ уникальности"


async def test_connect_target_without_chat_id_is_rejected(uow, user: User) -> None:
    with pytest.raises(ValueError):
        await uow.channels.connect(
            user_id=user.id, kind=ChannelKind.TARGET, username="habr_com"
        )


async def test_connect_without_any_identifier_is_rejected(uow, user: User) -> None:
    with pytest.raises(ValueError):
        await uow.channels.connect(user_id=user.id, kind=ChannelKind.SOURCE)


async def test_disconnect_removes_only_own_channel(uow, user: User, make_user) -> None:
    # Идентификатор канала приходит из callback_data, то есть от клиента:
    # без проверки владельца чужой канал удалялся бы по номеру.
    stranger = await make_user(telegram_id=TELEGRAM_ID + 31)
    created = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )

    assert await uow.channels.disconnect(stranger.id, created.channel.id) is False
    assert await uow.channels.get_by_username(user.id, ChannelKind.SOURCE, "habr_com") is not None

    assert await uow.channels.disconnect(user.id, created.channel.id) is True
    assert await uow.channels.get_by_username(user.id, ChannelKind.SOURCE, "habr_com") is None


async def test_list_for_user_filters_by_role_and_activity(uow, user: User) -> None:
    first = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )
    await uow.channels.connect(user_id=user.id, kind=ChannelKind.SOURCE, username="rbc_news")
    await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.TARGET, username="my_channel", chat_id=-100500
    )
    await uow.channels.set_active(user.id, first.channel.id, active=False)

    sources = await uow.channels.list_for_user(user.id, ChannelKind.SOURCE)
    active_sources = await uow.channels.list_for_user(
        user.id, ChannelKind.SOURCE, only_active=True
    )
    everything = await uow.channels.list_for_user(user.id)

    assert len(sources) == 2
    assert len(active_sources) == 1, "Выключенный канал не должен попадать в активные"
    assert len(everything) == 3


async def test_list_active_sources_puts_never_synced_first(uow, user: User) -> None:
    stale = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )
    fresh = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="rbc_news"
    )
    await uow.channels.mark_synced(stale.channel.id, FROZEN_NOW)

    queue = await uow.channels.list_active_sources()

    assert queue[0].id == fresh.channel.id, (
        "Ни разу не синхронизированный канал должен обрабатываться первым"
    )


async def test_list_active_sources_ignores_targets_and_disabled(uow, user: User) -> None:
    source = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )
    await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.TARGET, username="my_channel", chat_id=-100500
    )
    await uow.channels.set_active(user.id, source.channel.id, active=False)

    assert await uow.channels.list_active_sources() == [], (
        "Ни цели публикации, ни выключенные источники в очередь не попадают"
    )


async def test_count_active_reflects_only_enabled_channels(uow, user: User) -> None:
    first = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )
    await uow.channels.connect(user_id=user.id, kind=ChannelKind.SOURCE, username="rbc_news")

    assert await uow.channels.count_active(user.id, ChannelKind.SOURCE) == 2

    await uow.channels.set_active(user.id, first.channel.id, active=False)
    assert await uow.channels.count_active(user.id, ChannelKind.SOURCE) == 1


async def test_mark_failed_then_synced_clears_the_error(uow, user: User) -> None:
    created = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.SOURCE, username="habr_com"
    )

    await uow.channels.mark_failed(created.channel.id, "канал недоступен")
    await uow.session.refresh(created.channel)
    assert created.channel.last_error == "канал недоступен"

    await uow.channels.mark_synced(created.channel.id, FROZEN_NOW)
    await uow.session.refresh(created.channel)
    assert created.channel.last_error is None, "Успешная синхронизация снимает ошибку"


async def test_set_bot_admin_marks_target_publishable(uow, user: User) -> None:
    created = await uow.channels.connect(
        user_id=user.id, kind=ChannelKind.TARGET, chat_id=-100500, title="Цель"
    )
    assert not created.channel.is_publishable, "Без подтверждения прав публиковать нельзя"

    await uow.channels.set_bot_admin(created.channel.id, is_admin=True)
    await uow.session.refresh(created.channel)

    assert created.channel.is_publishable


async def test_parallel_connect_of_one_channel_creates_single_row(
    uow_factory,
    make_user,
    db_session,
) -> None:
    # Два одновременных нажатия «добавить канал» — обычное дело.
    owner = await make_user(telegram_id=TELEGRAM_ID + 41)
    await db_session.commit()
    user_id = owner.id

    async def attempt() -> bool:
        async with uow_factory() as unit:
            result = await unit.channels.connect(
                user_id=user_id, kind=ChannelKind.SOURCE, username="habr_com"
            )
            await unit.commit()
            return result.created

    outcomes = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    created_flags = [item for item in outcomes if item is True]

    async with uow_factory() as unit:
        channels = await unit.channels.list_for_user(user_id, ChannelKind.SOURCE)

    assert len(channels) == 1, f"Создано каналов: {len(channels)}, ожидался один"
    assert len(created_flags) == 1, "Ровно одна попытка должна сообщить о создании"


# --------------------------------------------------------------------------- #
# Unit of Work
# --------------------------------------------------------------------------- #


async def test_unit_of_work_exposes_every_repository(uow) -> None:
    assert uow.users is not None
    assert uow.subscriptions is not None
    assert uow.payments is not None
    assert uow.posts is not None
    assert uow.channels is not None


async def test_unit_of_work_rolls_back_without_commit(uow_factory, db_session) -> None:
    async with uow_factory() as unit:
        await unit.users.get_or_create(telegram_id=TELEGRAM_ID + 51, username="ghost")
        # Выходим без commit — изменения не должны сохраниться.

    async with uow_factory() as unit:
        assert await unit.users.get_by_telegram_id(TELEGRAM_ID + 51) is None, (
            "Выход из UnitOfWork без commit обязан откатывать изменения"
        )


async def test_unit_of_work_transaction_helper_commits_on_success(uow_factory) -> None:
    async def operation(unit) -> int:
        result = await unit.users.get_or_create(telegram_id=TELEGRAM_ID + 52)
        return result.user.id

    user_id = await uow_factory.transaction(operation)

    async with uow_factory() as unit:
        assert await unit.users.get_by_id(user_id) is not None


async def test_unit_of_work_transaction_helper_rolls_back_on_error(uow_factory) -> None:
    class Boom(RuntimeError):
        pass

    async def operation(unit) -> None:
        await unit.users.get_or_create(telegram_id=TELEGRAM_ID + 53)
        raise Boom

    with pytest.raises(Boom):
        await uow_factory.transaction(operation)

    async with uow_factory() as unit:
        assert await unit.users.get_by_telegram_id(TELEGRAM_ID + 53) is None


async def test_repository_access_outside_context_is_rejected(uow_factory) -> None:
    unit = uow_factory()

    with pytest.raises(RuntimeError):
        _ = unit.users
