"""In-process cache of admin-defined localization string overrides.

``Texts._get_value`` calls :func:`get_override` for **every** localized string of
**every** message, so the read path here is a plain synchronous dict lookup: no
DB access, no ``await``, no I/O. The cache is filled once at startup
(:func:`load_overrides`) and refreshed by the cabinet editor after each edit.

Kept free of FastAPI/aiogram imports so both the bot and the web layer can use it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog


_logger = structlog.get_logger(__name__)

# (language, key) -> value. Replaced wholesale, never mutated in place, so a
# concurrent reader always sees a fully-built map.
_overrides: dict[tuple[str, str], str] = {}


def get_override(language: str, key: str) -> str | None:
    """Return the admin override for ``key`` in ``language``, if any."""
    return _overrides.get((language, key))


def get_override_cache() -> dict[tuple[str, str], str]:
    """Snapshot of the current cache (a copy — mutating it changes nothing)."""
    return dict(_overrides)


def set_override_cache(mapping: Mapping[tuple[str, str], Any]) -> None:
    """Replace the whole cache with ``mapping`` in a single assignment."""
    global _overrides
    _overrides = {(str(language), str(key)): str(value) for (language, key), value in mapping.items()}


def clear_override_cache() -> None:
    global _overrides
    _overrides = {}


async def load_overrides(db: Any = None) -> int:
    """Refresh the cache from the DB. Never raises.

    Opens its own session when ``db`` is omitted. On any failure the previous
    cache is left untouched. Returns the number of entries in the cache after
    the attempt.
    """
    global _overrides

    try:
        from app.database.crud import locale_override as locale_override_crud

        if db is not None:
            rows = await locale_override_crud.list_locale_overrides(db)
        else:
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                rows = await locale_override_crud.list_locale_overrides(session)
    except Exception as error:
        _logger.warning('Failed to load locale overrides', error=error)
        return len(_overrides)

    _overrides = {(row.language, row.key): row.value for row in rows}
    return len(_overrides)
