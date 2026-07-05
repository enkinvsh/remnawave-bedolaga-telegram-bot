"""Schemas for advertising campaigns management in cabinet."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CampaignBonusType = Literal['balance', 'subscription', 'none', 'tariff']


class TariffInfo(BaseModel):
    """Tariff info for campaign."""

    id: int
    name: str


class CampaignListItem(BaseModel):
    """Campaign item for list view."""

    id: int
    name: str
    start_parameter: str
    bonus_type: CampaignBonusType
    is_active: bool
    registrations_count: int
    total_revenue_kopeks: int = 0
    conversion_rate: float = 0.0
    partner_user_id: int | None = None
    partner_name: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CampaignListResponse(BaseModel):
    """Response with list of campaigns."""

    campaigns: list[CampaignListItem]
    total: int


class CampaignDetailResponse(BaseModel):
    """Detailed campaign response."""

    id: int
    name: str
    start_parameter: str
    bonus_type: CampaignBonusType
    is_active: bool
    # Balance bonus
    balance_bonus_kopeks: int = 0
    balance_bonus_rubles: float = 0.0
    # Subscription bonus
    subscription_duration_days: int | None = None
    subscription_traffic_gb: int | None = None
    subscription_device_limit: int | None = None
    subscription_squads: list[str] = Field(default_factory=list)
    # Tariff bonus
    tariff_id: int | None = None
    tariff_duration_days: int | None = None
    tariff: TariffInfo | None = None
    # Partner
    partner_user_id: int | None = None
    partner_name: str | None = None
    # Meta
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime | None = None
    # Deep link
    deep_link: str | None = None
    web_link: str | None = None

    model_config = ConfigDict(from_attributes=True)


class CampaignCreateRequest(BaseModel):
    """Request to create a campaign."""

    name: str = Field(..., min_length=1, max_length=255)
    start_parameter: str = Field(..., min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    bonus_type: CampaignBonusType
    is_active: bool = True
    # Balance bonus
    balance_bonus_kopeks: int = Field(0, ge=0)
    # Subscription bonus
    subscription_duration_days: int | None = Field(None, ge=1)
    subscription_traffic_gb: int | None = Field(None, ge=0)
    subscription_device_limit: int | None = Field(None, ge=1)
    subscription_squads: list[str] = Field(default_factory=list)
    # Tariff bonus
    tariff_id: int | None = None
    tariff_duration_days: int | None = Field(None, ge=1)
    # Partner
    partner_user_id: int | None = None


class CampaignUpdateRequest(BaseModel):
    """Request to update a campaign."""

    name: str | None = Field(None, min_length=1, max_length=255)
    start_parameter: str | None = Field(None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    bonus_type: CampaignBonusType | None = None
    is_active: bool | None = None
    # Balance bonus
    balance_bonus_kopeks: int | None = Field(None, ge=0)
    # Subscription bonus
    subscription_duration_days: int | None = Field(None, ge=1)
    subscription_traffic_gb: int | None = Field(None, ge=0)
    subscription_device_limit: int | None = Field(None, ge=1)
    subscription_squads: list[str] | None = None
    # Tariff bonus
    tariff_id: int | None = None
    tariff_duration_days: int | None = Field(None, ge=1)
    # Partner
    partner_user_id: int | None = None


BulkSkipReason = Literal['invalid_name', 'invalid_start_parameter', 'duplicate_existing', 'duplicate_in_batch']


class BulkCampaignItem(BaseModel):
    """One campaign label; loose by design — invalid items are skipped, not 422'd."""

    name: str
    start_parameter: str


class BulkCampaignDefaults(BaseModel):
    """Shared bonus config applied to every item in a bulk create."""

    bonus_type: CampaignBonusType = 'none'
    is_active: bool = True
    # Balance bonus
    balance_bonus_kopeks: int = Field(0, ge=0)
    # Subscription bonus
    subscription_duration_days: int | None = Field(None, ge=1)
    subscription_traffic_gb: int | None = Field(None, ge=0)
    subscription_device_limit: int | None = Field(None, ge=1)
    subscription_squads: list[str] = Field(default_factory=list)
    # Tariff bonus
    tariff_id: int | None = None
    tariff_duration_days: int | None = Field(None, ge=1)


class BulkCampaignCreateRequest(BaseModel):
    """Request to create up to 500 campaigns from a list of labels."""

    items: list[BulkCampaignItem] = Field(..., max_length=500)
    defaults: BulkCampaignDefaults = Field(default_factory=BulkCampaignDefaults)


class BulkCampaignSummary(BaseModel):
    """Compact summary of a campaign created in a bulk request."""

    id: int
    name: str
    start_parameter: str


class BulkCampaignSkippedItem(BaseModel):
    """A bulk item that was not created, with the reason why."""

    name: str
    start_parameter: str
    reason: BulkSkipReason


class BulkCampaignCreateResponse(BaseModel):
    """Result of a bulk campaign creation."""

    created: list[BulkCampaignSummary]
    skipped: list[BulkCampaignSkippedItem]
    created_count: int
    skipped_count: int


BulkDeleteSkipReason = Literal['not_found']


class BulkCampaignDeleteRequest(BaseModel):
    """Request to mass-delete campaigns, optionally as a non-destructive dry-run preview."""

    ids: list[int] = Field(..., min_length=1, max_length=500)
    dry_run: bool = False


class BulkCampaignDeletableItem(BaseModel):
    """A resolvable campaign that would be (dry-run) or was deleted, with its cascade impact."""

    id: int
    name: str
    start_parameter: str
    registrations: int
    starts: int


class BulkCampaignDeleteSkippedItem(BaseModel):
    """A requested id that did not resolve to a campaign."""

    id: int
    reason: BulkDeleteSkipReason


class BulkCampaignDeleteResponse(BaseModel):
    """Result of a bulk delete (dry-run preview or real deletion).

    On a dry-run `deleted_count` is None; on a real deletion it holds the number of
    campaigns removed (the FK CASCADE additionally removes their registrations/starts).
    """

    deletable: list[BulkCampaignDeletableItem]
    skipped: list[BulkCampaignDeleteSkippedItem]
    total_registrations: int
    total_starts: int
    deleted_count: int | None = None


class CampaignToggleResponse(BaseModel):
    """Response after toggling campaign."""

    id: int
    is_active: bool
    message: str


class CampaignStatisticsResponse(BaseModel):
    """Detailed campaign statistics."""

    id: int
    name: str
    start_parameter: str
    bonus_type: CampaignBonusType
    is_active: bool
    # Registration stats
    registrations: int = 0
    balance_issued_kopeks: int = 0
    balance_issued_rubles: float = 0.0
    subscription_issued: int = 0
    last_registration: datetime | None = None
    # Revenue stats
    total_revenue_kopeks: int = 0
    total_revenue_rubles: float = 0.0
    avg_revenue_per_user_kopeks: int = 0
    avg_revenue_per_user_rubles: float = 0.0
    avg_first_payment_kopeks: int = 0
    avg_first_payment_rubles: float = 0.0
    # Trial & Conversion stats
    trial_users_count: int = 0
    active_trials_count: int = 0
    conversion_count: int = 0
    paid_users_count: int = 0
    conversion_rate: float = 0.0
    trial_conversion_rate: float = 0.0
    # Deep link
    deep_link: str | None = None
    web_link: str | None = None


class CampaignRegistrationItem(BaseModel):
    """Campaign registration item."""

    id: int
    user_id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    bonus_type: str
    balance_bonus_kopeks: int = 0
    subscription_duration_days: int | None = None
    tariff_id: int | None = None
    tariff_duration_days: int | None = None
    created_at: datetime
    # User stats
    user_balance_kopeks: int = 0
    has_subscription: bool = False
    has_paid: bool = False

    model_config = ConfigDict(from_attributes=True)


class CampaignRegistrationsResponse(BaseModel):
    """Response with campaign registrations."""

    registrations: list[CampaignRegistrationItem]
    total: int
    page: int
    per_page: int


class CampaignsOverviewResponse(BaseModel):
    """Overview of all campaigns."""

    total: int
    active: int
    inactive: int
    total_registrations: int
    total_balance_issued_kopeks: int
    total_balance_issued_rubles: float
    total_subscription_issued: int
    total_tariff_issued: int = 0


class AvailablePartnerItem(BaseModel):
    """Partner item for campaign partner selector."""

    user_id: int
    username: str | None = None
    first_name: str | None = None


class ServerSquadInfo(BaseModel):
    """Server squad info for campaign selection."""

    id: int
    squad_uuid: str
    display_name: str
    country_code: str | None = None


# --- Admin campaign chart data schemas ---


class AdminDailyStatItem(BaseModel):
    """Daily stat item for admin campaign charts."""

    date: str
    referrals_count: int = 0  # actually registrations, named for frontend compat
    earnings_kopeks: int = 0  # actually revenue, named for frontend compat


class AdminPeriodStats(BaseModel):
    """Period stats for admin campaign comparison."""

    days: int
    referrals_count: int = 0
    earnings_kopeks: int = 0


class AdminPeriodChange(BaseModel):
    """Change metrics between periods."""

    absolute: int = 0
    percent: float = 0.0
    trend: str = 'stable'


class AdminPeriodComparison(BaseModel):
    """Comparison of current vs previous period."""

    current: AdminPeriodStats
    previous: AdminPeriodStats
    referrals_change: AdminPeriodChange
    earnings_change: AdminPeriodChange


class AdminTopRegistrationItem(BaseModel):
    """Top user by spending in a campaign."""

    id: int
    full_name: str
    created_at: datetime
    has_paid: bool = False
    is_active: bool = False
    total_earnings_kopeks: int = 0  # actually total spending, named for frontend compat


class AdminCampaignChartDataResponse(BaseModel):
    """Chart data for admin campaign stats page."""

    campaign_id: int
    total_deposits_kopeks: int = 0
    total_spending_kopeks: int = 0
    daily_stats: list[AdminDailyStatItem] = []
    period_comparison: AdminPeriodComparison
    top_registrations: list[AdminTopRegistrationItem] = []
