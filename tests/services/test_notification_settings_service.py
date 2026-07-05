"""DB-backed NotificationSettingsService (кэш + reload + легаси-импорт).

Ключевой инвариант обратной совместимости: с ПУСТОЙ БД (только дефолты реестра)
геттеры возвращают ровно те же значения, что прежняя on-disk версия, поэтому
monitoring_service ведёт себя идентично.
"""

import json
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.database.crud import lifecycle as lifecycle_crud
from app.services.notification_settings_service import NotificationSettingsService as N


@pytest.fixture(autouse=True)
def _reset_service_state():
    N._cache = {}
    N._loaded = False
    N._pending_tasks = set()
    yield
    N._cache = {}
    N._loaded = False
    N._pending_tasks = set()


def _rule(key: str, enabled: bool, config: dict):
    return types.SimpleNamespace(key=key, enabled=enabled, config=config)


# ── обратная совместимость: пустая БД → легаси-дефолты ─────────────────────


def test_empty_state_returns_legacy_defaults():
    assert N.get_second_wave_discount_percent() == 10
    assert N.get_second_wave_valid_hours() == 24
    assert N.get_third_wave_discount_percent() == 20
    assert N.get_third_wave_valid_hours() == 24
    assert N.get_third_wave_trigger_days() == 5
    assert N.is_expired_1d_enabled() is True
    assert N.is_second_wave_enabled() is True
    assert N.is_third_wave_enabled() is True
    assert N.is_trial_channel_unsubscribed_enabled() is True


def test_get_config_shape_matches_legacy():
    config = N.get_config()
    assert set(config) == {
        'expired_1d',
        'expired_second_wave',
        'expired_third_wave',
        'trial_channel_unsubscribed',
    }
    assert config['expired_second_wave'] == {'enabled': True, 'discount_percent': 10, 'valid_hours': 24}
    assert config['expired_third_wave'] == {
        'enabled': True,
        'discount_percent': 20,
        'valid_hours': 24,
        'trigger_days': 5,
    }


# ── синхронные сеттеры обновляют кэш (без активного loop → без персиста) ────


def test_setter_updates_cache_in_place():
    assert N.set_second_wave_discount_percent(15) is True
    assert N.get_second_wave_discount_percent() == 15
    # get_config видит обновление немедленно (тот же процесс).
    assert N.get_config()['expired_second_wave']['discount_percent'] == 15


def test_setter_clamps_and_rejects_invalid():
    assert N.set_second_wave_discount_percent(999) is True
    assert N.get_second_wave_discount_percent() == 100  # клампится к 100
    assert N.set_second_wave_discount_percent('abc') is False  # мусор → False
    assert N.set_third_wave_trigger_days(1) is True
    assert N.get_third_wave_trigger_days() == 2  # клампится к минимуму 2


def test_toggle_enabled_updates_cache():
    assert N.is_second_wave_enabled() is True
    assert N.set_second_wave_enabled(False) is True
    assert N.is_second_wave_enabled() is False


# ── reload: БД-оверрайд поверх дефолтов ────────────────────────────────────


async def test_reload_overlays_db_values(monkeypatch):
    monkeypatch.setattr(N, '_storage_path', Path('/nonexistent/notification_settings.json'))
    monkeypatch.setattr(
        lifecycle_crud,
        'get_all_rules',
        AsyncMock(return_value=[_rule('expired_second_wave', False, {'discount_percent': 15})]),
    )

    await N.reload(db=AsyncMock())

    assert N.get_second_wave_discount_percent() == 15
    assert N.is_second_wave_enabled() is False
    # valid_hours не был в оверрайде → добирается из дефолта.
    assert N.get_second_wave_valid_hours() == 24
    # Ключ без строки в БД остаётся на дефолте.
    assert N.get_third_wave_discount_percent() == 20


# ── одноразовый импорт легаси-JSON в БД ────────────────────────────────────


async def test_legacy_json_imported_once(monkeypatch, tmp_path):
    legacy_file = tmp_path / 'notification_settings.json'
    legacy_file.write_text(
        json.dumps(
            {
                'expired_second_wave': {'enabled': False, 'discount_percent': 12, 'valid_hours': 48},
                'expired_1d': {'enabled': True},
            }
        ),
        encoding='utf-8',
    )
    monkeypatch.setattr(N, '_storage_path', legacy_file)
    monkeypatch.setattr(lifecycle_crud, 'get_rule', AsyncMock(return_value=None))
    upsert = AsyncMock()
    monkeypatch.setattr(lifecycle_crud, 'upsert_rule', upsert)

    await N._import_legacy_json_once(AsyncMock())

    calls = {call.args[1]: call.args for call in upsert.await_args_list}
    assert set(calls) == {'expired_second_wave', 'expired_1d'}
    # enabled вынесен из config'а в отдельный аргумент.
    _db, key, enabled, config = calls['expired_second_wave']
    assert enabled is False
    assert config == {'discount_percent': 12, 'valid_hours': 48}
    assert 'enabled' not in config
    # Файл остаётся на месте (легаси-фоллбэк).
    assert legacy_file.exists()


async def test_legacy_json_import_skips_keys_already_in_db(monkeypatch, tmp_path):
    legacy_file = tmp_path / 'notification_settings.json'
    legacy_file.write_text(json.dumps({'expired_1d': {'enabled': False}}), encoding='utf-8')
    monkeypatch.setattr(N, '_storage_path', legacy_file)
    # БД уже управляет ключом → не перетираем кабинетные правки.
    monkeypatch.setattr(lifecycle_crud, 'get_rule', AsyncMock(return_value=object()))
    upsert = AsyncMock()
    monkeypatch.setattr(lifecycle_crud, 'upsert_rule', upsert)

    await N._import_legacy_json_once(AsyncMock())

    upsert.assert_not_awaited()


async def test_reload_without_file_uses_defaults(monkeypatch):
    monkeypatch.setattr(N, '_storage_path', Path('/nonexistent/x.json'))
    monkeypatch.setattr(lifecycle_crud, 'get_all_rules', AsyncMock(return_value=[]))

    await N.reload(db=AsyncMock())

    assert N.get_second_wave_discount_percent() == 10
    assert N.is_expired_1d_enabled() is True
