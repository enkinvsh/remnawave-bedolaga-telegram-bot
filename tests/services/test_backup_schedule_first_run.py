"""Regression test: the FIRST auto-backup run after a (re)start must honour the
BACKUP_INTERVAL_HOURS for sub-daily intervals instead of deferring to the daily
BACKUP_TIME.

Bug: start_auto_backup() → _calculate_next_backup_datetime() anchored the first
run to BACKUP_TIME and pushed it +1 day if that time had already passed. With an
hourly interval that paused ALL hourly backups after any bot restart until the
next BACKUP_TIME (e.g. next midnight). _next_future_run only kicks in AFTER the
first backup, so it never rescued the deferred first run. Observed on prod
dropweb_bot: last backup 05:00 UTC, restart ~08:40 UTC, scheduler reported
next_run='23.07.2026 00:00:00' — a ~19h hole.

The fix walks the grid {BACKUP_TIME + k*interval} to the next FUTURE slot for
sub-daily intervals, while keeping the historical "next BACKUP_TIME" semantics
for daily-or-longer intervals (white-label tenants that back up once a day at a
fixed wall-clock time must be unaffected).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.backup_service import BackupService


_NOW = datetime(2026, 7, 22, 15, 39, 12, tzinfo=UTC)


def test_hourly_restart_after_backup_time_schedules_next_hour_not_midnight():
    # BACKUP_TIME=00:00, hourly, restart mid-afternoon → next top-of-hour, NOT tomorrow 00:00.
    result = BackupService._next_scheduled_run((0, 0), timedelta(hours=1), _NOW)
    assert result == datetime(2026, 7, 22, 16, 0, 0, tzinfo=UTC)


def test_hourly_with_nonzero_backup_time_minute_anchor():
    # BACKUP_TIME=00:30 → grid at HH:30. now 15:39 → next slot 16:30 (15:30 already passed).
    result = BackupService._next_scheduled_run((0, 30), timedelta(hours=1), _NOW)
    assert result == datetime(2026, 7, 22, 16, 30, 0, tzinfo=UTC)


def test_subdaily_backup_time_in_future_today_walks_back_onto_grid():
    # BACKUP_TIME=23:00 (still ahead today), hourly → next hourly slot 16:00, not 23:00.
    result = BackupService._next_scheduled_run((23, 0), timedelta(hours=1), _NOW)
    assert result == datetime(2026, 7, 22, 16, 0, 0, tzinfo=UTC)


def test_six_hour_interval_grid_from_midnight():
    # BACKUP_TIME=00:00, every 6h → grid 00,06,12,18. now 15:39 → 18:00.
    result = BackupService._next_scheduled_run((0, 0), timedelta(hours=6), _NOW)
    assert result == datetime(2026, 7, 22, 18, 0, 0, tzinfo=UTC)


def test_exactly_on_slot_advances_to_next_slot():
    # now exactly on an hourly slot → next slot (no immediate double-fire), matches old <= convention.
    on_slot = datetime(2026, 7, 22, 16, 0, 0, tzinfo=UTC)
    result = BackupService._next_scheduled_run((0, 0), timedelta(hours=1), on_slot)
    assert result == datetime(2026, 7, 22, 17, 0, 0, tzinfo=UTC)


def test_daily_interval_preserves_next_backup_time_today():
    # interval=24h, BACKUP_TIME=23:00 still ahead today → today 23:00 (unchanged daily behaviour).
    result = BackupService._next_scheduled_run((23, 0), timedelta(hours=24), _NOW)
    assert result == datetime(2026, 7, 22, 23, 0, 0, tzinfo=UTC)


def test_daily_interval_rolls_to_tomorrow_when_time_passed():
    # interval=24h, BACKUP_TIME=03:00 already passed → tomorrow 03:00 (unchanged daily behaviour).
    result = BackupService._next_scheduled_run((3, 0), timedelta(hours=24), _NOW)
    assert result == datetime(2026, 7, 23, 3, 0, 0, tzinfo=UTC)


def test_daily_interval_exact_equality_rolls_to_tomorrow():
    # interval=24h, BACKUP_TIME == now (to the minute) → strictly-after → tomorrow (unchanged).
    on_time = datetime(2026, 7, 22, 3, 0, 0, tzinfo=UTC)
    result = BackupService._next_scheduled_run((3, 0), timedelta(hours=24), on_time)
    assert result == datetime(2026, 7, 23, 3, 0, 0, tzinfo=UTC)


def test_five_hour_nondivisor_grid_from_midnight():
    # every 5h anchored at 00:00 → grid 00,05,10,15,20. now 15:39 → 20:00.
    result = BackupService._next_scheduled_run((0, 0), timedelta(hours=5), _NOW)
    assert result == datetime(2026, 7, 22, 20, 0, 0, tzinfo=UTC)


def test_twelve_hour_divisor_is_restart_stable():
    # every 12h (divides 24) → grid 00,12 regardless of restart moment.
    restart = datetime(2026, 7, 22, 8, 40, 0, tzinfo=UTC)
    result = BackupService._next_scheduled_run((0, 0), timedelta(hours=12), restart)
    assert result == datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def test_first_run_then_continuation_keeps_clean_hourly_cadence():
    # The real prod scenario: restart 08:40 → first 09:00, then _next_future_run holds the grid.
    interval = timedelta(hours=1)
    restart = datetime(2026, 7, 22, 8, 40, 0, tzinfo=UTC)
    first = BackupService._next_scheduled_run((0, 0), interval, restart)
    assert first == datetime(2026, 7, 22, 9, 0, 0, tzinfo=UTC)
    # backup completes ~09:00:03 → next slot 10:00 (not a double-fire, not a skip)
    second = BackupService._next_future_run(first, interval, datetime(2026, 7, 22, 9, 0, 3, tzinfo=UTC))
    assert second == datetime(2026, 7, 22, 10, 0, 0, tzinfo=UTC)
    third = BackupService._next_future_run(second, interval, datetime(2026, 7, 22, 10, 0, 5, tzinfo=UTC))
    assert third == datetime(2026, 7, 22, 11, 0, 0, tzinfo=UTC)


def test_non_positive_interval_raises():
    import pytest

    for bad in (timedelta(0), timedelta(hours=-1)):
        with pytest.raises(ValueError):
            BackupService._next_scheduled_run((0, 0), bad, _NOW)
