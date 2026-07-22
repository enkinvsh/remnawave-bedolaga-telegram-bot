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
        f'<div style="margin-top:28px;padding:26px 20px;background-color:{escape(fill_color)};'
        f'border:1px solid {escape(border_color)};border-radius:16px;text-align:center;">'
        f'<p style="margin:0;font-size:46px;line-height:1;font-weight:700;color:{escape(accent_color)};">'
        f'{settings.REFERRAL_COMMISSION_PERCENT}%</p>'
        f'<p style="margin:10px 0 0;color:{escape(text_color)};font-size:14px;line-height:1.5;">'
        f'с каждого пополнения приглашённого друга</p>'
        f'<p style="margin:6px 0 0;color:{escape(dim_color)};font-size:12px;line-height:1.5;">'
        f'+{_rubles(settings.REFERRAL_INVITER_BONUS_KOPEKS)} ₽ вам за первую оплату друга'
        f' &middot; +{_rubles(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)} ₽ другу</p>'
        f'<p style="margin:18px 0 0;"><a href="{escape(referral_url, quote=True)}" '
        f'style="color:{escape(accent_color)};font-size:14px;font-weight:600;text-decoration:none;">'
        'Пригласить друга &rarr;</a></p></div>'
    )
