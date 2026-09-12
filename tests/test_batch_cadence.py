"""Tests for the batch-cadence skill's pure clustering math and the
/api/payouts walk helper it (and the verifier) is fed by."""
import json

from f916 import skills
from f916.config import Settings
from f916.db import Database
from f916.opportunities import OpportunityRunner, evaluate_opportunity, walk_payout_receipts


# --- _batch_cadence (pure skill) ---------------------------------------------

def receipt(binding_id, block_timestamp, receipt_id=None, created_at=0):
    return {"binding_id": binding_id, "receipt_id": receipt_id or f"r{binding_id}",
            "block_timestamp": block_timestamp, "created_at": created_at}


# Two clear clusters: [100,110,130] (span 30) and [400,405] (span 5).
_TWO_CLUSTERS = [receipt(1, 100), receipt(2, 110), receipt(3, 130), receipt(4, 400), receipt(5, 405)]


def test_batch_cadence_clusters_by_window_and_reports_span_and_members():
    target = {"receipts": _TWO_CLUSTERS, "window_seconds": 60, "source": "GET https://1f916.ai/api/payouts"}
    result = skills._batch_cadence(target, {})

    assert result["status"] == "consistent"
    assert result["receipt_count"] == 5
    assert result["batch_count"] == 2
    assert result["batches"] == [
        {"members": [1, 2, 3], "span_seconds": 30},
        {"members": [4, 5], "span_seconds": 5},
    ]
    assert result["cadence_statement"] == "2 batches, n>=2, median inter-batch interval 300s"
    assert result["window_seconds"] == 60
    assert result["source"] == target["source"]
    assert "5 receipts" in result["summary"] and "2 batches" in result["summary"]
    assert result["findings"] == []


def test_batch_cadence_sorts_out_of_order_input_by_timestamp():
    shuffled = [_TWO_CLUSTERS[3], _TWO_CLUSTERS[0], _TWO_CLUSTERS[4], _TWO_CLUSTERS[2], _TWO_CLUSTERS[1]]
    result = skills._batch_cadence({"receipts": shuffled, "window_seconds": 60}, {})
    assert result["batches"] == [
        {"members": [1, 2, 3], "span_seconds": 30},
        {"members": [4, 5], "span_seconds": 5},
    ]


def test_batch_cadence_normalizes_millisecond_timestamps():
    ms_receipts = [receipt(10, 1_700_000_000_000), receipt(11, 1_700_000_010_000)]
    result = skills._batch_cadence({"receipts": ms_receipts, "window_seconds": 60}, {})
    assert result["batch_count"] == 1
    assert result["batches"][0] == {"members": [10, 11], "span_seconds": 10}


def test_batch_cadence_single_receipt_has_no_batches_and_honest_statement():
    result = skills._batch_cadence({"receipts": [receipt(1, 100)], "window_seconds": 60}, {})
    assert result["status"] == "consistent"
    assert result["receipt_count"] == 1
    assert result["batch_count"] == 0
    assert result["batches"] == []
    assert result["cadence_statement"] == \
        "fewer than two batches exist, so cadence is not identifiable from this data (n=0)"


def test_batch_cadence_missing_or_empty_receipts_is_inconclusive():
    assert skills._batch_cadence({}, {}) == {
        "status": "inconclusive", "findings": [],
        "summary": "batch-cadence requires a receipts list walked from /api/payouts.",
    }
    assert skills._batch_cadence({"receipts": "nope"}, {})["status"] == "inconclusive"
    assert skills._batch_cadence({"receipts": []}, {})["status"] == "inconclusive"


def test_batch_cadence_is_deterministic():
    target = {"receipts": _TWO_CLUSTERS, "window_seconds": 60}
    assert skills._batch_cadence(target, {}) == skills._batch_cadence(target, {})


def test_batch_cadence_never_raises_and_drops_unusable_rows():
    target = {"receipts": [
        None, "garbage", 42,
        {"binding_id": 1, "block_timestamp": float("nan")},
        {"binding_id": 2, "block_timestamp": "not-a-number"},
        {"binding_id": 3, "block_timestamp": True},
        receipt(4, 100),
    ]}
    result = skills._batch_cadence(target, {})
    assert result["status"] == "consistent"
    assert result["receipt_count"] == 1


def test_batch_cadence_skill_writes_reproducible_evidence(tmp_path):
    target = {"receipts": _TWO_CLUSTERS, "window_seconds": 60, "source": "GET https://1f916.ai/api/payouts"}
    result = skills.run("batch-cadence", target, {}, tmp_path)
    assert result["status"] == "consistent"
    evidence = json.loads(open(result["evidence_files"][0], encoding="utf-8").read())
    assert evidence["batch_count"] == 2
    assert evidence["reproduction"]["executes_community_commands"] is False


# --- walk_payout_receipts helper ---------------------------------------------

class TwoPagePayoutsClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        assert path == "/api/payouts"
        if params is None:
            return {"bindings": [
                {"id": 1, "receipt_id": "r1", "block_timestamp": 100, "created_at": 1},
                {"id": 2, "receipt_id": None, "block_timestamp": 110, "created_at": 2},  # no receipt -> dropped
                {"id": 3, "receipt_id": "r3", "block_timestamp": "bad", "created_at": 3},  # non-numeric -> dropped
            ], "has_more": True, "next_since_id": 3}
        assert params == {"since_id": 3}
        return {"bindings": [
            {"id": 4, "receipt_id": "r4", "block_timestamp": 400, "created_at": 4},
        ], "has_more": False, "next_since_id": None}


def test_walk_payout_receipts_paginates_and_filters_to_settled_receipts():
    client = TwoPagePayoutsClient()
    receipts = walk_payout_receipts(client)
    assert receipts == [
        {"binding_id": 1, "receipt_id": "r1", "block_timestamp": 100, "created_at": 1},
        {"binding_id": 4, "receipt_id": "r4", "block_timestamp": 400, "created_at": 4},
    ]
    assert client.calls == [("/api/payouts", None), ("/api/payouts", {"since_id": 3})]


def test_walk_payout_receipts_tolerates_client_error():
    class BoomClient:
        def get(self, path, params=None):
            raise RuntimeError("boom")
    assert walk_payout_receipts(BoomClient()) == []


def test_walk_payout_receipts_stops_after_hard_page_cap():
    class InfinitePagesClient:
        def __init__(self):
            self.calls = 0
        def get(self, path, params=None):
            self.calls += 1
            return {"bindings": [], "has_more": True, "next_since_id": self.calls}
    client = InfinitePagesClient()
    assert walk_payout_receipts(client, max_pages=3) == []
    assert client.calls == 3


# --- OpportunityRunner.build_artifact wiring ---------------------------------

def batch_cadence_item(**updates):
    base = {"listing_id": 41, "title": "Turbo: measure settlement batch cadence", "condition": "c"}
    return base | updates


def test_build_artifact_populates_receipts_for_walk_template(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    client = TwoPagePayoutsClient()
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, client, executor=object())
    item = batch_cadence_item()
    evaluation = evaluate_opportunity(item)
    assert evaluation["target"]["walk"] == "payouts"

    artifact = runner.build_artifact(item, evaluation)
    assert artifact["status"] == "consistent"
    assert artifact["receipt_count"] == 2
    evidence = json.loads(open(artifact["evidence_files"][0], encoding="utf-8").read())
    assert "walk" not in evidence["input"]["target"]
    assert len(evidence["input"]["target"]["receipts"]) == 2


def test_build_artifact_walk_error_is_inconclusive_not_a_crash(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    class BoomClient:
        def get(self, path, params=None):
            raise RuntimeError("boom")
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, BoomClient(), executor=object())
    item = batch_cadence_item()
    evaluation = evaluate_opportunity(item)

    artifact = runner.build_artifact(item, evaluation)
    assert artifact["status"] == "inconclusive"


def test_build_artifact_leaves_non_walk_templates_untouched(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    class NeverCalled:
        def get(self, path, params=None):
            raise AssertionError("non-walk templates must never trigger a payouts walk")
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, NeverCalled(), executor=object())
    item = {"listing_id": 41, "title": "Rail-state stranger check", "condition": "c",
            "escrow_address": "0xesc", "economics": {"available_award_capacity": 2}}
    evaluation = evaluate_opportunity(item)
    assert evaluation["skill"] == "rail-report"
    assert "walk" not in evaluation["target"]

    artifact = runner.build_artifact(item, evaluation)
    assert artifact["status"] == "consistent"
