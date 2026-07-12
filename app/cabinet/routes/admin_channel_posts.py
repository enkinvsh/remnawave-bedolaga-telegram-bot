"""Admin API for channel-post publishing (dedicated, additive path)."""

import mimetypes
import re
from datetime import datetime

import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.channel_post import (
    get_channel_post_by_idempotency_key,
    get_recent_channel_posts,
)
from app.database.crud.required_channel import (
    get_post_target_by_channel_id,
    get_post_targets,
    revoke_post_target,
    upsert_post_target,
)
from app.database.models import User
from app.services.broadcast_service import broadcast_service
from app.services.channel_post_render import (
    ERROR_RICH_WITH_MEDIA,
    ChannelPostRenderError,
    build_url_keyboard,
    canonicalize_destination,
    validate_post_content,
    validate_rich_content,
)
from app.services.channel_post_service import (
    ERROR_BOT_UNAVAILABLE,
    ERROR_NOT_POSTABLE,
    ERROR_TIMEOUT,
    ChannelPostSendError,
    fetch_chat,
    resolve_capability,
    send_post,
)
from app.services.permission_service import PermissionService
from app.utils.cache import RateLimitCache, cache, cache_key

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.channel_posts import ChannelPostRequest


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/channel-posts', tags=['Cabinet Admin Channel Posts'])

_RATE_LIMIT_ACTION = 'channel_post'
_HISTORY_LIMIT = 50

ERROR_EMOJI_NOT_FOUND = 'emoji_not_found'
ERROR_PACK_NOT_FOUND = 'pack_not_found'
ERROR_BAD_EMOJI_ID = 'bad_emoji_id'
ERROR_BAD_PACK_NAME = 'bad_pack_name'

_EMOJI_ID_RE = re.compile(r'^\d{1,32}$')
_PACK_NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,64}$')
_EMOJI_META_TTL = 86400
_PACK_TTL = 3600
_EMOJI_CACHE_HEADERS = {
    'Cache-Control': 'public, max-age=86400',
    'X-Content-Type-Options': 'nosniff',
}


class ChannelPostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: str
    status: str
    telegram_message_id: int | None
    message_thread_id: int | None
    is_rich: bool
    error_code: str | None
    created_at: datetime | None


class AllowlistEntry(BaseModel):
    channel_id: str
    title: str | None
    type: str | None


class EmojiPackEntry(BaseModel):
    custom_emoji_id: str
    emoji: str | None


class EmojiPackResponse(BaseModel):
    name: str
    title: str
    emojis: list[EmojiPackEntry]


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    destination_id: str


class PreviewResponse(BaseModel):
    chat_id: str
    title: str | None
    type: str | None
    username: str | None
    is_allowlisted: bool
    bot_status: str | None
    can_post: bool


class AllowlistAddRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    input: str


def _render_error(exc: ChannelPostRenderError) -> HTTPException:
    return HTTPException(status_code=422, detail={'code': exc.code, **exc.extra})


def _canonicalize(destination_id: str) -> tuple[int, str]:
    try:
        return canonicalize_destination(destination_id)
    except ChannelPostRenderError as exc:
        raise _render_error(exc) from exc


@router.get('', response_model=list[ChannelPostResponse])
async def list_channel_posts(
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channel_posts:read')),
) -> list[ChannelPostResponse]:
    rows = await get_recent_channel_posts(db, limit=_HISTORY_LIMIT)
    return [ChannelPostResponse.model_validate(row) for row in rows]


@router.get('/allowlist', response_model=list[AllowlistEntry])
async def list_allowlist(
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channel_posts:read')),
) -> list[AllowlistEntry]:
    targets = await get_post_targets(db)
    return [AllowlistEntry(channel_id=t.channel_id, title=t.title, type=None) for t in targets]


@router.post('/preview', response_model=PreviewResponse)
async def preview_channel_post(
    data: PreviewRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channel_posts:send')),
) -> PreviewResponse:
    int_value, canonical = _canonicalize(data.destination_id)
    target = await get_post_target_by_channel_id(db, canonical)
    if target is None:
        raise HTTPException(status_code=403, detail='destination is not allowlisted')
    cap = await _resolve(int_value, is_allowlisted=True)
    return PreviewResponse(**cap)


@router.post('', response_model=ChannelPostResponse)
async def create_channel_post_endpoint(
    data: ChannelPostRequest,
    idempotency_key: str = Header(..., alias='Idempotency-Key'),
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('channel_posts:send')),
) -> ChannelPostResponse:
    int_value, canonical = _canonicalize(data.destination_id)

    target = await get_post_target_by_channel_id(db, canonical)
    if target is None:
        raise HTTPException(status_code=403, detail='destination is not allowlisted')

    if await RateLimitCache.is_rate_limited(
        admin.id,
        _RATE_LIMIT_ACTION,
        settings.CHANNEL_POST_RATE_LIMIT_COUNT,
        settings.CHANNEL_POST_RATE_LIMIT_WINDOW_SECONDS,
        fail_closed=True,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': str(settings.CHANNEL_POST_RATE_LIMIT_WINDOW_SECONDS)},
        )

    if data.rich and data.media is not None:
        raise HTTPException(status_code=422, detail={'code': ERROR_RICH_WITH_MEDIA})

    existing = await get_channel_post_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return ChannelPostResponse.model_validate(existing)

    try:
        if data.rich:
            validate_rich_content(data.message_text)
        else:
            validate_post_content(data.message_text, data.media)
        keyboard = build_url_keyboard(data.custom_buttons)
    except ChannelPostRenderError as exc:
        raise _render_error(exc) from exc

    cap = await _resolve(int_value, is_allowlisted=True)
    if not cap['can_post']:
        raise HTTPException(status_code=409, detail={'code': ERROR_NOT_POSTABLE})

    row = await _send(
        db,
        int_value=int_value,
        canonical=canonical,
        title=cap.get('title'),
        data=data,
        keyboard=keyboard,
        idempotency_key=idempotency_key,
        admin_id=admin.id,
    )

    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action='channel_post',
        resource_type='channel_post',
        resource_id=canonical,
        details={'status': row.status, 'has_media': data.media is not None, 'is_rich': data.rich},
        status='success' if row.status == 'sent' else 'failure',
    )
    await db.commit()

    return ChannelPostResponse.model_validate(row)


@router.post('/allowlist', response_model=AllowlistEntry, status_code=201)
async def add_to_allowlist(
    data: AllowlistAddRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('channel_posts:allowlist')),
) -> AllowlistEntry:
    try:
        chat = await fetch_chat(data.input)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        raise HTTPException(status_code=404, detail={'code': 'chat_not_found'}) from exc

    canonical = str(chat.id)
    cap = await _resolve(chat.id, is_allowlisted=True, chat=chat)
    if not cap['can_post']:
        raise HTTPException(status_code=409, detail={'code': ERROR_NOT_POSTABLE})

    await upsert_post_target(db, channel_id=canonical, title=cap.get('title'))
    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action='channel_post_allowlist_add',
        resource_type='channel_post',
        resource_id=canonical,
        details={'type': cap.get('type')},
        status='success',
    )
    await db.commit()

    return AllowlistEntry(channel_id=canonical, title=cap.get('title'), type=cap.get('type'))


@router.delete('/allowlist/{channel_id}', status_code=204)
async def revoke_from_allowlist(
    channel_id: str,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('channel_posts:allowlist')),
) -> None:
    revoked = await revoke_post_target(db, channel_id)
    if not revoked:
        raise HTTPException(status_code=404, detail='post target not found')
    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action='channel_post_allowlist_revoke',
        resource_type='channel_post',
        resource_id=channel_id,
        status='success',
    )
    await db.commit()


async def _resolve(int_value: int, *, is_allowlisted: bool, chat=None) -> dict:
    try:
        return await resolve_capability(int_value, is_allowlisted=is_allowlisted, chat=chat)
    except ChannelPostSendError as exc:
        raise HTTPException(status_code=503, detail={'code': exc.code}) from exc


async def _send(db, *, int_value, canonical, title, data, keyboard, idempotency_key, admin_id):
    try:
        return await send_post(
            db,
            chat_id_int=int_value,
            canonical_channel_id=canonical,
            title=title,
            message_text=data.message_text,
            buttons=data.custom_buttons,
            media=data.media,
            keyboard=keyboard,
            disable_web_page_preview=data.disable_web_page_preview,
            idempotency_key=idempotency_key,
            admin_id=admin_id,
            message_thread_id=data.message_thread_id,
            is_rich=data.rich,
        )
    except ChannelPostSendError as exc:
        code = status.HTTP_504_GATEWAY_TIMEOUT if exc.code == ERROR_TIMEOUT else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=code, detail={'code': exc.code}) from exc


def _require_emoji_bot():
    bot = broadcast_service.bot
    if bot is None:
        raise HTTPException(status_code=503, detail={'code': ERROR_BOT_UNAVAILABLE})
    return bot


def _pick_emoji_file_id(sticker) -> str | None:
    if not getattr(sticker, 'is_animated', False) and not getattr(sticker, 'is_video', False):
        return sticker.file_id
    thumbnail = getattr(sticker, 'thumbnail', None)
    return thumbnail.file_id if thumbnail is not None else None


@router.get('/emoji/{custom_emoji_id}')
async def get_emoji_image(
    custom_emoji_id: str,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channel_posts:read')),
) -> Response:
    if not _EMOJI_ID_RE.match(custom_emoji_id):
        raise HTTPException(status_code=422, detail={'code': ERROR_BAD_EMOJI_ID})

    bot = _require_emoji_bot()
    meta_key = cache_key('telegram', 'custom_emoji', custom_emoji_id)
    meta = await cache.get(meta_key)
    if not meta:
        try:
            stickers = await bot.get_custom_emoji_stickers([custom_emoji_id])
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            raise HTTPException(status_code=404, detail={'code': ERROR_EMOJI_NOT_FOUND}) from exc
        if not stickers:
            raise HTTPException(status_code=404, detail={'code': ERROR_EMOJI_NOT_FOUND})
        file_id = _pick_emoji_file_id(stickers[0])
        if file_id is None:
            raise HTTPException(status_code=404, detail={'code': ERROR_EMOJI_NOT_FOUND})
        meta = {'file_id': file_id}
        await cache.set(meta_key, meta, _EMOJI_META_TTL)

    try:
        file = await bot.get_file(meta['file_id'])
        buffer = await bot.download_file(file.file_path)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        raise HTTPException(status_code=404, detail={'code': ERROR_EMOJI_NOT_FOUND}) from exc

    if hasattr(buffer, 'seek'):
        buffer.seek(0)
    content = buffer.read() if hasattr(buffer, 'read') else bytes(buffer)
    media_type = mimetypes.guess_type(file.file_path)[0] or 'application/octet-stream'
    return Response(content=content, media_type=media_type, headers=dict(_EMOJI_CACHE_HEADERS))


@router.get('/emoji-pack/{set_name}', response_model=EmojiPackResponse)
async def get_emoji_pack(
    set_name: str,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channel_posts:read')),
) -> EmojiPackResponse:
    if not _PACK_NAME_RE.match(set_name):
        raise HTTPException(status_code=422, detail={'code': ERROR_BAD_PACK_NAME})

    bot = _require_emoji_bot()
    pack_key = cache_key('telegram', 'emoji_pack', set_name)
    cached = await cache.get(pack_key)
    if cached:
        return EmojiPackResponse.model_validate(cached)

    try:
        sticker_set = await bot.get_sticker_set(set_name)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        raise HTTPException(status_code=404, detail={'code': ERROR_PACK_NOT_FOUND}) from exc

    emojis = [
        EmojiPackEntry(custom_emoji_id=st.custom_emoji_id, emoji=getattr(st, 'emoji', None))
        for st in sticker_set.stickers
        if getattr(st, 'custom_emoji_id', None)
    ]
    response = EmojiPackResponse(name=sticker_set.name, title=sticker_set.title, emojis=emojis)
    await cache.set(pack_key, response.model_dump(), _PACK_TTL)
    return response
