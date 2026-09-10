"""keywords, scheduled posts and post source link

Revision ID: e954b677d8e2
Revises: 7a2654509e93
Create Date: 2026-09-10 09:56:23.160813+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'e954b677d8e2'
down_revision: str | None = '7a2654509e93'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Типы создаются и удаляются явно: автогенерация делает это внутри
# CREATE TABLE, из-за чего downgrade оставляет типы в базе и повторный
# прогон «вниз-вверх» падает на попытке создать их заново.
keyword_kind = postgresql.ENUM("trigger", "stop", name="keyword_kind", create_type=False)
scheduled_post_status = postgresql.ENUM(
    "pending", "published", "failed", "cancelled",
    name="scheduled_post_status",
    create_type=False,
)

NEW_ENUMS = (keyword_kind, scheduled_post_status)


def upgrade() -> None:
    """Применяет миграцию."""
    bind = op.get_bind()
    for enum_type in NEW_ENUMS:
        enum_type.create(bind, checkfirst=True)

    op.create_table('user_keywords',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', keyword_kind, nullable=False),
    sa.Column('word', sa.String(length=64), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.CheckConstraint('length(word) > 0', name=op.f('ck_user_keywords_word_not_empty')),
    sa.CheckConstraint('word = lower(word)', name=op.f('ck_user_keywords_word_normalized')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_keywords_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_keywords')),
    sa.UniqueConstraint('user_id', 'kind', 'word', name='uq_user_keywords_word')
    )
    op.create_index('ix_user_keywords_active', 'user_keywords', ['user_id', 'kind'], unique=False, postgresql_where=sa.text('is_active'))
    op.create_table('scheduled_posts',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('target_channel_id', sa.BigInteger(), nullable=False),
    sa.Column('post_id', sa.BigInteger(), nullable=False),
    sa.Column('publish_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', scheduled_post_status, server_default='pending', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('caption', sa.String(length=1024), nullable=True),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status <> 'published' OR (published_at IS NOT NULL AND message_id IS NOT NULL)", name=op.f('ck_scheduled_posts_published_has_message')),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_scheduled_posts_attempts_non_negative')),
    sa.ForeignKeyConstraint(['post_id'], ['posts.id'], name=op.f('fk_scheduled_posts_post_id_posts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['target_channel_id'], ['user_channels.id'], name=op.f('fk_scheduled_posts_target_channel_id_user_channels'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_scheduled_posts_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scheduled_posts')),
    sa.UniqueConstraint('target_channel_id', 'post_id', name='uq_scheduled_posts_channel_post')
    )
    op.create_index('ix_scheduled_posts_post_id', 'scheduled_posts', ['post_id'], unique=False)
    op.create_index('ix_scheduled_posts_queue', 'scheduled_posts', ['publish_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('ix_scheduled_posts_target_channel_id', 'scheduled_posts', ['target_channel_id'], unique=False)
    op.create_index('ix_scheduled_posts_user_id_publish_at', 'scheduled_posts', ['user_id', 'publish_at'], unique=False)
    op.add_column('posts', sa.Column('source_channel_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_posts_source_channel_id', 'posts', ['source_channel_id'], unique=False, postgresql_where=sa.text('source_channel_id IS NOT NULL'))
    op.alter_column('user_channels', 'is_verified', new_column_name='bot_is_admin')
    op.create_foreign_key(op.f('fk_posts_source_channel_id_user_channels'), 'posts', 'user_channels', ['source_channel_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Откатывает миграцию."""
    op.alter_column('user_channels', 'bot_is_admin', new_column_name='is_verified')
    op.drop_constraint(op.f('fk_posts_source_channel_id_user_channels'), 'posts', type_='foreignkey')
    op.drop_index('ix_posts_source_channel_id', table_name='posts', postgresql_where=sa.text('source_channel_id IS NOT NULL'))
    op.drop_column('posts', 'source_channel_id')
    op.drop_index('ix_scheduled_posts_user_id_publish_at', table_name='scheduled_posts')
    op.drop_index('ix_scheduled_posts_target_channel_id', table_name='scheduled_posts')
    op.drop_index('ix_scheduled_posts_queue', table_name='scheduled_posts', postgresql_where=sa.text("status = 'pending'"))
    op.drop_index('ix_scheduled_posts_post_id', table_name='scheduled_posts')
    op.drop_table('scheduled_posts')
    op.drop_index('ix_user_keywords_active', table_name='user_keywords', postgresql_where=sa.text('is_active'))
    op.drop_table('user_keywords')

    bind = op.get_bind()
    for enum_type in reversed(NEW_ENUMS):
        enum_type.drop(bind, checkfirst=True)
