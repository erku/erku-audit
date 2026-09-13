import hashlib
import json

import pytest

from f916 import skills
from f916.opportunities import evaluate_opportunity
from f916.templates import (BUILDERS, load_templates, match_template, rail_self_report,
                             rail_derivation_check, batch_cadence)


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
    assert {"rail-state-self-report", "award-slot-census",
            "rail-false-number", "break-the-rail", "batch-cadence"} <= ids
    for t in templates:
        assert t["skill"] in {"rail-report", "rail-derivation-check", "batch-cadence"}
        assert t["builder"] in BUILDERS


def test_load_templates_never_raises_on_bad_path_or_content(tmp_path):
    assert load_templates(tmp_path / "does-not-exist.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_templates(bad) == []
    not_a_list = tmp_path / "obj.json"
    not_a_list.write_text("{}", encoding="utf-8")
    assert load_templates(not_a_list) == []


def test_load_templates_merges_operator_and_auto_stores_operator_first(tmp_path):
    operator_path = tmp_path / "bounty_templates.json"
    operator_path.write_text(json.dumps([
        {"id": "operator-one", "skill": "rail-report", "builder": "rail_self_report", "title_contains": ["stranger"]},
    ]), encoding="utf-8")
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text(json.dumps([
        {"id": "auto-one", "skill": "batch-cadence", "builder": "batch_cadence", "title_contains": ["cadence"]},
    ]), encoding="utf-8")

    merged = load_templates(operator_path, auto_path)

    assert [t["id"] for t in merged] == ["operator-one", "auto-one"]


def test_load_templates_tolerates_missing_or_malformed_auto_store(tmp_path):
    operator_path = tmp_path / "bounty_templates.json"
    operator_path.write_text(json.dumps([
        {"id": "operator-one", "skill": "rail-report", "builder": "rail_self_report", "title_contains": ["stranger"]},
    ]), encoding="utf-8")

    # Missing auto store -- operator entries still load.
    assert [t["id"] for t in load_templates(operator_path, tmp_path / "missing-auto.json")] == ["operator-one"]

    # Malformed auto store -- operator entries still load, auto contributes nothing.
    bad_auto = tmp_path / "bad_auto.json"
    bad_auto.write_text("not json", encoding="utf-8")
    assert [t["id"] for t in load_templates(operator_path, bad_auto)] == ["operator-one"]

    # Auto store with a non-dict entry -- that entry is skipped, not raised.
    mixed_auto = tmp_path / "mixed_auto.json"
    mixed_auto.write_text(json.dumps(["not-a-dict", {"id": "auto-good", "skill": "batch-cadence",
                                                       "builder": "batch_cadence", "title_contains": ["cadence"]}]),
                           encoding="utf-8")
    assert [t["id"] for t in load_templates(operator_path, mixed_auto)] == ["operator-one", "auto-good"]


def test_load_templates_operator_entry_wins_a_match_over_an_auto_entry():
    from f916.templates import match_template
    operator_entry = {"id": "operator", "skill": "rail-report", "builder": "rail_self_report",
                       "title_contains": ["stranger"]}
    auto_entry = {"id": "auto", "skill": "batch-cadence", "builder": "batch_cadence",
                  "title_contains": ["stranger"]}
    merged = [operator_entry, auto_entry]
    matched = match_template(listing(title="A stranger-checkable rail listing"), merged)
    assert matched["id"] == "operator"


def test_real_operator_seed_file_has_exactly_five_curated_templates_and_is_never_selfext_written():
    from f916.templates import _DEFAULT_PATH
    on_disk = json.loads(_DEFAULT_PATH.read_text(encoding="utf-8"))
    assert isinstance(on_disk, list)
    assert len(on_disk) == 5
    assert {t["id"] for t in on_disk} == {"rail-state-self-report", "award-slot-census",
                                           "rail-false-number", "break-the-rail", "batch-cadence"}


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


# --- _rail_derivation ---------------------------------------------------------

def test_rail_derivation_consistent_economics():
    target = {"listing_id": 41, "economics": {
        "outstanding_awarded_atomic": "500", "currently_due_atomic": "300", "overdue_unpaid_atomic": "200",
        "max_awards": 10, "awarded_slots_used": 4, "available_award_capacity": 6,
    }}
    result = skills._rail_derivation(target, {})
    assert result["status"] == "consistent"
    assert result["findings"] == []


def test_rail_derivation_flags_outstanding_mismatch():
    target = {"listing_id": 41, "economics": {
        "outstanding_awarded_atomic": "999", "currently_due_atomic": "300", "overdue_unpaid_atomic": "200",
    }}
    result = skills._rail_derivation(target, {})
    assert result["status"] == "findings"
    finding = result["findings"][0]
    assert finding["identity"] == "outstanding_awarded_atomic == currently_due_atomic + overdue_unpaid_atomic"
    assert finding["served"] == "999"
    assert finding["computed"] == "500"


def test_rail_derivation_flags_capacity_mismatch():
    target = {"listing_id": 41, "economics": {
        "max_awards": 10, "awarded_slots_used": 4, "available_award_capacity": 999,
    }}
    result = skills._rail_derivation(target, {})
    assert result["status"] == "findings"
    finding = result["findings"][0]
    assert finding["identity"] == "available_award_capacity == max_awards - awarded_slots_used"
    assert finding["served"] == "999"
    assert finding["computed"] == "6"


def test_rail_derivation_missing_economics_is_inconclusive():
    result = skills._rail_derivation({"listing_id": 41}, {})
    assert result == {"status": "inconclusive", "findings": [],
                       "summary": "No checkable published economic identities in the supplied fields."}
    result = skills._rail_derivation({"listing_id": 41, "economics": "not-a-dict"}, {})
    assert result["status"] == "inconclusive"
    result = skills._rail_derivation({"listing_id": 41, "economics": {}}, {})
    assert result["status"] == "inconclusive"


def test_rail_derivation_flags_non_integer_atomic_field():
    target = {"listing_id": 41, "economics": {
        "outstanding_awarded_atomic": "not-a-number", "currently_due_atomic": "300", "overdue_unpaid_atomic": "200",
        "max_awards": 10, "awarded_slots_used": 4, "available_award_capacity": 6,
    }}
    result = skills._rail_derivation(target, {})
    assert result["status"] == "findings"
    assert {"field": "outstanding_awarded_atomic", "issue": "not_a_nonnegative_integer",
            "value": "not-a-number"} in result["findings"]
    # The other identity (capacity) is still checkable and consistent, and the
    # broken identity is skipped rather than falsely flagged as a mismatch.
    assert not any(f.get("identity", "").startswith("outstanding_awarded_atomic ==") for f in result["findings"])


def test_rail_derivation_never_raises_on_malformed_input():
    assert skills._rail_derivation({}, {})["status"] == "inconclusive"
    assert skills._rail_derivation({"economics": None}, {})["status"] == "inconclusive"
    assert skills._rail_derivation({"economics": {"outstanding_awarded_atomic": None}}, {})["status"] == "inconclusive"


# --- rail_derivation_check builder --------------------------------------------

def test_rail_derivation_check_builds_target_with_listing_id_and_economics():
    target = rail_derivation_check(listing(economics={"outstanding_awarded_atomic": "500"}))
    assert target == {"listing_id": 41, "economics": {"outstanding_awarded_atomic": "500"}}
    # Missing/non-dict economics becomes {}, never raises.
    target = rail_derivation_check(listing())
    assert target == {"listing_id": 41, "economics": {}}


def test_rail_derivation_check_raises_without_a_usable_listing_id():
    with pytest.raises(ValueError, match="no_listing_id"):
        rail_derivation_check({"title": "no id here"})
    with pytest.raises(ValueError, match="no_listing_id"):
        rail_derivation_check({"listing_id": -1})


# --- rail-derivation-check skill via f916.skills.run --------------------------

def test_rail_derivation_check_skill_writes_consistent_evidence(tmp_path):
    target = rail_derivation_check(listing(economics={
        "outstanding_awarded_atomic": "500", "currently_due_atomic": "300", "overdue_unpaid_atomic": "200",
    }))
    result = skills.run("rail-derivation-check", target, {}, tmp_path)

    assert result["status"] == "consistent"
    evidence_path = result["evidence_files"][0]
    content = open(evidence_path, "rb").read()
    assert hashlib.sha256(content).hexdigest() == result["hash"]


# --- evaluate_opportunity routes the derivation-check template ---------------

def test_evaluate_opportunity_matches_rail_false_number_template():
    item = listing(title="Bounty: find a false number in the rail",
                    economics={"outstanding_awarded_atomic": "999", "currently_due_atomic": "300",
                               "overdue_unpaid_atomic": "200"})
    evaluation = evaluate_opportunity(item)
    assert evaluation["classification"] == "supported"
    assert evaluation["skill"] == "rail-derivation-check"
    assert evaluation["template_id"] == "rail-false-number"


# --- batch_cadence builder ----------------------------------------------------

def test_batch_cadence_builder_returns_walk_marker_and_defaults():
    target = batch_cadence(listing())
    assert target == {"listing_id": 41, "window_seconds": 60,
                       "source": "GET https://1f916.ai/api/payouts", "walk": "payouts"}


def test_batch_cadence_builder_accepts_string_resource_id():
    target = batch_cadence(listing(listing_id=None, id="listing-7"))
    assert target["listing_id"] == 7
    assert target["walk"] == "payouts"


def test_batch_cadence_builder_raises_without_a_usable_listing_id():
    with pytest.raises(ValueError, match="no_listing_id"):
        batch_cadence({"title": "no id here"})
    with pytest.raises(ValueError, match="no_listing_id"):
        batch_cadence({"listing_id": -1})


# --- evaluate_opportunity routes the batch-cadence template ------------------

def test_evaluate_opportunity_matches_batch_cadence_template_without_audit_dict():
    item = listing(title="Turbo: measure settlement batch cadence")
    evaluation = evaluate_opportunity(item)
    assert evaluation["classification"] == "supported"
    assert evaluation["skill"] == "batch-cadence"
    assert evaluation["template_id"] == "batch-cadence"
    assert evaluation["target"]["walk"] == "payouts"
