"""Дубль сессии БД для тестов, которые рендерят меню при включённом конструкторе.

ЗАЧЕМ.
`MENU_LAYOUT_ENABLED=True` заставляет `get_main_menu_keyboard_async` собирать
клавиатуру через `MenuLayoutService.build_keyboard`, а тот на первом рендере после
сброса кеша читает `SystemSetting` из БД. Любой тест, подсовывавший туда голый
`AsyncMock()`, падает: `execute()` у него возвращает корутину, и `get_config`
разбивается о `setting.value` ещё до единого ассерта. Дубль здесь — минимальная
замена `AsyncSession` ровно под `select(SystemSetting).where(key == ...)`.

ПУСТОЕ ХРАНИЛИЩЕ = ПРОД. Строки `menu_layout_config` в боевой БД нет, поэтому
`get_config` отдаёт `get_default_config()`. Тест с пустым стором проверяет ровно ту
раскладку, которую видят люди.

ГРАБЛЯ, РАДИ КОТОРОЙ ЭТОТ МОДУЛЬ ОБЩИЙ: `MenuLayoutService._cache` — КЛАССОВЫЙ
глобал. Не сбросив его до и после теста, получаешь конфигурацию соседнего теста, и
дубль БД вообще перестаёт вызываться — тест «проходит», ничего не проверив.
`menu_layout_default_config()` держит эту дисциплину в одном месте, чтобы её нельзя
было забыть в очередном файле.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.services.menu_layout.service import MenuLayoutService


class FakeSettingsResult:
    """Результат `execute()` с единственным нужным методом."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class FakeSettingsStoreDB:
    """Минимальная замена AsyncSession для `select(SystemSetting).where(key == ...)`."""

    def __init__(self, store: dict[str, Any] | None = None) -> None:
        self.store: dict[str, Any] = store if store is not None else {}

    async def execute(self, statement):
        key = statement.whereclause.right.value
        return FakeSettingsResult(self.store.get(key))

    def add(self, obj) -> None:
        self.store[obj.key] = obj

    async def flush(self) -> None:
        """Фейковая сессия ничего не сбрасывает на диск."""

    async def commit(self) -> None:
        """Фейковая сессия ничего не коммитит."""


@contextmanager
def menu_layout_default_config() -> Iterator[FakeSettingsStoreDB]:
    """Пустое хранилище настроек + сброс классового кеша до и после."""
    MenuLayoutService.invalidate_cache()
    try:
        yield FakeSettingsStoreDB()
    finally:
        MenuLayoutService.invalidate_cache()
