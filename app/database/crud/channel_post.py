from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChannelPost


logger = structlog.get_logger(__name__)


async def create_channel_post(
    db: AsyncSession,
    *,
    channel_id: str,
    title: str | None,
    message_text: str | None,
    buttons_json: list | None,
    media_json: dict | None,
    idempotency_key: str,
    admin_id: int | None,
    message_thread_id: int | None = None,
    is_rich: bool = False,
) -> ChannelPost:
    """Insert a history row in status ``sending`` and commit (at-most-once anchor)."""
    post = ChannelPost(
        channel_id=channel_id,
        title=title,
        message_text=message_text,
        buttons_json=buttons_json,
        media_json=media_json,
        idempotency_key=idempotency_key,
        admin_id=admin_id,
        message_thread_id=message_thread_id,
        is_rich=is_rich,
        status='sending',
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


async def get_channel_post_by_idempotency_key(db: AsyncSession, idempotency_key: str) -> ChannelPost | None:
    result = await db.execute(select(ChannelPost).where(ChannelPost.idempotency_key == idempotency_key))
    return result.scalar_one_or_none()


async def get_recent_channel_posts(db: AsyncSession, limit: int = 50) -> list[ChannelPost]:
    result = await db.execute(
        select(ChannelPost).order_by(ChannelPost.created_at.desc(), ChannelPost.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def mark_channel_post_sent(db: AsyncSession, post: ChannelPost, telegram_message_id: int) -> ChannelPost:
    post.status = 'sent'
    post.telegram_message_id = telegram_message_id
    post.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(post)
    return post


async def mark_channel_post_failed(db: AsyncSession, post: ChannelPost, error_code: str) -> ChannelPost:
    post.status = 'failed'
    post.error_code = error_code
    post.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(post)
    return post
