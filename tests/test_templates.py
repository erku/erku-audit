import hashlib
import json

import pytest

from f916 import skills
from f916.opportunities import evaluate_opportunity
from f916.templates import BUILDERS, load_templates, match_template, rail_self_report


TEMPLATES = [
    {"id": "rail-state-self-report", "skill": "rail-report", "builder": "rail_self_report",
     "title_contains": ["stranger"]},
    {"id": "award-slot-census", "skill": "rail-report", "builder": "rail_self_report",
     "title_contains": ["award", "census"]},
    {"id": "funder-scoped", "skill": "rail-report", "builder": "rail_self_report",
     "funder": "acme", "title_contains": ["rail"]},
]


def listing(**updates):
    base = {"listing_id": 41, "title": "Rail-state stranger check", "condition": "Re-check the public listing state."}
    return base | updates


# --- load_templates -------------------------------------------------------

def test_load_templates_reads_the_real_seed_file():
    templates = load_templates()
    ids = {t["id"] for t in templates}
    assert {"rail-state-self-report", "award-slot-census"} <= ids
    for t in templates:
        assert t["skill"] == "rail-report"
        assert t["builder"] in BUILDERS


def test_load_templates_never_raises_on_bad_path_or_content(tmp_path):
    assert load_templates(tmp_path / "does-not-exist.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_templates(bad) == []
    not_a_list = tmp_path / "obj.json"
    not_a_list.write_text("{}", encoding="utf-8")
    assert load_templates(not_a_list) == []


# --- match_template ---------------------------------------------------------

def test_match_template_requires_all_substrings_and_returns_first_match():
    assert match_template(listing(title="Award Slot Census for Q3"), TEMPLATES)["id"] == "award-slot-census"
    # Only one of the two required substrings present -> no match.
    assert match_template(listing(title="Award slot only"), TEMPLATES) is None


def test_match_template_enforces_exact_funder_when_present():
    matched = match_template(listing(title="Rail report", funder="acme"), TEMPLATES)
    assert matched["id"] == "funder-scoped"
    assert match_template(listing(title="Rail report", funder="other"), TEMPLATES) is None


def test_match_template_no_match_returns_none():
    assert match_template(listing(title="Totally unrelated bounty"), TEMPLATES) is None


def test_match_template_tolerates_malformed_input():
    assert match_template(None, TEMPLATES) is None
    assert match_template({}, TEMPLATES) is None
    assert match_template(listing(), "not-a-list") is None
    assert match_template(listing(), [{"skill": "rail-report"}, None, "garbage"]) is None


# --- rail_self_report --------------------------------------------------------

def test_rail_self_report_bid_on_escrow_with_capacity():
    target = rail_self_report(listing(escrow_address="0xesc", economics={"available_award_capacity": 2}))
    assert target["listing_id"] == 41
    assert target["verdict"] == "BID"
    assert target["quoted"]["available_award_capacity"] == 2
    assert "escrow_address" not in target["quoted"]  # only the enumerated quoted fields are copied


def test_rail_self_report_caution_on_unsettled_promise():
    target = rail_self_report(listing(funding_mode="promise",
                                       economics={"available_award_capacity": 1, "outstanding_awarded_atomic": "0"}))
    assert target["verdict"] == "CAUTION"
    assert target["quoted"]["funding_mode"] == "promise"
    assert target["quoted"]["outstanding_awarded_atomic"] == "0"


def test_rail_self_report_skip_on_withdrawn_expired_or_zero_capacity():
    withdrawn = rail_self_report(listing(withdrawn_at=1234567890, escrow_address="0xesc",
                                          economics={"available_award_capacity": 5}))
    assert withdrawn["verdict"] == "SKIP"

    expired = rail_self_report(listing(expired=True, escrow_address="0xesc",
                                        economics={"available_award_capacity": 5}))
    assert expired["verdict"] == "SKIP"

    zero_capacity = rail_self_report(listing(escrow_address="0xesc",
                                              economics={"available_award_capacity": 0}))
    assert zero_capacity["verdict"] == "SKIP"

    no_evidence = rail_self_report(listing(economics={"available_award_capacity": 3}))
    assert no_evidence["verdict"] == "SKIP"


def test_rail_self_report_quoted_only_includes_present_fields():
    target = rail_self_report(listing())
    assert target["quoted"] == {}
    target = rail_self_report(listing(state="open", economics={"available_award_capacity": 4}))
    assert target["quoted"] == {"lifecycle": "open", "available_award_capacity": 4}


def test_rail_self_report_raises_without_a_usable_listing_id():
    with pytest.raises(ValueError, match="no_listing_id"):
        rail_self_report({"title": "no id here"})
    with pytest.raises(ValueError, match="no_listing_id"):
        rail_self_report({"listing_id": -1})


# --- rail-report skill via f916.skills.run ------------------------------------

def test_rail_report_skill_writes_consistent_evidence(tmp_path):
    target = rail_self_report(listing(escrow_address="0xesc", economics={"available_award_capacity": 2}))
    result = skills.run("rail-report", target, {}, tmp_path)

    assert result["status"] == "consistent"
    assert result["verdict"] == "BID"
    assert result["quoted"] == target["quoted"]

    evidence_path = result["evidence_files"][0]
    content = open(evidence_path, "rb").read()
    assert hashlib.sha256(content).hexdigest() == result["hash"]
    evidence = json.loads(content)
    assert evidence["verdict"] == "BID"


def test_rail_report_skill_is_inconclusive_on_invalid_verdict(tmp_path):
    bad_target = {"listing_id": 41, "quoted": {"available_award_capacity": 1},
                  "verdict": "MAYBE", "verdict_basis": "x", "source": "s"}
    result = skills.run("rail-report", bad_target, {}, tmp_path)
    assert result["status"] == "inconclusive"
    assert result["findings"] == []


# --- evaluate_opportunity end-to-end -----------------------------------------

def test_evaluate_opportunity_matches_curated_template_without_audit_dict():
    item = listing(title="Rail-state stranger check", economics={"available_award_capacity": 3},
                    escrow_address="0xesc")
    evaluation = evaluate_opportunity(item)
    assert evaluation["classification"] == "supported"
    assert evaluation["skill"] == "rail-report"
    assert evaluation["template_id"] == "rail-state-self-report"
    assert evaluation["target"]["verdict"] == "BID"


def test_evaluate_opportunity_project_required_wins_over_template_match():
    item = listing(title="Rail-state stranger check",
                    condition="Please implement a fix and open a pull request.",
                    economics={"available_award_capacity": 3}, escrow_address="0xesc")
    evaluation = evaluate_opportunity(item)
    assert evaluation["classification"] == "project_required"
