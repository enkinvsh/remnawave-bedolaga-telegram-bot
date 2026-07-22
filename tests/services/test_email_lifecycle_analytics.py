from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from app.services import email_lifecycle_analytics


class _SeededDb:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def execute(self, statement, parameters=None):
        return self.connection.execute(statement, parameters or {})


def _session() -> tuple[_SeededDb, Engine, Connection]:
    engine = create_engine('sqlite:///:memory:')
    connection = engine.connect()
    with connection.begin():
        connection.execute(
            text(
                'CREATE TABLE lifecycle_message_log ('
                'id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, rule_key VARCHAR(64) NOT NULL, '
                'occurrence INTEGER NOT NULL, sent_at DATETIME NOT NULL)'
            )
        )
        connection.execute(
            text(
                'CREATE TABLE subscriptions ('
                'id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, is_trial BOOLEAN NOT NULL, created_at DATETIME NOT NULL)'
            )
        )
        connection.execute(
            text(
                'CREATE TABLE transactions ('
                'id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, type VARCHAR(50) NOT NULL, '
                'amount_kopeks INTEGER NOT NULL, is_completed BOOLEAN NOT NULL, created_at DATETIME NOT NULL)'
            )
        )
    return _SeededDb(connection), engine, connection


async def test_funnel_counts_only_activity_after_first_marker() -> None:
    db, engine, connection = _session()
    marker = datetime(2026, 7, 1, 12, tzinfo=UTC)
    with connection.begin():
        await db.execute(
            text(
                'INSERT INTO lifecycle_message_log VALUES '
                '(1, 10, :rule, 1, :marker), (2, 10, :rule, 2, :later), (3, 20, :rule, 1, :marker)'
            ),
            {'rule': 'post_trial_ladder:7_email', 'marker': marker, 'later': marker + timedelta(hours=1)},
        )
        await db.execute(
            text(
                'INSERT INTO subscriptions VALUES '
                '(1, 10, true, :after), (2, 20, true, :before)'
            ),
            {'after': marker + timedelta(days=1), 'before': marker - timedelta(days=1)},
        )
        await db.execute(
            text(
                "INSERT INTO transactions VALUES "
                "(1, 10, 'deposit', 12000, true, :after), "
                "(2, 10, 'deposit', 9000, true, :before), "
                "(3, 20, 'deposit', 7000, false, :after)"
            ),
            {'after': marker + timedelta(days=2), 'before': marker - timedelta(days=1)},
        )

    rows = await email_lifecycle_analytics.get_funnel(db)

    assert rows == [
        email_lifecycle_analytics.FunnelItem(
            rule_key='post_trial_ladder:7_email',
            touched=2,
            trials_after=1,
            deposits_after=1,
            revenue_after_kopeks=12000,
        )
    ]
    connection.close()
    engine.dispose()


async def test_timeline_fills_days_without_markers() -> None:
    db, engine, connection = _session()
    now = datetime(2026, 7, 10, 15, tzinfo=UTC)
    with connection.begin():
        await db.execute(
            text(
                'INSERT INTO lifecycle_message_log VALUES '
                '(1, 10, :first, 1, :old), (2, 20, :second, 1, :today)'
            ),
            {
                'first': 'winback_oneoff',
                'second': 'trial_ending:3_email',
                'old': now - timedelta(days=2),
                'today': now,
            },
        )

    days = await email_lifecycle_analytics.get_timeline(db, days=3, now=now)

    assert days == [
        email_lifecycle_analytics.TimelineDay(date='2026-07-08', by_event={'winback_oneoff': 1}),
        email_lifecycle_analytics.TimelineDay(date='2026-07-09', by_event={}),
        email_lifecycle_analytics.TimelineDay(date='2026-07-10', by_event={'trial_ending:3_email': 1}),
    ]
    connection.close()
    engine.dispose()
