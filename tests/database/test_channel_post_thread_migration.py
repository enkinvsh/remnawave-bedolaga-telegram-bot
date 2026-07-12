"""Migration sanity for channel_posts.message_thread_id (0099 → 0100)."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_VERSIONS = Path(__file__).resolve().parents[2] / 'migrations' / 'alembic' / 'versions'


def _load(filename: str):
    path = _VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_0099_to_0100() -> None:
    mig = _load('0100_channel_post_thread.py')
    assert mig.revision == '0100'
    assert mig.down_revision == '0099'


def test_upgrade_downgrade_defined() -> None:
    mig = _load('0100_channel_post_thread.py')
    assert callable(mig.upgrade)
    assert callable(mig.downgrade)


def test_adds_only_message_thread_id() -> None:
    source = (_VERSIONS / '0100_channel_post_thread.py').read_text(encoding='utf-8')
    assert "add_column(\n            'channel_posts'," in source
    assert 'message_thread_id' in source
    assert source.count('op.add_column') == 1
    assert source.count('op.drop_column') == 1
