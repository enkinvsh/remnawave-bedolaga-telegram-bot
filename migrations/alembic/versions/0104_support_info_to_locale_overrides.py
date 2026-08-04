"""support info -> locale_overrides

Текст экрана поддержки жил в двух хранилищах сразу: legacy-файл
``data/support_settings.json`` (ключ ``support_info_texts``) и таблица
``locale_overrides`` из редактора локалей. Побеждал файл, поэтому правка
``SUPPORT_INFO`` в кабинете «не срабатывала».

Миграция переносит тексты из файла в ``locale_overrides``, чтобы источник
остался один. Сам файл НЕ трогаем: код его больше не читает, и он остаётся
резервной копией.

Revision ID: 0104
Revises: 0103
Create Date: 2026-08-04

"""

import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0104'
down_revision: Union[str, None] = '0103'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_PATH = Path('data/support_settings.json')
_SUPPORT_KEY = 'SUPPORT_INFO'


def _legacy_support_texts(raw: object) -> list[tuple[str, str]]:
    """(язык, текст) из legacy-хранилища; мусор отбрасывается."""
    if not isinstance(raw, dict):
        return []

    result: list[tuple[str, str]] = []
    for language, value in raw.items():
        if not isinstance(language, str) or not isinstance(value, str) or not value.strip():
            continue
        # Тот же разбор кода языка, что делал legacy-геттер: 'ru-RU' -> 'ru'.
        normalized = language.split('-')[0].lower()
        if not normalized:
            continue
        result.append((normalized, value))

    return result


def upgrade() -> None:
    if not _LEGACY_PATH.exists():
        return

    try:
        data = json.loads(_LEGACY_PATH.read_text(encoding='utf-8'))
    except Exception:
        # Битый или нечитаемый файл не должен ронять `alembic upgrade`:
        # переносить нечего, миграция становится no-op.
        return

    if not isinstance(data, dict):
        return

    entries = _legacy_support_texts(data.get('support_info_texts'))
    if not entries:
        return

    conn = op.get_bind()
    for language, value in entries:
        existing = conn.execute(
            sa.text('SELECT 1 FROM locale_overrides WHERE key = :key AND language = :language'),
            {'key': _SUPPORT_KEY, 'language': language},
        ).first()
        if existing is not None:
            # В редакторе уже что-то сохранено — это свежее файла, не затираем.
            continue

        # created_at/updated_at заполняет server_default из миграции 0103.
        conn.execute(
            sa.text('INSERT INTO locale_overrides (key, language, value) VALUES (:key, :language, :value)'),
            {'key': _SUPPORT_KEY, 'language': language, 'value': value},
        )


def downgrade() -> None:
    # Намеренно no-op: перенесённая строка ничем не отличается от той, которую
    # владелец набрал в редакторе локалей. Удаление по ключу снесло бы и его
    # правки, поэтому откат оставляем ручным.
    pass
