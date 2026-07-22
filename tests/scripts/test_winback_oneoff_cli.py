from app.services import winback_oneoff as campaign
from scripts.winback_oneoff import parse_args


def test_cohort_defaults_to_all() -> None:
    options = parse_args([])

    assert options.cohort is campaign.CohortSelection.ALL


def test_cohort_filter_is_parsed() -> None:
    options = parse_args(['--cohort', 'c'])

    assert options.cohort is campaign.CohortSelection.C
