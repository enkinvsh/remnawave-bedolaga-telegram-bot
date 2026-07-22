from html import escape

from app.config import settings


def _rubles(kopeks: int) -> str:
    return f'{kopeks / 100:g}'


def build_referral_block(text_color: str, dim_color: str, accent_color: str, border_color: str, fill_color: str) -> str:
    if not settings.REFERRAL_PROGRAM_ENABLED:
        return ''
    referral_url = (
        f'{settings.CABINET_URL.rstrip("/")}/referral'
        '?campaign=email_referral_block&utm_source=email&utm_medium=email&utm_campaign=email_referral_block'
    )
    return (
        f'<div style="margin-top:28px;padding:20px;background-color:{escape(fill_color)};'
        f'border:1px solid {escape(border_color)};border-radius:16px;">'
        f'<p style="margin:0 0 10px;color:{escape(dim_color)};font-size:11px;font-weight:600;'
        f'letter-spacing:1.2px;text-transform:uppercase;">Реферальная программа</p>'
        f'<p style="margin:0 0 6px;color:{escape(text_color)};font-size:15px;font-weight:600;line-height:1.4;">'
        f'Приглашай друзей — получай {settings.REFERRAL_COMMISSION_PERCENT}% с каждого их пополнения</p>'
        f'<p style="margin:0 0 14px;color:{escape(dim_color)};font-size:13px;line-height:1.5;">'
        f'+{_rubles(settings.REFERRAL_INVITER_BONUS_KOPEKS)} ₽ за первую оплату друга'
        f' &middot; +{_rubles(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)} ₽ бонус другу</p>'
        f'<a href="{escape(referral_url, quote=True)}" style="color:{escape(accent_color)};font-size:14px;'
        f'font-weight:600;text-decoration:none;">Открыть реферальную программу &rarr;</a></div>'
    )
