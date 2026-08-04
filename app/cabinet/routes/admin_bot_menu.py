"""Роуты кабинета для конструктора меню БОТА (настройка `menu_layout_config`).

Тонкий мост над `MenuLayoutService`: тот же слой сервиса, что и у админского REST API
(`/menu-layout` в `app/webapi/routes/menu_layout.py`), но с авторизацией и сессией
кабинета. Вся логика раскладки живёт в сервисе — здесь только делегирование, чтобы
две реализации не разъехались.

ВАЖНО: это НЕ `admin_menu_layout.py`. Тот правит `CABINET_MENU_LAYOUT` — меню самого
кабинета (работает при `MAIN_MENU_MODE=cabinet`). Здесь правится `menu_layout_config` —
главное меню бота. Две разные системы, живущие рядом.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.menu_layout.schemas import (
    AvailableCallback,
    AvailableCallbacksResponse,
    BuiltinButtonInfo,
    BuiltinButtonsListResponse,
    ButtonConditions,
    DynamicPlaceholder,
    DynamicPlaceholdersResponse,
    MenuButtonConfig,
    MenuLayoutResponse,
    MenuLayoutUpdateRequest,
    MenuPreviewButton,
    MenuPreviewRequest,
    MenuPreviewResponse,
    MenuPreviewRow,
    MenuRowConfig,
)
from app.services.menu_layout_service import MENU_LAYOUT_CONFIG_KEY, MenuContext, MenuLayoutService
from app.services.permission_service import PermissionService

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/bot-menu', tags=['Admin Bot Menu'])


# ---- Хелперы -----------------------------------------------------------------


def _serialize_config(config: dict, is_enabled: bool, updated_at) -> MenuLayoutResponse:
    """Сериализовать конфигурацию сервиса в ответ API.

    Битые записи пропускаем с предупреждением — так же, как это делает webapi:
    одна испорченная кнопка не должна ронять весь экран конструктора.
    """
    rows: list[MenuRowConfig] = []
    for row_data in config.get('rows', []):
        try:
            rows.append(MenuRowConfig.model_validate(row_data))
        except ValidationError as error:
            logger.warning(
                'Ошибка сериализации ряда меню бота, пропускаю',
                row_id=row_data.get('id') if isinstance(row_data, dict) else None,
                error=str(error),
            )

    buttons: dict[str, MenuButtonConfig] = {}
    for button_id, button_data in config.get('buttons', {}).items():
        try:
            buttons[button_id] = MenuButtonConfig.model_validate(button_data)
        except ValidationError as error:
            logger.warning('Ошибка сериализации кнопки меню бота, пропускаю', button_id=button_id, error=str(error))

    return MenuLayoutResponse(
        version=config.get('version', 1),
        rows=rows,
        buttons=buttons,
        is_enabled=is_enabled,
        updated_at=updated_at,
    )


def _build_config(payload: MenuLayoutUpdateRequest, current: dict) -> dict:
    """Собрать новую конфигурацию поверх текущей (зеркало webapi PUT)."""
    config = current.copy()

    if payload.rows is not None:
        config['rows'] = [row.model_dump(mode='json') for row in payload.rows]

    if payload.buttons is not None:
        buttons_config: dict[str, dict] = {}
        for button_id, button in payload.buttons.items():
            button_dict = button.model_dump(mode='json')
            # Плейсхолдеры определяем автоматически, если dynamic_text не выставлен явно
            if not button_dict.get('dynamic_text', False):
                button_dict['dynamic_text'] = MenuLayoutService._text_has_placeholders(
                    button_dict.get('text', {}),
                    button_dict.get('text_key'),
                )
            buttons_config[button_id] = button_dict
        config['buttons'] = buttons_config

    return config


def _external_urls(config: dict) -> list[str]:
    """Собрать внешние ссылки раскладки — самая рискованная часть изменения.

    Аудит должен позволять ответить «какие ссылки админ поставил в меню бота»,
    не поднимая всю конфигурацию из истории.
    """
    urls: list[str] = []
    for button in config.get('buttons', {}).values():
        if not isinstance(button, dict):
            continue
        if button.get('type') in ('url', 'mini_app') and button.get('action'):
            urls.append(button['action'])
        if button.get('webapp_url'):
            urls.append(button['webapp_url'])
    return urls


async def _audit(db: AsyncSession, admin: User, *, action: str, details: dict) -> None:
    """Записать изменение меню бота в админский журнал (`admin_audit_log`).

    `log_action` только делает flush, поэтому коммит нужен явно — так же, как в
    соседних роутерах кабинета (см. `admin_channel_posts.py`).
    """
    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action=action,
        resource_type='bot_menu',
        resource_id=MENU_LAYOUT_CONFIG_KEY,
        details=details,
        status='success',
    )
    await db.commit()


def _require_valid_config(config: dict) -> None:
    """Проверить конфигурацию сервисом и не дать сохранить мусор."""
    result = MenuLayoutService.validate_config(config)
    if not result['is_valid']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'message': 'Некорректная конфигурация меню', 'errors': result['errors']},
        )


# ---- Роуты -------------------------------------------------------------------


@router.get('', response_model=MenuLayoutResponse)
async def get_bot_menu_layout(
    _admin: User = Depends(require_permission('bot_menu:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MenuLayoutResponse:
    """Получить текущую конфигурацию меню бота."""
    config = await MenuLayoutService.get_config(db)
    updated_at = await MenuLayoutService.get_config_updated_at(db)
    return _serialize_config(config, settings.MENU_LAYOUT_ENABLED, updated_at)


@router.put('', response_model=MenuLayoutResponse)
async def update_bot_menu_layout(
    payload: MenuLayoutUpdateRequest,
    admin: User = Depends(require_permission('bot_menu:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MenuLayoutResponse:
    """Сохранить конфигурацию меню бота целиком (после валидации)."""
    current = await MenuLayoutService.get_config(db)
    config = _build_config(payload, current)

    _require_valid_config(config)

    # save_config коммитит и сам инвалидирует кеш сервиса — бот подхватит без рестарта
    await MenuLayoutService.save_config(db, config)
    updated_at = await MenuLayoutService.get_config_updated_at(db)

    await _audit(
        db,
        admin,
        action='bot_menu_update',
        details={
            'rows_count': len(config.get('rows', [])),
            'buttons_count': len(config.get('buttons', {})),
            'row_ids': [row.get('id') for row in config.get('rows', [])],
            'external_urls': _external_urls(config),
        },
    )

    logger.info(
        'Админ обновил меню бота из кабинета',
        telegram_id=getattr(admin, 'telegram_id', None),
        rows_count=len(config.get('rows', [])),
        buttons_count=len(config.get('buttons', {})),
    )

    return _serialize_config(config, settings.MENU_LAYOUT_ENABLED, updated_at)


@router.post('/reset', response_model=MenuLayoutResponse)
async def reset_bot_menu_layout(
    admin: User = Depends(require_permission('bot_menu:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MenuLayoutResponse:
    """Сбросить конфигурацию меню бота к дефолтной."""
    config = await MenuLayoutService.reset_to_default(db)
    updated_at = await MenuLayoutService.get_config_updated_at(db)

    await _audit(db, admin, action='bot_menu_reset', details={'rows_count': len(config.get('rows', []))})

    logger.info('Админ сбросил меню бота к дефолту', telegram_id=getattr(admin, 'telegram_id', None))

    return _serialize_config(config, settings.MENU_LAYOUT_ENABLED, updated_at)


@router.get('/builtin-buttons', response_model=BuiltinButtonsListResponse)
async def list_bot_menu_builtin_buttons(
    _admin: User = Depends(require_permission('bot_menu:read')),
) -> BuiltinButtonsListResponse:
    """Получить каталог встроенных кнопок меню бота."""
    items = [
        BuiltinButtonInfo(
            id=button['id'],
            text_key=button['text_key'],
            default_text=button['default_text'],
            callback_data=button['callback_data'],
            default_conditions=ButtonConditions(**button['default_conditions'])
            if button.get('default_conditions')
            else None,
            supports_dynamic_text=button.get('supports_dynamic_text', False),
            supports_direct_open=button.get('supports_direct_open', False),
        )
        for button in MenuLayoutService.get_builtin_buttons_info()
    ]

    return BuiltinButtonsListResponse(items=items, total=len(items))


@router.get('/available-callbacks', response_model=AvailableCallbacksResponse)
async def list_bot_menu_available_callbacks(
    _admin: User = Depends(require_permission('bot_menu:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AvailableCallbacksResponse:
    """Получить список доступных callback_data для кнопок меню бота."""
    callbacks = await MenuLayoutService.get_available_callbacks(db)

    items = [
        AvailableCallback(
            callback_data=callback['callback_data'],
            name=callback['name'],
            description=callback.get('description'),
            category=callback['category'],
            default_text=callback.get('default_text'),
            default_icon=callback.get('default_icon'),
            requires_subscription=callback.get('requires_subscription', False),
            is_in_menu=callback.get('is_in_menu', False),
        )
        for callback in callbacks
    ]

    return AvailableCallbacksResponse(
        items=items,
        total=len(items),
        categories=sorted({callback['category'] for callback in callbacks}),
    )


@router.get('/placeholders', response_model=DynamicPlaceholdersResponse)
async def list_bot_menu_placeholders(
    _admin: User = Depends(require_permission('bot_menu:read')),
) -> DynamicPlaceholdersResponse:
    """Получить список динамических плейсхолдеров для текста кнопок."""
    items = [
        DynamicPlaceholder(
            placeholder=placeholder['placeholder'],
            description=placeholder['description'],
            example=placeholder['example'],
            category=placeholder['category'],
        )
        for placeholder in MenuLayoutService.get_dynamic_placeholders()
    ]

    return DynamicPlaceholdersResponse(items=items, total=len(items))


@router.post('/preview', response_model=MenuPreviewResponse)
async def preview_bot_menu(
    payload: MenuPreviewRequest,
    _admin: User = Depends(require_permission('bot_menu:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MenuPreviewResponse:
    """Предпросмотр меню бота для указанного контекста пользователя."""
    context = MenuContext(
        language=payload.language,
        is_admin=payload.is_admin,
        is_moderator=payload.is_moderator,
        has_active_subscription=payload.has_active_subscription,
        subscription_is_active=payload.subscription_is_active,
        balance_kopeks=payload.balance_kopeks,
    )

    preview_rows = await MenuLayoutService.preview_keyboard(db, context)

    rows: list[MenuPreviewRow] = []
    total_buttons = 0
    for row_data in preview_rows:
        buttons = [
            MenuPreviewButton(text=button['text'], action=button['action'], type=button['type'])
            for button in row_data['buttons']
        ]
        total_buttons += len(buttons)
        rows.append(MenuPreviewRow(buttons=buttons))

    return MenuPreviewResponse(rows=rows, total_buttons=total_buttons)
