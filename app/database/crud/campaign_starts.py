import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AdvertisingCampaignStart


logger = structlog.get_logger(__name__)


async def record_campaign_start(
    db: AsyncSession,
    campaign_id: int,
    *,
    telegram_id: int | None = None,
    user_id: int | None = None,
    source: str,
) -> None:
    """Записывает сырое событие «старт» рекламной кампании (bot /start или визит кабинета).

    Fire-and-forget: любая ошибка вставки логируется и проглатывается, чтобы сбой
    записи телеметрии никогда не ломал /start или вход в кабинет. Сам факт коммитим
    здесь же — существующий юзер уходит из вызывающего кода раньше любого другого
    commit, поэтому событие обязано персиститься независимо.
    """
    try:
        db.add(
            AdvertisingCampaignStart(
                campaign_id=campaign_id,
                telegram_id=telegram_id,
                user_id=user_id,
                source=source,
            )
        )
        await db.commit()
    except Exception as exc:
        logger.warning(
            'Не удалось записать старт рекламной кампании',
            campaign_id=campaign_id,
            source=source,
            error=exc,
        )
        try:
            await db.rollback()
        except Exception:
            logger.exception('Не удалось откатить сессию после ошибки записи старта')


async def get_campaign_start_counts(
    db: AsyncSession,
    campaign_ids: list[int],
) -> dict[int, tuple[int, int]]:
    """Считает старты по кампаниям ОДНИМ сгруппированным запросом.

    Returns:
        {campaign_id: (total, unique)} — total это все события, unique это число
        уникальных «личностей» по telegram_id (или user_id, если tg отсутствует).
        Кампании без стартов в словарь не попадают — caller подставляет (0, 0).
    """
    if not campaign_ids:
        return {}

    # Ключ идентичности: telegram_id когда он есть, иначе -user_id. Пространства
    # не пересекаются (tg-id всегда > 0, -user_id всегда < 0), поэтому один и тот
    # же человек не задваивается, а разные — не схлопываются. NULL/NULL (аноним
    # без обоих) в COUNT(DISTINCT) не попадает → учитывается только в total.
    identity = func.coalesce(
        AdvertisingCampaignStart.telegram_id,
        -AdvertisingCampaignStart.user_id,
    )

    result = await db.execute(
        select(
            AdvertisingCampaignStart.campaign_id,
            func.count(AdvertisingCampaignStart.id),
            func.count(func.distinct(identity)),
        )
        .where(AdvertisingCampaignStart.campaign_id.in_(campaign_ids))
        .group_by(AdvertisingCampaignStart.campaign_id)
    )

    return {row[0]: (row[1] or 0, row[2] or 0) for row in result.all()}
