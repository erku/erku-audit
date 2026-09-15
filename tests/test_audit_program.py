import re

from f916.audit_program import ROTATION, build_inputs, input_fingerprint, plan_next_audit
from f916.skills import run as run_skill

LABEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

RAIL_OK = {"awards": [{"amount_atomic": "5", "currency": "usd"}],
           "receipts": [{"amount_atomic": "5", "currency": "usd"}],
           "listing_id": 27}
LEAK_OK = {"listings": [{"title": "hello", "listing_id": 1}]}
GATE_OK = ["rm -rf"]


def _label_ok(label):
    assert LABEL_RE.fullmatch(label), label


# 1. rotation advances deterministically and wraps
def test_rotation_advances_and_wraps():
    inputs = {"rail": RAIL_OK, "leak_target": LEAK_OK, "gate_target": GATE_OK}
    seen = []
    cursor = 0
    for _ in range(len(ROTATION) * 2):
        job = plan_next_audit(cursor, inputs)
        seen.append(job["skill"])
        cursor = job["cursor_next"]
    assert seen == list(ROTATION) * 2
    # wraps back to the start
    assert seen[len(ROTATION)] == ROTATION[0]


def test_rotation_cursor_next_is_deterministic_index():
    inputs = {"rail": RAIL_OK, "leak_target": LEAK_OK, "gate_target": GATE_OK}
    job = plan_next_audit(1, inputs)
    assert job["skill"] == "rail-audit"
    assert job["cursor_next"] == 2
    job_wrap = plan_next_audit(len(ROTATION) - 1, inputs)
    assert job_wrap["skill"] == ROTATION[-1]
    assert job_wrap["cursor_next"] == 0


# 2. same cursor+inputs -> identical job (stable)
def test_same_cursor_and_inputs_is_stable():
    inputs = {"rail": RAIL_OK, "leak_target": LEAK_OK, "gate_target": GATE_OK}
    job1 = plan_next_audit(2, inputs)
    job2 = plan_next_audit(2, inputs)
    assert job1 == job2


def test_input_fingerprint_is_stable_across_mapping_order():
    first = {"skill": "leak-probe", "target": {"listings": [{"title": "x", "listing_id": 1}]},
             "params": {}, "listing_id": None}
    second = {"params": {}, "listing_id": None,
              "target": {"listings": [{"listing_id": 1, "title": "x"}]}, "skill": "leak-probe"}
    assert input_fingerprint(first) == input_fingerprint(second)


# 3. missing inputs for a slot are skipped in favor of the next runnable slot
def test_missing_slot_inputs_are_skipped():
    # cursor points at rail-audit (index 1), but no rail input is available;
    # leak-probe (index 2) should be picked instead.
    inputs = {"rail": None, "leak_target": LEAK_OK, "gate_target": None}
    job = plan_next_audit(1, inputs)
    assert job["skill"] == "leak-probe"
    assert job["cursor_next"] == 3


def test_missing_slot_inputs_skips_gate_too():
    inputs = {"rail": None, "leak_target": None, "gate_target": None}
    job = plan_next_audit(1, inputs)
    assert job["skill"] == "self-redteam"


# 4. self-redteam is the guaranteed fallback when nothing else is runnable
def test_self_redteam_is_guaranteed_fallback():
    for cursor in range(len(ROTATION)):
        job = plan_next_audit(cursor, {})
        assert job["skill"] == "self-redteam"
        assert job["target"] == {}
        assert job["params"] == {}
        _label_ok(job["label"])


def test_self_redteam_not_forced_when_something_else_is_runnable():
    inputs = {"rail": RAIL_OK}
    job = plan_next_audit(1, inputs)
    assert job["skill"] == "rail-audit"
    assert job["listing_id"] == 27
    assert job["label"] == "rail-audit-27"


# 5. build_inputs extracts a rail target, None when absent
def test_build_inputs_extracts_rail_target():
    details = [
        {"listing_id": 5, "title": "nothing here"},
        {"listing_id": 9, "awards": [{"amount_atomic": "1"}], "receipts": [{"amount_atomic": "1"}]},
    ]
    inputs = build_inputs(client=None, listings_details=details)
    assert inputs["rail"] == {
        "awards": [{"amount_atomic": "1"}],
        "receipts": [{"amount_atomic": "1"}],
        "listing_id": 9,
    }


def test_build_inputs_rail_none_when_absent():
    details = [{"listing_id": 5, "title": "no awards here"}, {"not": "a listing detail, still tolerated"}]
    inputs = build_inputs(client=None, listings_details=details)
    assert inputs["rail"] is None


def test_build_inputs_leak_target_from_titles_and_conditions():
    details = [{"listing_id": 3, "title": "widget", "condition": "used"}]
    inputs = build_inputs(client=None, listings_details=details)
    assert inputs["leak_target"] == {"listings": [{"title": "widget", "condition": "used", "listing_id": 3}]}


def test_build_inputs_leak_target_none_when_no_public_fields():
    inputs = build_inputs(client=None, listings_details=[{"listing_id": 1}])
    assert inputs["leak_target"] is None


def test_leak_probe_is_inconclusive_without_a_data_surface(tmp_path):
    result = run_skill("leak-probe", {}, {}, tmp_path)
    assert result["status"] == "inconclusive"
    assert result["findings"] == []


def test_build_inputs_gate_target_none_without_policy_tokens():
    # config/policy.json in this repo does not declare blocked_tokens today.
    inputs = build_inputs(client=None, listings_details=[])
    assert inputs["gate_target"] is None


# 6. malformed inputs never raise
def test_plan_next_audit_never_raises_on_malformed_inputs():
    malformed_inputs = [
        None, "garbage", 42, [], {},
        {"rail": 42}, {"rail": "nope"}, {"rail": {"awards": "not-a-list", "receipts": []}},
        {"rail": {"awards": [], "receipts": []}},
        {"rail": {"awards": [1], "receipts": [1], "listing_id": "not-an-int"}},
        {"rail": {"awards": [1], "receipts": [1], "listing_id": -5}},
        {"leak_target": "nope"}, {"leak_target": []}, {"leak_target": {}},
        {"gate_target": "nope"}, {"gate_target": []}, {"gate_target": [1, 2]}, {"gate_target": [""]},
    ]
    malformed_cursors = [0, -1, "abc", None, 3.7, 999999]
    for cursor in malformed_cursors:
        for inputs in malformed_inputs:
            job = plan_next_audit(cursor, inputs)
            assert job["skill"] in ROTATION
            _label_ok(job["label"])


def test_build_inputs_never_raises_on_malformed_rows():
    malformed = [None, "garbage", 42, [1, 2, {"awards": None, "receipts": None}],
                 [{"awards": [1, 2], "receipts": "nope"}], [{"title": 12345, "condition": None}]]
    for listings_details in malformed:
        inputs = build_inputs(client=None, listings_details=listings_details)
        assert set(inputs) == {"rail", "leak_target", "gate_target"}


# 7. labels always match the seal slug regex
def test_all_rotation_labels_match_seal_slug_regex():
    inputs = {"rail": RAIL_OK, "leak_target": LEAK_OK, "gate_target": GATE_OK}
    for cursor in range(len(ROTATION)):
        job = plan_next_audit(cursor, inputs)
        _label_ok(job["label"])


def test_rail_audit_label_with_large_listing_id_matches_regex():
    rail = {"awards": [1], "receipts": [1], "listing_id": 999999999999}
    job = plan_next_audit(1, {"rail": rail})
    assert job["skill"] == "rail-audit"
    _label_ok(job["label"])
