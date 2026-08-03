"""Render one bot screen and report the localization strings it is made of.

Powers the cabinet's screen-first locale editor: pick a screen, see it exactly
as the bot renders it, edit only its strings.

Two guarantees the callers depend on:

* ``draft`` (unsaved edits, sent on every keystroke of a live preview) applies
  to **this render only**. It is carried by a throw-away ``Texts`` instance, so
  it never reaches the process-wide override cache and concurrent requests
  cannot see each other's drafts.
* :func:`render_screen` never raises. Anything that goes wrong comes back as an
  ``error`` field, and the tracing ContextVar is always restored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from app.localization.loader import DEFAULT_LANGUAGE, load_locale
from app.localization.overrides import get_override
from app.localization.texts import Texts
from app.localization.tracing import record_key, trace_keys

from .registry import get_screen
from .synthetic import DEFAULT_SYNTHETIC_STATE, is_known_state


logger = structlog.get_logger(__name__)

_LOCALES_DIR = Path(__file__).resolve().parents[2] / 'localization' / 'locales'


def supported_languages() -> list[str]:
    """Languages that ship with a bundled locale file."""
    return sorted(path.stem for path in _LOCALES_DIR.glob('*.json'))


class _DraftTexts(Texts):
    """``Texts`` with unsaved edits layered on top, for one render.

    The draft wins over the saved admin override — while the owner is typing,
    what they typed is what the preview must show.
    """

    def __init__(self, language: str, draft: dict[str, str]):
        # Присваиваем ДО super().__init__: Texts.__getattr__ уводит любой
        # неизвестный атрибут в _get_value, и обращение к отсутствующему
        # self._draft оттуда ушло бы в бесконечную рекурсию.
        self._draft = dict(draft)
        super().__init__(language)

    def _get_value(self, item: str, warn: bool = True) -> Any:
        if item in self._draft:
            record_key(item)
            return self._draft[item]
        return super()._get_value(item, warn)


def _build_texts(language: str, draft: dict[str, str] | None) -> Texts:
    if draft:
        return _DraftTexts(language, draft)
    return Texts(language)


def _describe_keys(
    keys: list[str],
    language: str,
    draft: dict[str, str] | None,
    *,
    rendered: bool,
) -> list[dict[str, Any]]:
    """Per key: bundled default, saved override and the value actually used."""
    bundled = load_locale(language)
    fallback = bundled if language == DEFAULT_LANGUAGE else load_locale(DEFAULT_LANGUAGE)

    described: list[dict[str, Any]] = []
    for key in keys:
        raw_default = bundled.get(key, fallback.get(key))
        default_value = None if raw_default is None else str(raw_default)
        override_value = get_override(language, key)
        draft_value = draft.get(key) if draft else None

        if draft_value is not None:
            value = draft_value
        elif override_value is not None:
            value = override_value
        else:
            value = default_value

        described.append(
            {
                'key': key,
                'default_value': default_value,
                'override_value': override_value,
                'value': value,
                'rendered': rendered,
            }
        )
    return described


async def render_screen(
    screen_id: str,
    language: str,
    db: Any,
    draft: dict[str, str] | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Render ``screen_id`` in ``language`` and list the strings it is made of.

    ``state`` picks the synthetic user's subscription state, which decides
    *which* status string the screen renders.

    ``keys`` is the union of what this render actually touched
    (``rendered: true``, in trace order) and everything else the screen declares
    (``rendered: false``, in declaration order) — a state cannot reach every
    string, but all of them must stay editable.

    ``draft`` maps key -> unsaved value and is applied to this render only.
    Returns ``{'screen_id', 'language', 'state', 'text', 'keys'}``; on any
    failure the same shape plus ``'error'``.
    """
    resolved_state = state or DEFAULT_SYNTHETIC_STATE
    payload: dict[str, Any] = {
        'screen_id': screen_id,
        'language': language,
        'state': resolved_state,
        'text': '',
        'keys': [],
    }

    try:
        screen = get_screen(screen_id)
        if screen is None:
            payload['error'] = f'Неизвестный экран: {screen_id}'
            return payload

        if language not in supported_languages():
            payload['error'] = f'Неизвестный язык: {language}'
            return payload

        if not is_known_state(resolved_state):
            payload['error'] = f'Неизвестное состояние: {resolved_state}'
            return payload

        texts = _build_texts(language, draft)
        with trace_keys() as touched:
            text = await screen.render(texts, db, resolved_state)
            traced = list(touched)

        declared_only = [key for key in screen.keys if key not in set(traced)]

        payload['text'] = text or ''
        payload['keys'] = _describe_keys(traced, language, draft, rendered=True) + _describe_keys(
            declared_only, language, draft, rendered=False
        )
        return payload
    except Exception as error:
        logger.warning(
            'Не удалось отрендерить превью экрана',
            screen_id=screen_id,
            language=language,
            state=resolved_state,
            error=error,
        )
        payload['text'] = ''
        payload['keys'] = []
        payload['error'] = str(error) or error.__class__.__name__
        return payload
