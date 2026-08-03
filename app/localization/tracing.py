"""Opt-in recording of the localization keys a render actually touched.

Powers the cabinet's screen preview: to tell the owner "this screen is made of
exactly these six strings" we need the keys that really resolved, not a guess
from grepping the handler.

``Texts._get_value`` runs for **every** string of **every** message, so the
default state is off and costs exactly one :meth:`ContextVar.get` plus an
``is None`` check — no allocation, no I/O, no logging. Nothing here is async:
``_get_value`` must stay synchronous.

Isolation comes from :class:`~contextvars.ContextVar`: an asyncio task started
by ``gather``/``create_task`` copies the context at creation, so two concurrent
renders never write into each other's list. A nested :func:`trace_keys` shadows
the outer one — keys touched inside the inner block belong to the inner list
only.

Kept free of FastAPI/aiogram/DB imports so the bot and the web layer can share it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


# The active recorder, or None when tracing is off (the normal case).
# The list is ordered by first touch and de-duplicated by `record_key`.
_recorder: ContextVar[list[str] | None] = ContextVar('locale_key_recorder', default=None)


def get_recorder() -> list[str] | None:
    """The list keys are being recorded into, or ``None`` when tracing is off."""
    return _recorder.get()


def record_key(key: str) -> None:
    """Note that ``key`` resolved. No-op unless a :func:`trace_keys` is active.

    Hot path: called once per resolved localization string. Keep it to the
    ContextVar read and the ``is None`` check when tracing is off.
    """
    recorder = _recorder.get()
    if recorder is None:
        return
    if key not in recorder:
        recorder.append(key)


@contextmanager
def trace_keys() -> Iterator[list[str]]:
    """Record localization keys resolved inside the block.

    Yields the live list — ordered by first touch, de-duplicated — so it can be
    inspected inside the block as well as after it. The recorder is always
    restored on exit, including when the body raises.
    """
    keys: list[str] = []
    token = _recorder.set(keys)
    try:
        yield keys
    finally:
        _recorder.reset(token)
