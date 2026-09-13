"""f916.worthwhile -- a cheap, deterministic, never-raising ROI gate used by
f916/selfext.py to decide whether a gap's measured reward can recoup the
cost of the expensive tier-2 brain.generate_skill call, unless the gap
carries PRESTIGE (reputation / future value) worth pursuing regardless."""
from f916 import worthwhile
from f916.config import Settings


def make_settings(tmp_path, **overrides):
    return Settings(data_dir=tmp_path / "data", **overrides)


def gap(class_key="acme|novel class", funder="acme", sample_title="A brand new bounty class",
        paid_total_atomic=0):
    return {"class_key": class_key, "funder": funder, "sample_title": sample_title,
            "paid_total_atomic": paid_total_atomic}


# ---------------------------------------------------------------------------
# _usd
# ---------------------------------------------------------------------------
def test_usd_converts_atomic_to_dollars():
    assert worthwhile._usd(1_000_000) == 1.0
    assert worthwhile._usd("500000") == 0.5
    assert worthwhile._usd(0) == 0.0


def test_usd_returns_zero_on_bad_input_and_never_raises():
    assert worthwhile._usd(None) == 0.0
    assert worthwhile._usd("not-a-number") == 0.0
    assert worthwhile._usd([1, 2, 3]) == 0.0
    assert worthwhile._usd(object()) == 0.0


# ---------------------------------------------------------------------------
# has_prestige
# ---------------------------------------------------------------------------
def test_has_prestige_true_for_configured_funder_casefolded(tmp_path):
    settings = make_settings(tmp_path, worthwhile_prestige_funders=("ACME Foundation",))
    g = gap(funder="acme foundation")
    assert worthwhile.has_prestige(g, settings) is True


def test_has_prestige_true_for_configured_term_in_class_key_funder_or_title(tmp_path):
    settings = make_settings(tmp_path, worthwhile_prestige_terms=("grant",))
    assert worthwhile.has_prestige(gap(class_key="acme|grant renewal"), settings) is True
    assert worthwhile.has_prestige(gap(funder="grant foundation"), settings) is True
    assert worthwhile.has_prestige(gap(sample_title="Annual research grant"), settings) is True


def test_has_prestige_false_when_no_funder_or_term_matches(tmp_path):
    settings = make_settings(tmp_path, worthwhile_prestige_funders=("other-funder",),
                              worthwhile_prestige_terms=("charter",))
    assert worthwhile.has_prestige(gap(), settings) is False


def test_has_prestige_defaults_match_grant_and_official(tmp_path):
    settings = make_settings(tmp_path)  # default terms: grant,peer review,peer-review,official,charter
    assert worthwhile.has_prestige(gap(sample_title="Official audit of rail totals"), settings) is True
    assert worthwhile.has_prestige(gap(sample_title="A peer review process"), settings) is True
    assert worthwhile.has_prestige(gap(sample_title="Nothing special here"), settings) is False


def test_has_prestige_never_raises_on_malformed_gap_or_settings(tmp_path):
    settings = make_settings(tmp_path)
    assert worthwhile.has_prestige({}, settings) is False
    assert worthwhile.has_prestige(None, settings) is False
    assert worthwhile.has_prestige("not-a-dict", settings) is False

    class BrokenSettings:
        worthwhile_prestige_funders = None
        worthwhile_prestige_terms = None

    assert worthwhile.has_prestige(gap(), BrokenSettings()) is False


# ---------------------------------------------------------------------------
# assess
# ---------------------------------------------------------------------------
def test_assess_worth_when_reward_meets_or_exceeds_minimum(tmp_path):
    settings = make_settings(tmp_path, worthwhile_min_reward_usd=0.5)
    result = worthwhile.assess(gap(paid_total_atomic=500_000), settings)  # exactly $0.50
    assert result == {"worth": True, "reward_usd": 0.5, "prestige": False, "reason": "reward_ok"}


def test_assess_not_worth_below_minimum_with_no_prestige(tmp_path):
    settings = make_settings(tmp_path, worthwhile_min_reward_usd=0.5)
    result = worthwhile.assess(gap(paid_total_atomic=100_000), settings)  # $0.10
    assert result == {"worth": False, "reward_usd": 0.1, "prestige": False,
                       "reason": "below_min_reward_no_prestige"}


def test_assess_worth_when_prestige_funder_regardless_of_reward(tmp_path):
    settings = make_settings(tmp_path, worthwhile_min_reward_usd=5.0,
                              worthwhile_prestige_funders=("acme",))
    result = worthwhile.assess(gap(funder="acme", paid_total_atomic=0), settings)
    assert result == {"worth": True, "reward_usd": 0.0, "prestige": True, "reason": "prestige"}


def test_assess_worth_when_prestige_term_regardless_of_reward(tmp_path):
    settings = make_settings(tmp_path, worthwhile_min_reward_usd=5.0)
    result = worthwhile.assess(gap(sample_title="Official charter review", paid_total_atomic=0), settings)
    assert result["worth"] is True
    assert result["prestige"] is True
    assert result["reason"] == "prestige"


def test_assess_bad_atomic_yields_zero_reward_and_not_worth_without_prestige(tmp_path):
    settings = make_settings(tmp_path, worthwhile_min_reward_usd=0.5)
    result = worthwhile.assess(gap(paid_total_atomic="not-a-number"), settings)
    assert result["reward_usd"] == 0.0
    assert result["worth"] is False
    assert result["reason"] == "below_min_reward_no_prestige"


def test_assess_bad_atomic_still_worth_when_prestige(tmp_path):
    settings = make_settings(tmp_path, worthwhile_min_reward_usd=0.5,
                              worthwhile_prestige_funders=("acme",))
    result = worthwhile.assess(gap(funder="acme", paid_total_atomic="garbage"), settings)
    assert result["reward_usd"] == 0.0
    assert result["worth"] is True
    assert result["reason"] == "prestige"


def test_assess_never_raises_on_malformed_gap_or_settings(tmp_path):
    settings = make_settings(tmp_path)
    for bad_gap in (None, "not-a-dict", {}, 42, []):
        result = worthwhile.assess(bad_gap, settings)
        assert isinstance(result, dict)
        assert result["worth"] is False

    class BrokenSettings:
        pass

    result = worthwhile.assess(gap(), BrokenSettings())
    assert isinstance(result, dict)
