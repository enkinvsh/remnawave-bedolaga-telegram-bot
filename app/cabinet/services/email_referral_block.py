from html import escape

from app.config import settings


def _rubles(kopeks: int) -> str:
    return f'{kopeks / 100:g}'


def build_referral_block(text_color: str, dim_color: str, accent_color: str) -> str:
    if not settings.REFERRAL_PROGRAM_ENABLED:
        return ''
    referral_url = f'{settings.CABINET_URL.rstrip("/")}/referral'
    return (
        f'<div style="margin-top:24px;padding:18px;border:1px solid {escape(accent_color)};border-radius:14px;">'
        f'<p style="margin:0 0 8px;color:{escape(text_color)};font-weight:700;">'
        f'Приглашай друзей — получай {settings.REFERRAL_COMMISSION_PERCENT}% с каждого их пополнения</p>'
        f'<p style="margin:0 0 10px;color:{escape(dim_color)};font-size:13px;line-height:1.5;">'
        f'+{_rubles(settings.REFERRAL_INVITER_BONUS_KOPEKS)} ₽ за первую оплату друга и '
        f'+{_rubles(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)} ₽ бонус другу.</p>'
        f'<a href="{escape(referral_url, quote=True)}" style="color:{escape(accent_color)};font-weight:600;">'
        'Открыть реферальную программу</a></div>'
    )
