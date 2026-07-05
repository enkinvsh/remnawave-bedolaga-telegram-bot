"""Санити-тесты реестра lifecycle-правил (единственный источник дефолтов).

Проверяем инварианты реестра: ключи уникальны, группы валидны, форма config'а
платных ключей сохранена 1:1 с легаси NotificationSettingsService, дефолты
отдаются копией (мутировать безопасно).
"""

from app.services import lifecycle_rules as lr


def test_all_keys_are_unique():
    keys = lr.all_keys()
    assert len(keys) == len(set(keys))
    assert len(keys) == 8


def test_every_descriptor_has_valid_group_and_self_consistent_key():
    for key in lr.all_keys():
        descriptor = lr.get_descriptor(key)
        assert descriptor is not None
        assert descriptor.key == key
        assert descriptor.group in lr.GROUPS
        assert isinstance(descriptor.enabled, bool)
        assert isinstance(descriptor.config, dict)


def test_every_group_is_represented():
    represented = {lr.get_descriptor(key).group for key in lr.all_keys()}
    assert represented == set(lr.GROUPS)


def test_placeholders_are_documented_subset():
    for key in lr.all_keys():
        for placeholder in lr.get_descriptor(key).placeholders:
            assert placeholder in lr.PLACEHOLDERS


def test_paid_group_matches_legacy_service_keys():
    assert lr.keys_for_group('paid') == (
        'expired_1d',
        'expired_second_wave',
        'expired_third_wave',
        'trial_channel_unsubscribed',
    )


def test_legacy_config_shapes_are_preserved():
    # Форма config'а платных ключей ДОЛЖНА совпадать с прежними DEFAULTS
    # (enabled вынесен в отдельную колонку → в config его больше нет).
    assert lr.default_config('expired_1d') == {}
    assert lr.default_config('trial_channel_unsubscribed') == {}
    assert lr.default_config('expired_second_wave') == {'discount_percent': 10, 'valid_hours': 24}
    assert lr.default_config('expired_third_wave') == {
        'discount_percent': 20,
        'valid_hours': 24,
        'trigger_days': 5,
    }
    for key in lr.keys_for_group('paid'):
        assert lr.default_enabled(key) is True


def test_pre_trial_rule_carries_full_config():
    config = lr.default_config('trial_not_activated')
    assert config['first_offset_hours'] == 1
    assert config['repeat_hours'] == 48
    assert config['max_repeats'] == 3
    assert config['button'] == 'activate_trial'
    assert isinstance(config['message_template'], str) and config['message_template']
    assert isinstance(config['repeat_message_template'], str) and config['repeat_message_template']


def test_post_trial_ladder_has_five_ascending_steps():
    steps = lr.default_config('post_trial_ladder')['steps']
    assert len(steps) == 5
    percents = [step['discount_percent'] for step in steps]
    assert percents == [0, 5, 10, 15, 20]


def test_in_trial_rules_shapes():
    zero = lr.default_config('trial_zero_traffic')
    assert zero['offsets_hours'] == [1, 24]
    assert set(zero['message_templates']) == {'1', '24'}
    ending = lr.default_config('trial_ending')
    assert ending['hours_before'] == 2
    assert '{hours}' in ending['message_template']


def test_default_config_returns_isolated_copy():
    first = lr.default_config('expired_second_wave')
    first['discount_percent'] = 999
    # Реестр не должен мутироваться копией, отданной наружу.
    assert lr.default_config('expired_second_wave')['discount_percent'] == 10


def test_merged_config_overlays_override_without_touching_defaults():
    merged = lr.merged_config('expired_second_wave', {'discount_percent': 15})
    assert merged == {'discount_percent': 15, 'valid_hours': 24}
    assert lr.default_config('expired_second_wave')['discount_percent'] == 10
    # None override → чистый дефолт.
    assert lr.merged_config('expired_1d', None) == {}


def test_is_known_key():
    assert lr.is_known_key('expired_1d') is True
    assert lr.is_known_key('nope') is False


def test_unknown_key_helpers_are_safe():
    assert lr.get_descriptor('nope') is None
    assert lr.default_enabled('nope') is True
    assert lr.default_config('nope') == {}
