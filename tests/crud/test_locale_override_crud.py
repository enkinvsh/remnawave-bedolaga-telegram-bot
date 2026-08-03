"""CRUD-тесты override'ов локализации на реальном SQLite."""

import sys
from importlib import import_module

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database.crud.locale_override import (
    delete_locale_override,
    get_locale_override,
    list_locale_overrides,
    upsert_locale_override,
)
from app.database.models import LocaleOverride


def _make_engine(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)
    import_module('aiosqlite')
    return create_async_engine(
        f'sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true',
        poolclass=NullPool,
    )


async def test_upsert_is_idempotent_and_updates_value(monkeypatch: pytest.MonkeyPatch):
    engine = _make_engine(monkeypatch, 'locale_override_upsert')
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.connect() as keeper:
            await keeper.run_sync(LocaleOverride.__table__.create)
            await keeper.commit()

            async with session_factory() as db:
                await upsert_locale_override(db, key='ACCESS_DENIED', language='ru', value='Первое')
                await db.commit()

                await upsert_locale_override(db, key='ACCESS_DENIED', language='ru', value='Второе')
                await db.commit()

                rows = await list_locale_overrides(db)
                assert len(rows) == 1
                assert rows[0].value == 'Второе'

                existing = await get_locale_override(db, key='ACCESS_DENIED', language='ru')
                assert existing is not None
                assert existing.value == 'Второе'
    finally:
        await engine.dispose()


async def test_unique_key_language_constraint_holds(monkeypatch: pytest.MonkeyPatch):
    engine = _make_engine(monkeypatch, 'locale_override_unique')
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.connect() as keeper:
            await keeper.run_sync(LocaleOverride.__table__.create)
            await keeper.commit()

            async with session_factory() as db:
                db.add(LocaleOverride(key='ACCESS_DENIED', language='ru', value='A'))
                db.add(LocaleOverride(key='ACCESS_DENIED', language='ru', value='B'))
                with pytest.raises(IntegrityError):
                    await db.commit()
    finally:
        await engine.dispose()


async def test_same_key_different_languages_coexist(monkeypatch: pytest.MonkeyPatch):
    engine = _make_engine(monkeypatch, 'locale_override_langs')
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.connect() as keeper:
            await keeper.run_sync(LocaleOverride.__table__.create)
            await keeper.commit()

            async with session_factory() as db:
                await upsert_locale_override(db, key='ACCESS_DENIED', language='ru', value='Отказ')
                await upsert_locale_override(db, key='ACCESS_DENIED', language='en', value='Denied')
                await db.commit()

                rows = await list_locale_overrides(db)
                assert {(r.language, r.value) for r in rows} == {('ru', 'Отказ'), ('en', 'Denied')}
    finally:
        await engine.dispose()


async def test_delete_removes_row_and_reports_result(monkeypatch: pytest.MonkeyPatch):
    engine = _make_engine(monkeypatch, 'locale_override_delete')
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.connect() as keeper:
            await keeper.run_sync(LocaleOverride.__table__.create)
            await keeper.commit()

            async with session_factory() as db:
                await upsert_locale_override(db, key='ACCESS_DENIED', language='ru', value='Отказ')
                await db.commit()

                assert await delete_locale_override(db, key='ACCESS_DENIED', language='ru') is True
                await db.commit()

                assert await list_locale_overrides(db) == []
                assert await delete_locale_override(db, key='ACCESS_DENIED', language='ru') is False
    finally:
        await engine.dispose()
