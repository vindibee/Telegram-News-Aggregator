"""channels, referrals, promocodes and click tracking

Revision ID: 7a2654509e93
Revises: cc8adb931da0
Create Date: 2026-09-10 09:29:56.706570+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = '7a2654509e93'
down_revision: str | None = 'cc8adb931da0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Типы объявляются с ``create_type=False``, а создаются и удаляются явно.
# Автогенерация этого не умеет: она пытается создать тип внутри каждого
# CREATE TABLE, и повторное использование уже существующего
# ``subscription_plan`` роняло бы миграцию с «type already exists».
channel_kind = postgresql.ENUM("source", "target", name="channel_kind", create_type=False)
promocode_kind = postgresql.ENUM(
    "bonus_days", "discount_percent", name="promocode_kind", create_type=False
)
referral_status = postgresql.ENUM(
    "pending", "qualified", "rewarded", "rejected", name="referral_status", create_type=False
)
user_language = postgresql.ENUM("ru", "en", name="user_language", create_type=False)

#: Тип уже создан начальной миграцией — здесь он только переиспользуется.
subscription_plan = postgresql.ENUM(
    "free", "pro", "business", name="subscription_plan", create_type=False
)

#: Типы, которые вводит именно эта миграция: их и создаём, и удаляем.
NEW_ENUMS = (channel_kind, promocode_kind, referral_status, user_language)


def upgrade() -> None:
    """Применяет миграцию."""
    bind = op.get_bind()
    for enum_type in NEW_ENUMS:
        enum_type.create(bind, checkfirst=True)

    op.create_table('promocodes',
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('kind', promocode_kind, nullable=False),
    sa.Column('value', sa.Integer(), nullable=False),
    sa.Column('plan', subscription_plan, nullable=True),
    sa.Column('max_activations', sa.Integer(), nullable=True),
    sa.Column('activations', sa.Integer(), server_default='0', nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('comment', sa.String(length=255), nullable=True),
    sa.Column('created_by_id', sa.BigInteger(), nullable=True),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind <> 'discount_percent' OR value <= 100", name=op.f('ck_promocodes_discount_within_100')),
    sa.CheckConstraint('activations >= 0', name=op.f('ck_promocodes_activations_non_negative')),
    sa.CheckConstraint('max_activations IS NULL OR activations <= max_activations', name=op.f('ck_promocodes_activations_within_limit')),
    sa.CheckConstraint('max_activations IS NULL OR max_activations > 0', name=op.f('ck_promocodes_max_activations_positive')),
    sa.CheckConstraint('valid_from IS NULL OR valid_until IS NULL OR valid_until > valid_from', name=op.f('ck_promocodes_validity_period_order')),
    sa.CheckConstraint('value > 0', name=op.f('ck_promocodes_value_positive')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_promocodes_created_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promocodes')),
    sa.UniqueConstraint('code', name='uq_promocodes_code')
    )
    op.create_index('ix_promocodes_active', 'promocodes', ['code'], unique=False, postgresql_where=sa.text('is_active'))
    op.create_table('tracked_links',
    sa.Column('token', sa.String(length=32), nullable=False),
    sa.Column('target_url', sa.Text(), nullable=False),
    sa.Column('post_id', sa.BigInteger(), nullable=True),
    sa.Column('owner_id', sa.BigInteger(), nullable=True),
    sa.Column('clicks', sa.Integer(), server_default='0', nullable=False),
    sa.Column('unique_clicks', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('clicks >= 0', name=op.f('ck_tracked_links_clicks_non_negative')),
    sa.CheckConstraint('length(target_url) > 0', name=op.f('ck_tracked_links_target_url_present')),
    sa.CheckConstraint('unique_clicks <= clicks', name=op.f('ck_tracked_links_unique_clicks_within_total')),
    sa.CheckConstraint('unique_clicks >= 0', name=op.f('ck_tracked_links_unique_clicks_non_negative')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_tracked_links_owner_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['post_id'], ['posts.id'], name=op.f('fk_tracked_links_post_id_posts'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tracked_links')),
    sa.UniqueConstraint('token', name='uq_tracked_links_token')
    )
    op.create_index('ix_tracked_links_active_token', 'tracked_links', ['token'], unique=False, postgresql_where=sa.text('is_active'))
    op.create_index('ix_tracked_links_owner_id_created_at', 'tracked_links', ['owner_id', 'created_at'], unique=False)
    op.create_index('ix_tracked_links_post_id', 'tracked_links', ['post_id'], unique=False)
    op.create_table('user_channels',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', channel_kind, nullable=False),
    sa.Column('username', sa.String(length=32), nullable=True),
    sa.Column('chat_id', sa.BigInteger(), nullable=True),
    sa.Column('title', sa.String(length=128), server_default='', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('is_verified', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind <> 'target' OR chat_id IS NOT NULL", name=op.f('ck_user_channels_target_requires_chat_id')),
    sa.CheckConstraint('username IS NOT NULL OR chat_id IS NOT NULL', name=op.f('ck_user_channels_identifier_present')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_channels_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_channels')),
    sa.UniqueConstraint('user_id', 'kind', 'chat_id', name='uq_user_channels_chat_id'),
    sa.UniqueConstraint('user_id', 'kind', 'username', name='uq_user_channels_username')
    )
    op.create_index('ix_user_channels_active', 'user_channels', ['user_id', 'kind'], unique=False, postgresql_where=sa.text('is_active'))
    op.create_index('ix_user_channels_username', 'user_channels', ['username'], unique=False)
    op.create_table('click_logs',
    sa.Column('link_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=True),
    sa.Column('clicked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('visitor_hash', sa.String(length=64), nullable=True),
    sa.Column('is_unique', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('referer', sa.String(length=255), nullable=True),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.ForeignKeyConstraint(['link_id'], ['tracked_links.id'], name=op.f('fk_click_logs_link_id_tracked_links'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_click_logs_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_click_logs')),
    sa.UniqueConstraint('link_id', 'visitor_hash', name='uq_click_logs_link_visitor')
    )
    op.create_index('ix_click_logs_clicked_at', 'click_logs', ['clicked_at'], unique=False)
    op.create_index('ix_click_logs_link_id_clicked_at', 'click_logs', ['link_id', 'clicked_at'], unique=False)
    op.create_index('ix_click_logs_user_id', 'click_logs', ['user_id'], unique=False, postgresql_where=sa.text('user_id IS NOT NULL'))
    op.create_table('promocode_redemptions',
    sa.Column('promocode_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('payment_id', sa.BigInteger(), nullable=True),
    sa.Column('days_granted', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.CheckConstraint('days_granted >= 0', name=op.f('ck_promocode_redemptions_days_granted_non_negative')),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_promocode_redemptions_payment_id_payments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['promocode_id'], ['promocodes.id'], name=op.f('fk_promocode_redemptions_promocode_id_promocodes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_promocode_redemptions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promocode_redemptions')),
    sa.UniqueConstraint('promocode_id', 'user_id', name='uq_promocode_redemptions_user')
    )
    op.create_index('ix_promocode_redemptions_created_at', 'promocode_redemptions', ['created_at'], unique=False)
    op.create_index('ix_promocode_redemptions_user_id', 'promocode_redemptions', ['user_id'], unique=False)
    op.create_table('referrals',
    sa.Column('referrer_id', sa.BigInteger(), nullable=False),
    sa.Column('referred_id', sa.BigInteger(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('status', referral_status, server_default='pending', nullable=False),
    sa.Column('bonus_days', sa.Integer(), server_default='0', nullable=False),
    sa.Column('payment_id', sa.BigInteger(), nullable=True),
    sa.Column('qualified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rewarded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.CheckConstraint("status <> 'rewarded' OR (rewarded_at IS NOT NULL AND bonus_days > 0)", name=op.f('ck_referrals_rewarded_has_bonus')),
    sa.CheckConstraint('bonus_days >= 0', name=op.f('ck_referrals_bonus_days_non_negative')),
    sa.CheckConstraint('referrer_id <> referred_id', name=op.f('ck_referrals_no_self_referral')),
    sa.ForeignKeyConstraint(['payment_id'], ['payments.id'], name=op.f('fk_referrals_payment_id_payments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['referred_id'], ['users.id'], name=op.f('fk_referrals_referred_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['referrer_id'], ['users.id'], name=op.f('fk_referrals_referrer_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_referrals')),
    sa.UniqueConstraint('referred_id', name='uq_referrals_referred_id')
    )
    op.create_index('ix_referrals_created_at', 'referrals', ['created_at'], unique=False)
    op.create_index('ix_referrals_referrer_id_status', 'referrals', ['referrer_id', 'status'], unique=False)
    op.add_column('users', sa.Column('language', user_language, server_default='ru', nullable=False))


def downgrade() -> None:
    """Откатывает миграцию."""
    op.drop_column('users', 'language')
    op.drop_index('ix_referrals_referrer_id_status', table_name='referrals')
    op.drop_index('ix_referrals_created_at', table_name='referrals')
    op.drop_table('referrals')
    op.drop_index('ix_promocode_redemptions_user_id', table_name='promocode_redemptions')
    op.drop_index('ix_promocode_redemptions_created_at', table_name='promocode_redemptions')
    op.drop_table('promocode_redemptions')
    op.drop_index('ix_click_logs_user_id', table_name='click_logs', postgresql_where=sa.text('user_id IS NOT NULL'))
    op.drop_index('ix_click_logs_link_id_clicked_at', table_name='click_logs')
    op.drop_index('ix_click_logs_clicked_at', table_name='click_logs')
    op.drop_table('click_logs')
    op.drop_index('ix_user_channels_username', table_name='user_channels')
    op.drop_index('ix_user_channels_active', table_name='user_channels', postgresql_where=sa.text('is_active'))
    op.drop_table('user_channels')
    op.drop_index('ix_tracked_links_post_id', table_name='tracked_links')
    op.drop_index('ix_tracked_links_owner_id_created_at', table_name='tracked_links')
    op.drop_index('ix_tracked_links_active_token', table_name='tracked_links', postgresql_where=sa.text('is_active'))
    op.drop_table('tracked_links')
    op.drop_index('ix_promocodes_active', table_name='promocodes', postgresql_where=sa.text('is_active'))
    op.drop_table('promocodes')

    # Типы удаляются после таблиц, которые на них ссылались: иначе
    # PostgreSQL откажется удалять используемый тип, и повторный прогон
    # "downgrade -> upgrade" падал бы на попытке создать его заново.
    bind = op.get_bind()
    for enum_type in reversed(NEW_ENUMS):
        enum_type.drop(bind, checkfirst=True)
