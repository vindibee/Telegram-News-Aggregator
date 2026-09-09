"""initial saas schema

Схема SaaS-ядра: пользователи, защита триала, подписки с журналом операций,
платежи и новостные посты с полнотекстовым поиском и отпечатками для
дедупликации.

Отличие от результата ``--autogenerate``: ENUM-типы создаются и удаляются
явно. Alembic не отслеживает типы PostgreSQL, поэтому после ``downgrade``
они оставались бы в базе и следующий ``upgrade`` падал бы с
``DuplicateObjectError``.

Revision ID: cc8adb931da0
Revises:
Create Date: 2026-09-09 15:45:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "cc8adb931da0"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------- #
# ENUM-типы. create_type=False: созданием и удалением управляем вручную,
# иначе CREATE TYPE выполнялся бы повторно для каждой использующей таблицы.
# --------------------------------------------------------------------------- #
post_status = postgresql.ENUM(
    "new", "published", "rejected", "duplicate",
    name="post_status",
    create_type=False,
)
payment_provider = postgresql.ENUM(
    "telegram_stars", "crypto_bot",
    name="payment_provider",
    create_type=False,
)
payment_status = postgresql.ENUM(
    "pending", "processing", "succeeded", "failed", "refunded", "expired",
    name="payment_status",
    create_type=False,
)
subscription_plan = postgresql.ENUM(
    "free", "pro", "business",
    name="subscription_plan",
    create_type=False,
)
subscription_status = postgresql.ENUM(
    "trialing", "active", "past_due", "expired", "cancelled",
    name="subscription_status",
    create_type=False,
)
subscription_source = postgresql.ENUM(
    "trial", "payment", "referral", "promo", "manual",
    name="subscription_source",
    create_type=False,
)
subscription_event_kind = postgresql.ENUM(
    "created", "extended", "downgraded", "expired", "cancelled", "expiry_notified",
    name="subscription_event_kind",
    create_type=False,
)
trial_fingerprint_kind = postgresql.ENUM(
    "phone", "ip", "device",
    name="trial_fingerprint_kind",
    create_type=False,
)

_ENUM_TYPES: tuple[postgresql.ENUM, ...] = (
    post_status,
    payment_provider,
    payment_status,
    subscription_plan,
    subscription_status,
    subscription_source,
    subscription_event_kind,
    trial_fingerprint_kind,
)


def upgrade() -> None:
    """Применяет миграцию."""
    bind = op.get_bind()
    for enum_type in _ENUM_TYPES:
        enum_type.create(bind, checkfirst=True)

    # Таблица news_posts осталась от версии проекта до внедрения миграций,
    # где схема создавалась через Base.metadata.create_all. Её преемник —
    # posts, поэтому наследие удаляется здесь.
    op.execute("DROP TABLE IF EXISTS news_posts")

    op.create_table(
        "posts",
        sa.Column("channel_name", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("post_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "media_urls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("status", post_status, server_default="new", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duplicate_of_id", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column("simhash_band_0", sa.Integer(), nullable=True),
        sa.Column("simhash_band_1", sa.Integer(), nullable=True),
        sa.Column("simhash_band_2", sa.Integer(), nullable=True),
        sa.Column("simhash_band_3", sa.Integer(), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian', coalesce(content, ''))", persisted=True),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "duplicate_of_id IS NULL OR duplicate_of_id <> id",
            name=op.f("ck_posts_no_self_duplicate"),
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"],
            ["posts.id"],
            name=op.f("fk_posts_duplicate_of_id_posts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_posts")),
        sa.UniqueConstraint("channel_name", "message_id", name="uq_posts_channel_message"),
    )
    op.create_index(
        "ix_posts_channel_name_post_time",
        "posts",
        ["channel_name", sa.text("post_time DESC")],
        unique=False,
    )
    op.create_index("ix_posts_content_hash", "posts", ["content_hash"], unique=False)
    op.create_index("ix_posts_duplicate_of_id", "posts", ["duplicate_of_id"], unique=False)
    op.create_index(
        "ix_posts_search_vector", "posts", ["search_vector"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        "ix_posts_simhash_band_0",
        "posts",
        ["simhash_band_0"],
        unique=False,
        postgresql_where=sa.text("simhash_band_0 IS NOT NULL"),
    )
    op.create_index(
        "ix_posts_simhash_band_1",
        "posts",
        ["simhash_band_1"],
        unique=False,
        postgresql_where=sa.text("simhash_band_1 IS NOT NULL"),
    )
    op.create_index(
        "ix_posts_simhash_band_2",
        "posts",
        ["simhash_band_2"],
        unique=False,
        postgresql_where=sa.text("simhash_band_2 IS NOT NULL"),
    )
    op.create_index(
        "ix_posts_simhash_band_3",
        "posts",
        ["simhash_band_3"],
        unique=False,
        postgresql_where=sa.text("simhash_band_3 IS NOT NULL"),
    )
    op.create_index(
        "ix_posts_status_post_time",
        "posts",
        ["status", sa.text("post_time DESC")],
        unique=False,
    )

    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=True),
        sa.Column("first_name", sa.String(length=64), nullable=True),
        sa.Column("last_name", sa.String(length=64), nullable=True),
        sa.Column("language_code", sa.String(length=8), nullable=True),
        sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_banned", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_bot_blocked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("referral_code", sa.String(length=16), nullable=False),
        sa.Column("referred_by_id", sa.BigInteger(), nullable=True),
        sa.Column("trial_activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "referred_by_id IS NULL OR referred_by_id <> id",
            name=op.f("ck_users_self_referral"),
        ),
        sa.ForeignKeyConstraint(
            ["referred_by_id"],
            ["users.id"],
            name=op.f("fk_users_referred_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("referral_code", name="uq_users_referral_code"),
        sa.UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
    )
    op.create_index(
        "ix_users_broadcastable",
        "users",
        ["id"],
        unique=False,
        postgresql_where=sa.text("NOT is_bot_blocked AND NOT is_banned"),
    )
    op.create_index("ix_users_referred_by_id", "users", ["referred_by_id"], unique=False)

    op.create_table(
        "payments",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", payment_provider, nullable=False),
        sa.Column("status", payment_status, nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("plan", subscription_plan, nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status <> 'succeeded') OR (external_id IS NOT NULL AND paid_at IS NOT NULL)",
            name=op.f("ck_payments_succeeded_has_external_id"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount_positive")),
        sa.CheckConstraint("period_days > 0", name=op.f("ck_payments_period_days_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_payments_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        sa.UniqueConstraint("provider", "external_id", name="uq_payments_provider_external_id"),
        sa.UniqueConstraint("provider", "invoice_id", name="uq_payments_provider_invoice_id"),
    )
    op.create_index(
        "ix_payments_paid_at_succeeded",
        "payments",
        ["paid_at"],
        unique=False,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        "ix_payments_status_created_at", "payments", ["status", "created_at"], unique=False
    )
    op.create_index(
        "ix_payments_user_id_created_at", "payments", ["user_id", "created_at"], unique=False
    )

    op.create_table(
        "subscriptions",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan", subscription_plan, nullable=False),
        sa.Column("status", subscription_status, nullable=False),
        sa.Column("source", subscription_source, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("expiry_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at > started_at", name=op.f("ck_subscriptions_period_order")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_subscriptions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index(
        "ix_subscriptions_status_expires_at",
        "subscriptions",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_user_id_created_at",
        "subscriptions",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_subscriptions_one_live_per_user",
        "subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('trialing', 'active')"),
    )

    op.create_table(
        "trial_claims",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", trial_fingerprint_kind, nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_trial_claims_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trial_claims")),
        sa.UniqueConstraint("kind", "fingerprint", name="uq_trial_claims_kind_fingerprint"),
    )
    op.create_index("ix_trial_claims_user_id", "trial_claims", ["user_id"], unique=False)

    op.create_table(
        "subscription_events",
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", subscription_event_kind, nullable=False),
        sa.Column("days_granted", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "days_granted >= 0", name=op.f("ck_subscription_events_days_granted_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_subscription_events_payment_id_payments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_subscription_events_subscription_id_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_subscription_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_events")),
        sa.UniqueConstraint("payment_id", name="uq_subscription_events_payment_id"),
    )
    op.create_index(
        "ix_subscription_events_kind_created_at",
        "subscription_events",
        ["kind", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_events_subscription_id_created_at",
        "subscription_events",
        ["subscription_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_events_user_id_created_at",
        "subscription_events",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Откатывает миграцию."""
    op.drop_index(
        "ix_subscription_events_user_id_created_at", table_name="subscription_events"
    )
    op.drop_index(
        "ix_subscription_events_subscription_id_created_at", table_name="subscription_events"
    )
    op.drop_index("ix_subscription_events_kind_created_at", table_name="subscription_events")
    op.drop_table("subscription_events")

    op.drop_index("ix_trial_claims_user_id", table_name="trial_claims")
    op.drop_table("trial_claims")

    op.drop_index(
        "uq_subscriptions_one_live_per_user",
        table_name="subscriptions",
        postgresql_where=sa.text("status IN ('trialing', 'active')"),
    )
    op.drop_index("ix_subscriptions_user_id_created_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_status_expires_at", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_payments_user_id_created_at", table_name="payments")
    op.drop_index("ix_payments_status_created_at", table_name="payments")
    op.drop_index(
        "ix_payments_paid_at_succeeded",
        table_name="payments",
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.drop_table("payments")

    op.drop_index("ix_users_referred_by_id", table_name="users")
    op.drop_index(
        "ix_users_broadcastable",
        table_name="users",
        postgresql_where=sa.text("NOT is_bot_blocked AND NOT is_banned"),
    )
    op.drop_table("users")

    op.drop_index("ix_posts_status_post_time", table_name="posts")
    op.drop_index(
        "ix_posts_simhash_band_3",
        table_name="posts",
        postgresql_where=sa.text("simhash_band_3 IS NOT NULL"),
    )
    op.drop_index(
        "ix_posts_simhash_band_2",
        table_name="posts",
        postgresql_where=sa.text("simhash_band_2 IS NOT NULL"),
    )
    op.drop_index(
        "ix_posts_simhash_band_1",
        table_name="posts",
        postgresql_where=sa.text("simhash_band_1 IS NOT NULL"),
    )
    op.drop_index(
        "ix_posts_simhash_band_0",
        table_name="posts",
        postgresql_where=sa.text("simhash_band_0 IS NOT NULL"),
    )
    op.drop_index("ix_posts_search_vector", table_name="posts", postgresql_using="gin")
    op.drop_index("ix_posts_duplicate_of_id", table_name="posts")
    op.drop_index("ix_posts_content_hash", table_name="posts")
    op.drop_index("ix_posts_channel_name_post_time", table_name="posts")
    op.drop_table("posts")

    # Типы удаляются последними: пока существует колонка соответствующего
    # типа, DROP TYPE завершится ошибкой зависимости.
    bind = op.get_bind()
    for enum_type in reversed(_ENUM_TYPES):
        enum_type.drop(bind, checkfirst=True)
