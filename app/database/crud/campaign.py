from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

import structlog
from sqlalchemy import ColumnElement, Row, Select, and_, case, delete, distinct, func, or_, select, union, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlalchemy.sql.expression import CTE

from app.database.crud.campaign_starts import get_campaign_start_counts
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    Subscription,
    SubscriptionConversion,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
)


logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CampaignAggregateStats:
    """Presentation-agnostic funnel aggregates for a single advertising campaign.

    Raw domain numbers only (kopeks, not rubles; no formatted link) so one payload
    can feed both the CSV export and the admin stats dashboard.

    `trial_activated` is BEST-EFFORT: the bot's traffic accounting is known to be
    inconsistent (the panel sometimes sees usage the bot does not), so this counts a
    trial user as activated on ANY traffic evidence — a trial subscription with
    ``traffic_used_gb > 0`` OR a user ``lifetime_used_traffic_bytes > 0`` — and may
    under- or over-count at the margins.
    """

    campaign_id: int
    name: str
    start_parameter: str
    bonus_type: str
    is_active: bool
    created_at: datetime | None
    updated_at: datetime | None
    starts_total: int
    starts_unique: int
    registrations: int
    trial_users: int
    trial_activated: int
    paying_users: int
    total_amount_kopeks: int
    #: The admin-list "paid users" numerator: ``max(|SUBSCRIPTION_PAYMENT ∪ SubscriptionConversion|,
    #: COUNT(has_had_paid_subscription))``. DIFFERENT from ``paying_users`` (real DEPOSIT payers) on
    #: purpose — both are already in production under different screens. Carried here so the export
    #: and the admin list read the exact same CTE and can never drift apart again.
    paid_users: int


class CampaignPageRow(TypedDict):
    """One row of the admin campaigns list, already reduced to display values.

    Key names mirror ``get_campaign_statistics`` exactly so the parity script can
    compare the two sources field-for-field without a translation table.
    """

    id: int
    name: str
    start_parameter: str
    bonus_type: str
    is_active: bool
    registrations: int
    total_revenue_kopeks: int
    paid_users_count: int
    conversion_rate: float
    partner_user_id: int | None
    partner_name: str | None
    created_at: datetime


def _real_deposit_predicates() -> list[ColumnElement[bool]]:
    """The one definition of "a real deposit" — completed, typed DEPOSIT, real payment method.

    Every place that sums campaign revenue MUST call this. Promo bonuses, wheel prizes and
    admin top-ups are excluded by ``REAL_PAYMENT_METHODS``; inlining the triple anywhere else
    is how the numbers drift.
    """
    return [
        Transaction.type == TransactionType.DEPOSIT.value,
        Transaction.is_completed.is_(True),
        Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
    ]


def _campaign_filters(
    *,
    include_inactive: bool = True,
    is_active: bool | None = None,
    search: str | None = None,
    partner_user_id: int | None = None,
) -> list[ColumnElement[bool]]:
    """Row-level filtering for the campaigns list — the SINGLE source of truth.

    The page query and the total-count query MUST both be built from this exact list,
    otherwise pagination desynchronises from the reported total.

    Status is tri-state via ``is_active`` (None = any, True = active, False = inactive).
    ``include_inactive`` is the older two-state flag and survives for clients that still
    send it. PRECEDENCE: an explicit ``is_active`` WINS and ``include_inactive`` is ignored;
    only when ``is_active`` is None does ``include_inactive`` apply. A client sending
    neither, or only the old flag, gets exactly the behaviour it got before ``is_active``
    existed — the old flag alone can never express "inactive only", which is why it cannot
    be the one that decides.

    If a future filter ever targets an AGGREGATE (``min_revenue``, "has registrations"),
    it does NOT belong here: ``total`` would then have to be recomputed from the
    aggregate-joined subquery instead of a plain ``count(AdvertisingCampaign.id)``.
    """
    campaign = AdvertisingCampaign
    criteria: list[ColumnElement[bool]] = []
    if is_active is not None:
        criteria.append(campaign.is_active.is_(is_active))
    elif not include_inactive:
        criteria.append(campaign.is_active.is_(True))
    if partner_user_id is not None:
        criteria.append(campaign.partner_user_id == partner_user_id)
    if search and search.strip():
        needle = f'%{search.strip()}%'
        criteria.append(or_(campaign.name.ilike(needle), campaign.start_parameter.ilike(needle)))
    return criteria


def _paid_users_cte(scope_campaign_ids: Select[tuple[int]]) -> CTE:
    """Per-campaign ``paid_users`` reproducing ``get_campaign_statistics`` EXACTLY.

    The original is ``max(len(subscription_payers ∪ conversions), count(has_had_paid_subscription))``.
    Here that is a UNION cte (UNION, never UNION ALL — it must de-duplicate, otherwise the
    LEFT JOIN fans out and the distinct count is still right but the plan is not) plus a
    two-way max.

    That max is a CASE rather than ``greatest()``: ``greatest`` is PostgreSQL-only, and the
    parity suite executes this exact expression against SQLite. The CASE is standard SQL,
    compiles identically on both, and costs nothing.

    Both joins are OUTER on purpose: an inner join on ``User`` or on the union would drop
    registrations whose user has neither signal and could only shrink the counters.
    """
    reg = AdvertisingCampaignRegistration

    scope_users = select(reg.user_id.label('user_id')).where(reg.campaign_id.in_(scope_campaign_ids)).distinct()
    scope_users_cte = scope_users.cte('scope_users')

    paid_union = union(
        select(Transaction.user_id.label('user_id')).where(
            Transaction.user_id.in_(select(scope_users_cte.c.user_id)),
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed.is_(True),
        ),
        select(SubscriptionConversion.user_id.label('user_id')).where(
            SubscriptionConversion.user_id.in_(select(scope_users_cte.c.user_id))
        ),
    ).cte('paid_union')

    union_payers = func.count(distinct(paid_union.c.user_id))
    flagged_payers = func.count(distinct(case((User.has_had_paid_subscription.is_(True), reg.user_id))))

    return (
        select(
            reg.campaign_id.label('campaign_id'),
            case((union_payers > flagged_payers, union_payers), else_=flagged_payers).label('paid_users'),
        )
        .select_from(reg)
        .outerjoin(User, User.id == reg.user_id)
        .outerjoin(paid_union, paid_union.c.user_id == reg.user_id)
        .where(reg.campaign_id.in_(scope_campaign_ids))
        .group_by(reg.campaign_id)
        .cte('paid')
    )


def _campaign_stat_ctes(filters: list[ColumnElement[bool]]) -> tuple[CTE, CTE, CTE]:
    """Build the (registrations, revenue, paid_users) CTEs for one filtered campaign scope.

    ``scope`` is a CTE rather than an inlined subquery because it is referenced three times;
    materialising it once keeps the filter (including the ILIKE search) from being re-evaluated
    per branch.
    """
    campaign = AdvertisingCampaign
    reg_model = AdvertisingCampaignRegistration

    scope = select(campaign.id.label('campaign_id')).where(*filters).cte('scope')
    scope_ids = select(scope.c.campaign_id)

    # count(R.id), NOT count(distinct user_id) — mirrors the original denominator verbatim.
    reg = (
        select(
            reg_model.campaign_id.label('campaign_id'),
            func.count(reg_model.id).label('registrations'),
        )
        .where(reg_model.campaign_id.in_(scope_ids))
        .group_by(reg_model.campaign_id)
        .cte('reg')
    )

    # Joining registrations -> transactions on user_id is fan-out-safe ONLY because
    # uq_campaign_user makes a registration unique per (campaign_id, user_id): every
    # transaction matches exactly one registration row inside a campaign group.
    rev = (
        select(
            reg_model.campaign_id.label('campaign_id'),
            func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('revenue'),
        )
        .select_from(reg_model)
        .join(Transaction, Transaction.user_id == reg_model.user_id)
        .where(reg_model.campaign_id.in_(scope_ids), *_real_deposit_predicates())
        .group_by(reg_model.campaign_id)
        .cte('rev')
    )

    return reg, rev, _paid_users_cte(scope_ids)


async def create_campaign(
    db: AsyncSession,
    *,
    name: str,
    start_parameter: str,
    bonus_type: str,
    created_by: int | None = None,
    balance_bonus_kopeks: int = 0,
    subscription_duration_days: int | None = None,
    subscription_traffic_gb: int | None = None,
    subscription_device_limit: int | None = None,
    subscription_squads: list[str] | None = None,
    # Поля для типа "tariff"
    tariff_id: int | None = None,
    tariff_duration_days: int | None = None,
    is_active: bool = True,
    partner_user_id: int | None = None,
) -> AdvertisingCampaign:
    campaign = AdvertisingCampaign(
        name=name,
        start_parameter=start_parameter,
        bonus_type=bonus_type,
        balance_bonus_kopeks=balance_bonus_kopeks or 0,
        subscription_duration_days=subscription_duration_days,
        subscription_traffic_gb=subscription_traffic_gb,
        subscription_device_limit=subscription_device_limit,
        subscription_squads=subscription_squads or [],
        tariff_id=tariff_id,
        tariff_duration_days=tariff_duration_days,
        created_by=created_by,
        is_active=is_active,
        partner_user_id=partner_user_id,
    )

    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    logger.info(
        '📣 Создана рекламная кампания',
        campaign_name=campaign.name,
        start_parameter=campaign.start_parameter,
        bonus_type=campaign.bonus_type,
    )
    return campaign


async def get_campaign_by_id(db: AsyncSession, campaign_id: int) -> AdvertisingCampaign | None:
    result = await db.execute(
        select(AdvertisingCampaign)
        .options(
            selectinload(AdvertisingCampaign.registrations),
            selectinload(AdvertisingCampaign.tariff),
            selectinload(AdvertisingCampaign.partner),
        )
        .where(AdvertisingCampaign.id == campaign_id)
    )
    return result.scalar_one_or_none()


async def get_campaign_by_start_parameter(
    db: AsyncSession,
    start_parameter: str,
    *,
    only_active: bool = False,
) -> AdvertisingCampaign | None:
    stmt = select(AdvertisingCampaign).where(AdvertisingCampaign.start_parameter == start_parameter)
    if only_active:
        stmt = stmt.where(AdvertisingCampaign.is_active.is_(True))

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_campaigns_list(
    db: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 20,
    include_inactive: bool = True,
    with_registrations: bool = False,
) -> list[AdvertisingCampaign]:
    """List campaign rows.

    ``with_registrations`` eagerly loads EVERY registration row of every returned
    campaign — opt in only if the caller actually reads ``campaign.registrations``
    (the bot's admin list does, to sum bonuses). Callers that only need campaign
    columns must leave it off.
    """
    options = [
        selectinload(AdvertisingCampaign.tariff),
        selectinload(AdvertisingCampaign.partner),
    ]
    if with_registrations:
        options.append(selectinload(AdvertisingCampaign.registrations))

    stmt = (
        select(AdvertisingCampaign)
        .options(*options)
        .order_by(AdvertisingCampaign.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if not include_inactive:
        stmt = stmt.where(AdvertisingCampaign.is_active.is_(True))

    result = await db.execute(stmt)
    return result.scalars().all()


async def get_campaigns_page_with_stats(
    db: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 50,
    include_inactive: bool = True,
    is_active: bool | None = None,
    search: str | None = None,
    partner_user_id: int | None = None,
    sort_by: str = 'created_at',
    sort_dir: str = 'desc',
) -> tuple[list[CampaignPageRow], int]:
    """One page of campaigns WITH their funnel stats, in exactly two round-trips.

    Replaces the per-campaign ``get_campaign_statistics`` loop. The displayed numbers are
    byte-identical to that function by construction: the same denominator (``count(R.id)``),
    the same deposit predicates, the same ``max(|A ∪ B|, C)`` numerator, and the same
    Python-side rounding.
    """
    campaign = AdvertisingCampaign
    filters = _campaign_filters(
        include_inactive=include_inactive,
        is_active=is_active,
        search=search,
        partner_user_id=partner_user_id,
    )
    reg, rev, paid = _campaign_stat_ctes(filters)

    # COALESCE is not cosmetic: a LEFT JOIN miss yields NULL, and PostgreSQL sorts NULLS FIRST
    # on DESC, which would float every zero-activity campaign above the best-performing ones.
    registrations_col = func.coalesce(reg.c.registrations, 0)
    revenue_col = func.coalesce(rev.c.revenue, 0)
    paid_col = func.coalesce(paid.c.paid_users, 0)
    # ORDER BY key ONLY. The displayed rate is computed in Python below — PostgreSQL's
    # round(numeric, 1) is half-away-from-zero while Python's round() is banker's rounding,
    # so paid=1 / count=16 renders 6.3 in SQL but 6.2 in Python.
    conversion_key = case((registrations_col > 0, paid_col * 100.0 / registrations_col), else_=0.0)

    sort_map = {
        'created_at': campaign.created_at,
        'name': campaign.name,
        'registrations': registrations_col,
        'revenue': revenue_col,
        'conversion': conversion_key,
    }
    sort_column = sort_map.get(sort_by, campaign.created_at)
    ordering = sort_column.asc() if sort_dir == 'asc' else sort_column.desc()

    partner = aliased(User)
    page_stmt = (
        select(
            campaign.id,
            campaign.name,
            campaign.start_parameter,
            campaign.bonus_type,
            campaign.is_active,
            campaign.partner_user_id,
            campaign.created_at,
            registrations_col,
            revenue_col,
            paid_col,
            partner.id,
            partner.first_name,
            partner.username,
        )
        .select_from(campaign)
        .outerjoin(reg, reg.c.campaign_id == campaign.id)
        .outerjoin(rev, rev.c.campaign_id == campaign.id)
        .outerjoin(paid, paid.c.campaign_id == campaign.id)
        .outerjoin(partner, partner.id == campaign.partner_user_id)
        .where(*filters)
        # The id tiebreaker is mandatory, not defensive: most campaigns have 0 revenue and 0
        # registrations, so ties are the norm, and an unstable sort makes rows duplicate or
        # vanish between pages.
        .order_by(ordering, campaign.id.desc())
        .offset(offset)
        .limit(limit)
    )

    rows = (await db.execute(page_stmt)).all()
    # Same `filters` list as the page query — see _campaign_filters.
    total = (await db.execute(select(func.count(campaign.id)).where(*filters))).scalar_one() or 0

    return [_build_campaign_page_row(row) for row in rows], total


def _build_campaign_page_row(row: Row[tuple[object, ...]]) -> CampaignPageRow:
    (
        campaign_id,
        name,
        start_parameter,
        bonus_type,
        is_active,
        partner_user_id,
        created_at,
        registrations,
        revenue,
        paid_users,
        partner_id,
        partner_first_name,
        partner_username,
    ) = row

    registrations = int(registrations)
    paid_users = int(paid_users)

    return CampaignPageRow(
        id=int(campaign_id),
        name=str(name),
        start_parameter=str(start_parameter),
        bonus_type=str(bonus_type),
        is_active=bool(is_active),
        registrations=registrations,
        # sum() over bigint returns numeric in PostgreSQL, i.e. Decimal in the driver.
        total_revenue_kopeks=int(revenue),
        paid_users_count=paid_users,
        conversion_rate=round((paid_users / registrations) * 100, 1) if registrations else 0.0,
        partner_user_id=partner_user_id,
        # Not COALESCE in SQL: Python treats an empty first_name as falsy and falls through
        # to username, while SQL COALESCE would return the empty string.
        partner_name=_partner_display_name(partner_id, partner_first_name, partner_username),
        created_at=created_at,
    )


def _partner_display_name(partner_id: int | None, first_name: str | None, username: str | None) -> str | None:
    if partner_id is None:
        return None
    return first_name or username or f'#{partner_id}'


async def get_campaigns_count(db: AsyncSession, *, is_active: bool | None = None) -> int:
    stmt = select(func.count(AdvertisingCampaign.id))
    if is_active is not None:
        stmt = stmt.where(AdvertisingCampaign.is_active.is_(is_active))

    result = await db.execute(stmt)
    return result.scalar_one() or 0


async def update_campaign(
    db: AsyncSession,
    campaign: AdvertisingCampaign,
    **kwargs,
) -> AdvertisingCampaign:
    allowed_fields = {
        'name',
        'start_parameter',
        'bonus_type',
        'balance_bonus_kopeks',
        'subscription_duration_days',
        'subscription_traffic_gb',
        'subscription_device_limit',
        'subscription_squads',
        'tariff_id',
        'tariff_duration_days',
        'is_active',
        'partner_user_id',
    }

    nullable_fields = {
        'partner_user_id',
        'tariff_id',
        'subscription_duration_days',
        'subscription_traffic_gb',
        'subscription_device_limit',
        'tariff_duration_days',
    }

    update_data = {}
    for key, value in kwargs.items():
        if key not in allowed_fields:
            continue
        if value is None and key not in nullable_fields:
            continue
        update_data[key] = value

    if not update_data:
        return campaign

    update_data['updated_at'] = datetime.now(UTC)

    await db.execute(update(AdvertisingCampaign).where(AdvertisingCampaign.id == campaign.id).values(**update_data))
    await db.commit()
    await db.refresh(campaign)

    logger.info('✏️ Обновлена рекламная кампания', campaign_name=campaign.name, update_data=update_data)
    return campaign


async def delete_campaign(db: AsyncSession, campaign: AdvertisingCampaign) -> bool:
    await db.execute(delete(AdvertisingCampaign).where(AdvertisingCampaign.id == campaign.id))
    await db.commit()
    logger.info('🗑️ Удалена рекламная кампания', campaign_name=campaign.name)
    return True


async def get_campaign_registration_by_user(
    db: AsyncSession,
    user_id: int,
) -> AdvertisingCampaignRegistration | None:
    result = await db.execute(
        select(AdvertisingCampaignRegistration)
        .options(selectinload(AdvertisingCampaignRegistration.campaign))
        .where(AdvertisingCampaignRegistration.user_id == user_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def record_campaign_registration(
    db: AsyncSession,
    *,
    campaign_id: int,
    user_id: int,
    bonus_type: str,
    balance_bonus_kopeks: int = 0,
    subscription_duration_days: int | None = None,
    tariff_id: int | None = None,
    tariff_duration_days: int | None = None,
) -> tuple[AdvertisingCampaignRegistration, bool]:
    """Создаёт или возвращает запись регистрации в рекламной кампании.

    Returns:
        (registration, created): второе поле True если запись была создана прямо сейчас,
        False — если уже существовала (или другая параллельная транзакция её только что
        создала). Caller использует флаг чтобы понять, нужно ли отправить уведомление
        в админ-чат (один раз на первую успешную регистрацию).

    Race-safe: если между select() и commit() параллельная транзакция вставила
    запись с тем же (campaign_id, user_id), мы поймаем IntegrityError на UNIQUE
    constraint, откатим savepoint и вернём существующую запись с created=False.
    Это критично для паритета «1 message в чате == 1 row в БД» и для того, чтобы
    балансный бонус не начислился дважды при concurrent /start от одного юзера.
    """
    # Используем savepoint, чтобы IntegrityError не убил внешнюю транзакцию
    # caller'а, а только нашу попытку INSERT.
    async with db.begin_nested() as nested:
        existing = await db.execute(
            select(AdvertisingCampaignRegistration).where(
                and_(
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                    AdvertisingCampaignRegistration.user_id == user_id,
                )
            )
        )
        registration = existing.scalar_one_or_none()
        if registration:
            return registration, False

        registration = AdvertisingCampaignRegistration(
            campaign_id=campaign_id,
            user_id=user_id,
            bonus_type=bonus_type,
            balance_bonus_kopeks=balance_bonus_kopeks or 0,
            subscription_duration_days=subscription_duration_days,
            tariff_id=tariff_id,
            tariff_duration_days=tariff_duration_days,
        )
        db.add(registration)
        try:
            await db.flush()
        except IntegrityError:
            # Параллельный INSERT успел раньше — откатываем savepoint и читаем
            # существующую запись.
            await nested.rollback()
            existing = await db.execute(
                select(AdvertisingCampaignRegistration).where(
                    and_(
                        AdvertisingCampaignRegistration.campaign_id == campaign_id,
                        AdvertisingCampaignRegistration.user_id == user_id,
                    )
                )
            )
            registration = existing.scalar_one_or_none()
            if registration is None:
                # Не должно случиться при UNIQUE constraint, но если констрейнта
                # физически нет (legacy таблица без uq_campaign_user) — re-raise.
                raise
            return registration, False

    await db.commit()
    await db.refresh(registration)

    logger.info('📈 Регистрируем пользователя в кампании', user_id=user_id, campaign_id=campaign_id)
    return registration, True


async def get_campaign_statistics(
    db: AsyncSession,
    campaign_id: int,
) -> dict[str, int | None]:
    registrations_query = select(AdvertisingCampaignRegistration.user_id).where(
        AdvertisingCampaignRegistration.campaign_id == campaign_id
    )
    registrations_subquery = registrations_query.subquery()

    result = await db.execute(
        select(
            func.count(AdvertisingCampaignRegistration.id),
            func.coalesce(func.sum(AdvertisingCampaignRegistration.balance_bonus_kopeks), 0),
            func.max(AdvertisingCampaignRegistration.created_at),
        ).where(AdvertisingCampaignRegistration.campaign_id == campaign_id)
    )
    count, total_balance, last_registration = result.one()
    count = count or 0
    total_balance = total_balance or 0

    subscription_count_result = await db.execute(
        select(func.count(AdvertisingCampaignRegistration.id)).where(
            and_(
                AdvertisingCampaignRegistration.campaign_id == campaign_id,
                AdvertisingCampaignRegistration.bonus_type == 'subscription',
            )
        )
    )
    subscription_bonuses_issued = subscription_count_result.scalar() or 0

    # Only count real deposits (exclude promo bonuses, wheel prizes, admin top-ups)
    deposits_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            Transaction.user_id.in_(select(registrations_subquery.c.user_id)),
            *_real_deposit_predicates(),
        )
    )
    deposits_total = deposits_result.scalar() or 0

    trials_result = await db.execute(
        select(func.count(func.distinct(Subscription.user_id))).where(
            Subscription.user_id.in_(select(registrations_subquery.c.user_id)),
            Subscription.is_trial.is_(True),
        )
    )
    trial_users_count = trials_result.scalar() or 0

    active_trials_result = await db.execute(
        select(func.count(func.distinct(Subscription.user_id))).where(
            Subscription.user_id.in_(select(registrations_subquery.c.user_id)),
            Subscription.is_trial.is_(True),
            Subscription.status == SubscriptionStatus.ACTIVE.value,
        )
    )
    active_trials_count = active_trials_result.scalar() or 0

    conversions_result = await db.execute(
        select(func.count(func.distinct(SubscriptionConversion.user_id))).where(
            SubscriptionConversion.user_id.in_(select(registrations_subquery.c.user_id))
        )
    )
    conversion_count = conversions_result.scalar() or 0

    paid_users_result = await db.execute(
        select(func.count(User.id)).where(
            User.id.in_(select(registrations_subquery.c.user_id)),
            User.has_had_paid_subscription.is_(True),
        )
    )
    paid_users_from_flag = paid_users_result.scalar() or 0

    conversions_rows = await db.execute(
        select(
            SubscriptionConversion.user_id,
            SubscriptionConversion.first_payment_amount_kopeks,
            SubscriptionConversion.converted_at,
        )
        .where(SubscriptionConversion.user_id.in_(select(registrations_subquery.c.user_id)))
        .order_by(SubscriptionConversion.converted_at)
    )
    conversion_entries = conversions_rows.all()

    subscription_payments_rows = await db.execute(
        select(
            Transaction.user_id,
            Transaction.amount_kopeks,
            Transaction.created_at,
        )
        .where(
            Transaction.user_id.in_(select(registrations_subquery.c.user_id)),
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed.is_(True),
        )
        .order_by(Transaction.user_id, Transaction.created_at)
    )
    subscription_payments = subscription_payments_rows.all()

    subscription_payments_total = 0
    paid_users_from_transactions = set()
    conversion_user_ids = set()
    first_payment_amount_by_user: dict[int, int] = {}
    first_payment_time_by_user: dict[int, datetime | None] = {}

    for user_id, amount_kopeks, converted_at in conversion_entries:
        conversion_user_ids.add(user_id)
        amount_value = int(amount_kopeks or 0)
        first_payment_amount_by_user[user_id] = amount_value
        first_payment_time_by_user[user_id] = converted_at

    for user_id, amount_kopeks, created_at in subscription_payments:
        amount_value = abs(int(amount_kopeks or 0))
        subscription_payments_total += amount_value
        paid_users_from_transactions.add(user_id)

        if user_id not in first_payment_amount_by_user:
            first_payment_amount_by_user[user_id] = amount_value
            first_payment_time_by_user[user_id] = created_at
        else:
            existing_time = first_payment_time_by_user.get(user_id)
            if (existing_time is None and created_at is not None) or (
                existing_time is not None and created_at is not None and created_at < existing_time
            ):
                first_payment_amount_by_user[user_id] = amount_value
                first_payment_time_by_user[user_id] = created_at

    # Revenue = only real deposits (exclude bonus-funded subscription spending)
    total_revenue = deposits_total

    paid_user_ids = set(paid_users_from_transactions)
    paid_user_ids.update(conversion_user_ids)
    paid_users_count = max(len(paid_user_ids), paid_users_from_flag)

    conversion_count = conversion_count or len(paid_user_ids)
    conversion_count = max(conversion_count, len(paid_user_ids))

    avg_first_payment = 0
    if first_payment_amount_by_user:
        avg_first_payment = int(sum(first_payment_amount_by_user.values()) / len(first_payment_amount_by_user))

    conversion_rate = 0.0
    if count:
        conversion_rate = round((paid_users_count / count) * 100, 1)

    trial_conversion_rate = 0.0
    if trial_users_count:
        trial_conversion_rate = round((conversion_count / trial_users_count) * 100, 1)

    avg_revenue_per_user = 0
    if count:
        avg_revenue_per_user = int(total_revenue / count)

    return {
        'registrations': count,
        'balance_issued': total_balance,
        'subscription_issued': subscription_bonuses_issued,
        'last_registration': last_registration,
        'total_revenue_kopeks': total_revenue,
        'trial_users_count': trial_users_count,
        'active_trials_count': active_trials_count,
        'conversion_count': conversion_count,
        'paid_users_count': paid_users_count,
        'conversion_rate': conversion_rate,
        'trial_conversion_rate': trial_conversion_rate,
        'avg_revenue_per_user_kopeks': avg_revenue_per_user,
        'avg_first_payment_kopeks': avg_first_payment,
    }


async def get_campaigns_aggregate_stats(
    db: AsyncSession,
    campaign_ids: list[int] | None = None,
) -> list[CampaignAggregateStats]:
    """Compute per-campaign funnel aggregates in a FIXED number of grouped queries.

    Args:
        campaign_ids: restrict to these campaigns; ``None`` means every campaign
            (active AND inactive). An empty list yields an empty result.

    Never loops per-campaign: starts, registrations, the trial funnel and
    paying/revenue are each ONE ``GROUP BY`` query over the whole id set, then
    stitched onto the campaign rows with ``(0, 0, ...)`` fallbacks so a campaign
    with zero activity still produces a complete row. Reused by the CSV export and
    the stats dashboard, hence the presentation-agnostic return type.
    """
    campaigns_stmt = select(AdvertisingCampaign)
    if campaign_ids is not None:
        campaigns_stmt = campaigns_stmt.where(AdvertisingCampaign.id.in_(campaign_ids))
    campaigns_stmt = campaigns_stmt.order_by(AdvertisingCampaign.created_at.desc())

    campaigns = (await db.execute(campaigns_stmt)).scalars().all()
    if not campaigns:
        return []

    ids = [campaign.id for campaign in campaigns]

    # Reuse the coalesce(telegram_id, -user_id) identity logic owned by the starts table.
    start_counts = await get_campaign_start_counts(db, ids)

    registrations_rows = await db.execute(
        select(
            AdvertisingCampaignRegistration.campaign_id,
            func.count(AdvertisingCampaignRegistration.id),
        )
        .where(AdvertisingCampaignRegistration.campaign_id.in_(ids))
        .group_by(AdvertisingCampaignRegistration.campaign_id)
    )
    registrations_by_campaign = {row[0]: row[1] or 0 for row in registrations_rows.all()}

    # INNER JOIN User cannot drop trial_users rows (registration.user_id is a FK); it
    # exists only to expose lifetime_used_traffic_bytes for the activation test.
    activated = or_(
        Subscription.traffic_used_gb > 0,
        User.lifetime_used_traffic_bytes > 0,
    )
    trial_rows = await db.execute(
        select(
            AdvertisingCampaignRegistration.campaign_id,
            func.count(func.distinct(AdvertisingCampaignRegistration.user_id)),
            func.count(func.distinct(case((activated, AdvertisingCampaignRegistration.user_id)))),
        )
        .select_from(AdvertisingCampaignRegistration)
        .join(Subscription, Subscription.user_id == AdvertisingCampaignRegistration.user_id)
        .join(User, User.id == AdvertisingCampaignRegistration.user_id)
        .where(
            AdvertisingCampaignRegistration.campaign_id.in_(ids),
            Subscription.is_trial.is_(True),
        )
        .group_by(AdvertisingCampaignRegistration.campaign_id)
    )
    trial_by_campaign = {row[0]: (row[1] or 0, row[2] or 0) for row in trial_rows.all()}

    # Deposit semantics mirror get_campaign_statistics; duplicated on purpose to leave
    # that function untouched for its other consumers.
    paying_rows = await db.execute(
        select(
            AdvertisingCampaignRegistration.campaign_id,
            func.count(func.distinct(Transaction.user_id)),
            func.coalesce(func.sum(Transaction.amount_kopeks), 0),
        )
        .select_from(AdvertisingCampaignRegistration)
        .join(Transaction, Transaction.user_id == AdvertisingCampaignRegistration.user_id)
        .where(
            AdvertisingCampaignRegistration.campaign_id.in_(ids),
            *_real_deposit_predicates(),
        )
        .group_by(AdvertisingCampaignRegistration.campaign_id)
    )
    paying_by_campaign = {row[0]: (row[1] or 0, int(row[2] or 0)) for row in paying_rows.all()}

    # Same CTE the admin list uses, so the two screens cannot report different paid_users.
    paid_cte = _paid_users_cte(select(AdvertisingCampaign.id).where(AdvertisingCampaign.id.in_(ids)))
    paid_rows = await db.execute(select(paid_cte.c.campaign_id, paid_cte.c.paid_users))
    paid_by_campaign = {row[0]: int(row[1] or 0) for row in paid_rows.all()}

    stats: list[CampaignAggregateStats] = []
    for campaign in campaigns:
        starts_total, starts_unique = start_counts.get(campaign.id, (0, 0))
        trial_users, trial_activated = trial_by_campaign.get(campaign.id, (0, 0))
        paying_users, total_amount_kopeks = paying_by_campaign.get(campaign.id, (0, 0))
        stats.append(
            CampaignAggregateStats(
                campaign_id=campaign.id,
                name=campaign.name,
                start_parameter=campaign.start_parameter,
                bonus_type=campaign.bonus_type,
                is_active=campaign.is_active,
                created_at=campaign.created_at,
                updated_at=campaign.updated_at,
                starts_total=starts_total,
                starts_unique=starts_unique,
                registrations=registrations_by_campaign.get(campaign.id, 0),
                trial_users=trial_users,
                trial_activated=trial_activated,
                paying_users=paying_users,
                total_amount_kopeks=total_amount_kopeks,
                paid_users=paid_by_campaign.get(campaign.id, 0),
            )
        )
    return stats


async def get_campaigns_overview(db: AsyncSession) -> dict[str, int]:
    total = await get_campaigns_count(db)
    active = await get_campaigns_count(db, is_active=True)
    inactive = await get_campaigns_count(db, is_active=False)

    registrations_result = await db.execute(select(func.count(AdvertisingCampaignRegistration.id)))

    balance_result = await db.execute(
        select(func.coalesce(func.sum(AdvertisingCampaignRegistration.balance_bonus_kopeks), 0))
    )

    subscription_result = await db.execute(
        select(func.count(AdvertisingCampaignRegistration.id)).where(
            AdvertisingCampaignRegistration.bonus_type == 'subscription'
        )
    )

    return {
        'total': total,
        'active': active,
        'inactive': inactive,
        'registrations': registrations_result.scalar() or 0,
        'balance_total': balance_result.scalar() or 0,
        'subscription_total': subscription_result.scalar() or 0,
    }
