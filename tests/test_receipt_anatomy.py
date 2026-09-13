import json

import pytest

from f916.config import Settings
from f916.db import Database
from f916.opportunities import OpportunityRunner, evaluate_opportunity
from f916.skills import run as run_skill
from f916.templates import BUILDERS, receipt_anatomy


def listing(**updates):
    base = {
        "listing_id": 31,
        "title": "Receipt Anatomy Spot-Check",
        "condition": "Name payout-binding 31 and quote its receipt fields.",
        "payload_hash": "b" * 64,
    }
    return base | updates


# --- 2b: receipt_anatomy builder --------------------------------------------

def test_receipt_anatomy_extracts_binding_id_from_payout_binding_phrase():
    target = receipt_anatomy(listing(condition="Name payout-binding 265 and quote its receipt."))
    assert target == {
        "binding_id": 265,
        "source": "GET https://1f916.ai/api/payout-bindings/265",
        "walk": "binding",
    }


def test_receipt_anatomy_extracts_binding_id_from_bare_binding_phrase():
    target = receipt_anatomy(listing(condition="See binding 42 for the settlement receipt."))
    assert target["binding_id"] == 42
    assert target["walk"] == "binding"


def test_receipt_anatomy_raises_no_binding_id_when_absent():
    with pytest.raises(ValueError, match="no_binding_id"):
        receipt_anatomy(listing(condition="No numeric reference here at all."))
    with pytest.raises(ValueError, match="no_binding_id"):
        receipt_anatomy(None)


def test_receipt_anatomy_registered_in_builders():
    assert BUILDERS["receipt_anatomy"] is receipt_anatomy


# --- 2c: build_artifact walk:'binding' --------------------------------------

class _BindingClient:
    def __init__(self, response=None, raises=False):
        self.response = response
        self.raises = raises
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(path)
        if self.raises:
            raise RuntimeError("upstream down")
        return self.response


def test_build_artifact_walk_binding_paid_from_nested_receipt(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    client = _BindingClient({
        "id": 265,
        "receipt": {"receipt_id": "r-9", "tx_hash": "0xabc", "transfer_log_index": 3, "amount_atomic": 5000},
    })
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, client, executor=object())
    item = listing()
    evaluation = evaluate_opportunity(item)
    assert evaluation["classification"] == "supported"
    assert evaluation["template_id"] == "receipt-anatomy"

    artifact = runner.build_artifact(item, evaluation)
    assert client.calls == ["/api/payout-bindings/31"]
    assert artifact["verdict"] == "PAID"
    assert artifact["quoted"]["receipt_id"] == "r-9"
    assert artifact["quoted"]["tx_hash"] == "0xabc"
    assert artifact["quoted"]["transfer_log_index"] == 3
    # The quoted binding_id comes from the listing's own prose (31, via the
    # receipt_anatomy builder) -- not from any "id" field in the fetched
    # payout-binding JSON, which is a different, unrelated identifier.
    assert "payout-binding 31" in artifact["summary"]
    assert "PAID" in artifact["summary"]


def test_build_artifact_walk_binding_missing_fields_is_unproven(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    client = _BindingClient({"id": 265})  # no receipt fields anywhere
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, client, executor=object())
    item = listing()
    evaluation = evaluate_opportunity(item)

    artifact = runner.build_artifact(item, evaluation)
    assert artifact["verdict"] == "UNPROVEN"


def test_build_artifact_walk_binding_payable_without_tx_hash(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    client = _BindingClient({"receipt_id": "r-1", "amount_atomic": 1234})
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, client, executor=object())
    item = listing()
    evaluation = evaluate_opportunity(item)

    artifact = runner.build_artifact(item, evaluation)
    assert artifact["verdict"] == "PAYABLE"


def test_build_artifact_walk_binding_client_error_is_unproven_no_crash(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    client = _BindingClient(raises=True)
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, client, executor=object())
    item = listing()
    evaluation = evaluate_opportunity(item)

    artifact = runner.build_artifact(item, evaluation)
    assert artifact["verdict"] == "UNPROVEN"
    assert artifact["quoted"] == {}


# --- 2a: _receipt_report skill via run() ------------------------------------

def test_receipt_report_skill_consistent_with_evidence(tmp_path):
    target = {
        "binding_id": 265,
        "quoted": {"receipt_id": "r-9", "tx_hash": "0xabc", "transfer_log_index": 3, "amount_atomic": 5000},
        "verdict": "PAID",
        "verdict_basis": "tx_hash and transfer_log_index show a settled transfer.",
        "source": "GET https://1f916.ai/api/payout-bindings/265",
    }
    result = run_skill("receipt-report", target, {}, tmp_path / "artifacts")
    assert result["status"] == "consistent"
    assert result["findings"] == []
    assert "payout-binding 265" in result["summary"]
    assert result["verdict"] == "PAID"
    assert "hash" in result
    evidence = json.loads(open(result["evidence_files"][0], encoding="utf-8").read())
    assert evidence["skill"] == "receipt-report"
    assert evidence["verdict"] == "PAID"


def test_receipt_report_skill_inconclusive_on_invalid_verdict(tmp_path):
    target = {"binding_id": 265, "quoted": {"receipt_id": "r-9"}, "verdict": "BOGUS"}
    result = run_skill("receipt-report", target, {}, tmp_path / "artifacts")
    assert result["status"] == "inconclusive"
    assert result["findings"] == []


def test_receipt_report_skill_inconclusive_when_quoted_missing(tmp_path):
    target = {"binding_id": 265, "verdict": "PAID"}
    result = run_skill("receipt-report", target, {}, tmp_path / "artifacts")
    assert result["status"] == "inconclusive"


# --- 2d: evaluate_opportunity routing ---------------------------------------

def test_evaluate_opportunity_routes_receipt_anatomy_listing_to_receipt_report():
    evaluation = evaluate_opportunity(listing())
    assert evaluation["classification"] == "supported"
    assert evaluation["skill"] == "receipt-report"
    assert evaluation["template_id"] == "receipt-anatomy"
    assert evaluation["target"]["binding_id"] == 31
    assert evaluation["target"]["walk"] == "binding"


def test_evaluate_opportunity_falls_back_to_unsupported_without_binding_id():
    # A "receipt" titled listing whose condition names no binding id at all
    # -- the builder raises no_binding_id -- must not crash classification.
    evaluation = evaluate_opportunity(listing(condition="A receipt-themed listing with no numeric reference."))
    assert evaluation["classification"] == "unsupported"
