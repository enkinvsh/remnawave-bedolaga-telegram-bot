"""Admin routes for overriding bundled localization strings.

Mirrors the email-template editor: list → edit one → reset to default. An
override lives in ``locale_overrides`` and is served to the bot from an
in-memory cache, so every mutating endpoint refreshes that cache and the change
takes effect immediately, without a restart.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.locale_override import (
    delete_locale_override,
    get_locale_override,
    list_locale_overrides,
    upsert_locale_override,
)
from app.database.models import User
from app.localization.loader import DEFAULT_LANGUAGE, load_locale
from app.localization.overrides import load_overrides

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/locales', tags=['Admin Locales'])


AVAILABLE_LANGUAGES = ['ru', 'en', 'ua', 'fa', 'zh']

MAX_VALUE_LENGTH = 4096
MAX_PAGE_SIZE = 500


# ============ Bundled locale helpers ============


def _bundled(language: str) -> dict[str, Any]:
    return load_locale(language)


def _default_value(key: str, language: str) -> str | None:
    """Bundled string for ``key``, falling back the way ``Texts`` does."""
    value = _bundled(language).get(key)
    if value is None and language != DEFAULT_LANGUAGE:
        value = _bundled(DEFAULT_LANGUAGE).get(key)
    return None if value is None else str(value)


def _known_keys() -> set[str]:
    """Every key present in any bundled locale — inventing new keys is refused."""
    keys: set[str] = set()
    for language in AVAILABLE_LANGUAGES:
        keys.update(_bundled(language).keys())
    return keys


def _validate_language(language: str) -> str:
    if language not in AVAILABLE_LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid language: {language}. Available: {AVAILABLE_LANGUAGES}',
        )
    return language


def _validate_key(key: str) -> str:
    if key not in _known_keys():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Unknown localization key: {key}',
        )
    return key


async def _override_map(db: AsyncSession) -> dict[tuple[str, str], str]:
    rows = await list_locale_overrides(db)
    return {(row.language, row.key): row.value for row in rows}


# ============ Schemas ============


class LocaleOverrideUpdate(BaseModel):
    """Request to override a single localization string."""

    value: str = Field(..., max_length=MAX_VALUE_LENGTH)


class LocaleImportRequest(BaseModel):
    """``{language: {key: value}}`` — the shape produced by ``GET /export``."""

    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)


# ============ Endpoints ============


@router.get('', summary='List localization strings')
async def list_locale_strings(
    search: str | None = Query(default=None),
    language: str = Query(default=DEFAULT_LANGUAGE),
    only_overridden: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Paginated list of keys for one language, with their current values.

    ``search`` matches the key **or** the string itself (default or override),
    case-insensitively — nobody remembers ADMIN_SUBSCRIPTION_EXTEND_CONFIRM,
    they remember the wording they saw.
    """
    _validate_language(language)

    overrides = await _override_map(db)
    needle = (search or '').strip().lower()

    items: list[dict[str, Any]] = []
    for key in sorted(_known_keys()):
        override_value = overrides.get((language, key))
        if only_overridden and override_value is None:
            continue

        default_value = _default_value(key, language)
        current = override_value if override_value is not None else default_value

        if needle:
            haystack = f'{key}\n{default_value or ""}\n{override_value or ""}'.lower()
            if needle not in haystack:
                continue

        items.append(
            {
                'key': key,
                'language': language,
                'default_value': default_value,
                'override_value': override_value,
                'value': current,
                'is_overridden': override_value is not None,
            }
        )

    total = len(items)
    return {
        'items': items[offset : offset + limit],
        'total': total,
        'limit': limit,
        'offset': offset,
        'available_languages': AVAILABLE_LANGUAGES,
    }


# NOTE: `/export` MUST stay declared before `/{key}`, otherwise FastAPI routes
# GET /export into the key handler and answers 404 for an unknown key.
@router.get('/export', summary='Export all overrides')
async def export_locale_overrides(
    _admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """``{language: {key: value}}`` — feed it back to ``POST /import`` to seed a white-label instance."""
    rows = await list_locale_overrides(db)

    payload: dict[str, dict[str, str]] = {}
    for row in rows:
        payload.setdefault(row.language, {})[row.key] = row.value

    return {'overrides': payload, 'total': len(rows)}


@router.get('/{key}', summary='Get one key across all languages')
async def get_locale_string(
    key: str,
    _admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    _validate_key(key)

    overrides = await _override_map(db)

    languages: dict[str, Any] = {}
    for language in AVAILABLE_LANGUAGES:
        override_value = overrides.get((language, key))
        default_value = _default_value(key, language)
        languages[language] = {
            'default_value': default_value,
            'override_value': override_value,
            'value': override_value if override_value is not None else default_value,
            'is_overridden': override_value is not None,
        }

    return {'key': key, 'languages': languages}


@router.put('/{key}/{language}', summary='Save an override')
async def update_locale_string(
    key: str,
    language: str,
    data: LocaleOverrideUpdate,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    _validate_key(key)
    _validate_language(language)

    await upsert_locale_override(db, key=key, language=language, value=data.value)
    await db.commit()
    await load_overrides(db)

    logger.info(
        'Админ переопределил строку локализации',
        admin_id=getattr(admin, 'id', None),
        key=key,
        language=language,
    )

    return {'status': 'ok', 'key': key, 'language': language, 'value': data.value}


@router.delete('/{key}/{language}', summary='Reset a string to its bundled default')
async def delete_locale_string(
    key: str,
    language: str,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    _validate_key(key)
    _validate_language(language)

    deleted = await delete_locale_override(db, key=key, language=language)
    await db.commit()
    await load_overrides(db)

    if deleted:
        logger.info(
            'Админ сбросил строку локализации к дефолту',
            admin_id=getattr(admin, 'id', None),
            key=key,
            language=language,
        )

    return {'status': 'ok', 'key': key, 'language': language, 'was_overridden': deleted}


@router.post('/reload', summary='Reload the override cache from DB')
async def reload_locale_overrides(
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    loaded = await load_overrides(db)
    logger.info('Кеш override-ов локализации перезагружен', admin_id=getattr(admin, 'id', None), loaded=loaded)
    return {'status': 'ok', 'loaded': loaded}


@router.post('/import', summary='Import overrides')
async def import_locale_overrides(
    data: LocaleImportRequest,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Upsert a ``{language: {key: value}}`` payload. Idempotent.

    Unknown keys and unsupported languages are skipped and counted rather than
    rejecting the whole batch — a white-label seed file may legitimately carry
    leftovers from another build.
    """
    known_keys = _known_keys()

    created = 0
    updated = 0
    skipped_unknown_key = 0
    skipped_unknown_language = 0

    for language, entries in data.overrides.items():
        if language not in AVAILABLE_LANGUAGES:
            skipped_unknown_language += len(entries)
            continue

        for key, value in entries.items():
            if key not in known_keys:
                skipped_unknown_key += 1
                continue
            if len(value) > MAX_VALUE_LENGTH:
                skipped_unknown_key += 1
                continue

            existing = await get_locale_override(db, key=key, language=language)
            await upsert_locale_override(db, key=key, language=language, value=value)
            if existing is None:
                created += 1
            else:
                updated += 1

    await db.commit()
    await load_overrides(db)

    logger.info(
        'Импорт override-ов локализации',
        admin_id=getattr(admin, 'id', None),
        created=created,
        updated=updated,
        skipped_unknown_key=skipped_unknown_key,
        skipped_unknown_language=skipped_unknown_language,
    )

    return {
        'status': 'ok',
        'created': created,
        'updated': updated,
        'skipped_unknown_key': skipped_unknown_key,
        'skipped_unknown_language': skipped_unknown_language,
    }
