import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import lifecycle_rules


logger = structlog.get_logger(__name__)


class NotificationSettingsService:
    """Runtime-editable notification settings, backed by the ``lifecycle_rules`` table.

    Публичный classmethod-API сохранён 1:1 с прежней on-disk версией, чтобы
    ``monitoring_service`` (читатели) и бот-панель (читатели+писатели) работали без
    изменений. Внутри — небольшой in-memory кэш ``_cache`` (единственный источник
    для СИНХРОННЫХ геттеров), который наполняется:
      * синхронно из реестра-дефолтов при первом обращении (обратная совместимость:
        пустая БД → дефолты → поведение идентично прежнему);
      * асинхронно из БД через :meth:`reload` — на старте приложения и после записи
        из кабинета (см. C1-контракт).

    Персист: БД (``lifecycle_rules``). Легаси-файл ``data/notification_settings.json``
    импортируется в БД ОДИН раз при первом ``reload`` и остаётся на месте.

    Сеттеры (синхронные, вызываются из async-хендлеров бота) обновляют кэш мгновенно
    и планируют fire-and-forget персист в БД на активном event-loop'е; при отсутствии
    цикла (тесты) кэш всё равно обновлён, а БД догонится следующим ``reload``.
    """

    _storage_path: Path = Path('data/notification_settings.json')

    # Ключи, которыми управляет ИМЕННО этот сервис (группа ``paid`` реестра —
    # исторические ключи NotificationSettingsService). Остальные lifecycle-правила
    # управляются кабинетом напрямую через crud.lifecycle.
    _SERVICE_KEYS: tuple[str, ...] = lifecycle_rules.keys_for_group('paid')

    _cache: dict[str, dict[str, Any]] = {}
    _loaded: bool = False
    _pending_tasks: set[asyncio.Task] = set()

    # ── Внутреннее: дефолты и кэш ──────────────────────────────────────────

    @classmethod
    def _service_default(cls, key: str) -> dict[str, Any]:
        """Дефолтная секция правила в легаси-форме ``{'enabled': bool, **config}``."""
        return {'enabled': lifecycle_rules.default_enabled(key), **lifecycle_rules.default_config(key)}

    @classmethod
    def _ensure_loaded(cls) -> None:
        """Синхронно засевает кэш дефолтами реестра (идемпотентно)."""
        if cls._loaded:
            return
        for key in cls._SERVICE_KEYS:
            cls._cache.setdefault(key, cls._service_default(key))
        cls._loaded = True

    @classmethod
    def _get(cls, key: str) -> dict[str, Any]:
        cls._ensure_loaded()
        value = cls._cache.get(key)
        if not isinstance(value, dict):
            value = cls._service_default(key)
            cls._cache[key] = value
        return value

    @classmethod
    def get_config(cls) -> dict[str, dict[str, Any]]:
        cls._ensure_loaded()
        return deepcopy(cls._cache)

    @classmethod
    def _set_field(cls, key: str, field: str, value: Any) -> bool:
        cls._ensure_loaded()
        section = cls._get(key)
        section[field] = value
        cls._cache[key] = section
        cls._schedule_persist(key)
        return True

    # ── Асинхронный слой: загрузка из БД и персист ─────────────────────────

    @classmethod
    async def reload(cls, db: AsyncSession | None = None) -> None:
        """Перечитывает управляемые правила из БД в кэш.

        Вызывать на старте приложения и после записи из кабинета. При первом
        вызове импортирует легаси-JSON в БД (idempotent). ``db=None`` → сервис
        сам откроет сессию.
        """
        cls._ensure_loaded()
        if db is not None:
            await cls._reload_with_session(db)
            return

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            await cls._reload_with_session(session)

    @classmethod
    async def _reload_with_session(cls, db: AsyncSession) -> None:
        from app.database.crud.lifecycle import get_all_rules

        await cls._import_legacy_json_once(db)

        rules_by_key = {rule.key: rule for rule in await get_all_rules(db)}
        for key in cls._SERVICE_KEYS:
            rule = rules_by_key.get(key)
            if rule is None:
                cls._cache[key] = cls._service_default(key)
                continue
            config = lifecycle_rules.merged_config(key, rule.config or {})
            cls._cache[key] = {'enabled': bool(rule.enabled), **config}

    @classmethod
    async def _import_legacy_json_once(cls, db: AsyncSession) -> None:
        """Одноразовый импорт data/notification_settings.json в БД.

        Импортирует только ключи, которых ещё нет в БД (idempotent — повторный
        старт ничего не перетирает). Файл НЕ удаляется (легаси-фоллбэк).
        """
        if not cls._storage_path.exists():
            return

        try:
            raw = cls._storage_path.read_text(encoding='utf-8')
            legacy = json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            logger.warning('Не удалось прочитать легаси notification_settings.json', error=exc)
            return

        if not isinstance(legacy, dict):
            return

        from app.database.crud.lifecycle import get_rule, upsert_rule

        imported: list[str] = []
        for key in cls._SERVICE_KEYS:
            section = legacy.get(key)
            if not isinstance(section, dict):
                continue
            if await get_rule(db, key) is not None:
                continue  # БД уже управляет ключом — не перетираем кабинетные правки
            enabled = bool(section.get('enabled', lifecycle_rules.default_enabled(key)))
            config = {field: value for field, value in section.items() if field != 'enabled'}
            await upsert_rule(db, key, enabled, config)
            imported.append(key)

        if imported:
            logger.info(
                'Импортированы легаси notification_settings в БД',
                keys=imported,
                file=str(cls._storage_path),
            )

    @classmethod
    def _schedule_persist(cls, key: str) -> None:
        """Планирует fire-and-forget персист правила в БД на активном loop'е."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Нет активного цикла (тесты/sync-контекст): кэш обновлён, БД догонится
            # следующим reload() из кабинета или на старте. Не бросаем.
            return
        task = loop.create_task(cls._persist_key(key))
        cls._pending_tasks.add(task)
        task.add_done_callback(cls._pending_tasks.discard)

    @classmethod
    async def _persist_key(cls, key: str) -> None:
        section = deepcopy(cls._cache.get(key, {}))
        enabled = bool(section.pop('enabled', lifecycle_rules.default_enabled(key)))
        try:
            from app.database.crud.lifecycle import upsert_rule
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                await upsert_rule(db, key, enabled, section)
        except Exception as exc:
            logger.warning('Не удалось персистить lifecycle-правило в БД', key=key, error=exc)

    # ── Публичный API (сохранён 1:1) ──────────────────────────────────────

    @classmethod
    def set_enabled(cls, key: str, enabled: bool) -> bool:
        return cls._set_field(key, 'enabled', bool(enabled))

    @classmethod
    def is_enabled(cls, key: str) -> bool:
        return bool(cls._get(key).get('enabled', True))

    @classmethod
    def is_trial_channel_unsubscribed_enabled(cls) -> bool:
        return cls.is_enabled('trial_channel_unsubscribed')

    @classmethod
    def set_trial_channel_unsubscribed_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('trial_channel_unsubscribed', enabled)

    # Expired subscription notifications
    @classmethod
    def is_expired_1d_enabled(cls) -> bool:
        return cls.is_enabled('expired_1d')

    @classmethod
    def set_expired_1d_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_1d', enabled)

    @classmethod
    def is_second_wave_enabled(cls) -> bool:
        return cls.is_enabled('expired_second_wave')

    @classmethod
    def set_second_wave_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_second_wave', enabled)

    @classmethod
    def get_second_wave_discount_percent(cls) -> int:
        value = cls._get('expired_second_wave').get('discount_percent', 10)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 10

    @classmethod
    def set_second_wave_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_second_wave', 'discount_percent', percent_int)

    @classmethod
    def get_second_wave_valid_hours(cls) -> int:
        value = cls._get('expired_second_wave').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_second_wave_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_second_wave', 'valid_hours', hours_int)

    @classmethod
    def is_third_wave_enabled(cls) -> bool:
        return cls.is_enabled('expired_third_wave')

    @classmethod
    def set_third_wave_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_third_wave', enabled)

    @classmethod
    def get_third_wave_discount_percent(cls) -> int:
        value = cls._get('expired_third_wave').get('discount_percent', 20)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 20

    @classmethod
    def set_third_wave_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'discount_percent', percent_int)

    @classmethod
    def get_third_wave_valid_hours(cls) -> int:
        value = cls._get('expired_third_wave').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_third_wave_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'valid_hours', hours_int)

    @classmethod
    def get_third_wave_trigger_days(cls) -> int:
        value = cls._get('expired_third_wave').get('trigger_days', 5)
        try:
            return max(2, min(60, int(value)))
        except (TypeError, ValueError):
            return 5

    @classmethod
    def set_third_wave_trigger_days(cls, days: int) -> bool:
        try:
            days_int = max(2, min(60, int(days)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'trigger_days', days_int)

    @classmethod
    def are_notifications_globally_enabled(cls) -> bool:
        return bool(getattr(settings, 'ENABLE_NOTIFICATIONS', True))
