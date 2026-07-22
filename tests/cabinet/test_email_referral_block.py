import pytest

from app.cabinet.services.email_referral_block import build_referral_block
from app.config import settings


@pytest.mark.parametrize('commission_percent', [7, 31])
def test_referral_block_uses_settings_values(monkeypatch: pytest.MonkeyPatch, commission_percent: int) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', commission_percent)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 12_300)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 4_500)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')

    block = build_referral_block('#fff', '#999', '#06f', '#023', '#161616')

    assert f'{commission_percent}%' in block
    assert '123 ₽' in block
    assert '45 ₽' in block
    assert 'https://cabinet.example.com/referral' in block


def test_referral_block_absent_when_program_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)

    assert build_referral_block('#fff', '#999', '#06f', '#023', '#161616') == ''
