import hashlib
import json

from f916.config import Settings
from f916.db import Database
from f916.opportunities import OpportunityRunner, evaluate_opportunity


def listing(**updates):
    base = {
        "listing_id": 41,
        "title": "Inspect a literal command gate",
        "condition": "Run the bounded, non-executing gate probe against the supplied rule.",
        "payload_hash": "a" * 64,
        "audit": {"skill": "gate-probe", "target": {"blocked_tokens": ["rm -rf"]}, "params": {}},
    }
    return base | updates


def test_opportunity_classification_is_deterministic_and_conservative():
    supported = evaluate_opportunity(listing())
    assert supported["classification"] == "supported"
    assert supported["skill"] == "gate-probe"
    assert supported == evaluate_opportunity(listing())

    project = evaluate_opportunity(listing(condition="Fix the repository and provide a regression test."))
    assert project["classification"] == "project_required"

    unsupported = evaluate_opportunity(listing(audit={"skill": "shell", "target": {"command": "echo owned"}}))
    assert unsupported["classification"] == "unsupported"
    assert "command" not in json.dumps(unsupported)


def test_listing_artifact_binds_id_condition_and_source_hash(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    item = listing()
    evaluation = evaluate_opportunity(item)
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, object(), executor=object())

    artifact = runner.build_artifact(item, evaluation)
    evidence_path = artifact["evidence_files"][0]
    evidence = json.loads(open(evidence_path, encoding="utf-8").read())

    assert artifact["listing_id"] == item["listing_id"]
    assert artifact["source_hash"] == evaluation["source_hash"]
    assert evidence["listing_id"] == item["listing_id"]
    assert evidence["source_condition"] == item["condition"]
    assert evidence["source_hash"] == evaluation["source_hash"]
    assert evidence["reproduction"]["executes_community_commands"] is False
    assert hashlib.sha256(open(evidence_path, "rb").read()).hexdigest() == artifact["hash"]


def test_pipeline_publishes_then_seals_then_submits_and_deduplicates(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize(); calls = []
    class Publisher:
        def publish(self, artifact):
            calls.append("publish")
            return artifact | {"commit": "b" * 40, "public_url": "https://github.com/erku/erku-audit/blob/" + "b" * 40 + "/artifacts/" + artifact["hash"] + ".json"}
    class API:
        def post(self, path, payload):
            calls.append(("seal", path, payload["hash"]))
            return {"id": 77}
    class Executor:
        def dispatch(self, intent):
            calls.append(("submit", intent.listing_id, intent.artifact))
            return {"status": "sent", "response": {"id": 9}}
    sealer = lambda client, settings, digest, label: client.post("/api/seal", {"hash": digest})
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, API(), Executor(), Publisher(), sealer=sealer)

    first = runner.process(listing())
    second = runner.process(listing())

    assert first["status"] == "submitted"
    assert second["status"] == "submitted"
    assert second["classification"] == "already_attempted"
    assert [c[0] if isinstance(c, tuple) else c for c in calls] == ["publish", "seal", "submit"]
    submitted = calls[-1][2]
    assert "sha256:" in submitted and "seal:77" in submitted and "commit:" + "b" * 40 in submitted
    artifact_events = db.events("artifact")
    assert len(artifact_events) == 1
    assert artifact_events[0]["data"]["listing_id"] == 41


def test_unsupported_and_project_work_never_publish_or_submit(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    class Bomb:
        def __getattr__(self, name):
            raise AssertionError(name)
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, Bomb(), Bomb(), Bomb())

    assert runner.process(listing(audit={"skill": "unknown", "target": {}}))["status"] == "unsupported"
    assert runner.process(listing(condition="Implement a fix and open a pull request."))["status"] == "project_required"
    assert {event["data"]["classification"] for event in db.events("opportunity")} == {"unsupported", "project_required"}


def test_uncertain_seal_or_submission_is_not_retried(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize(); calls = []
    class Publisher:
        def publish(self, artifact):
            calls.append("publish")
            return artifact | {"commit": "c" * 40, "public_url": "https://example.test/evidence"}
    class UncertainSeal:
        def post(self, path, payload):
            calls.append("seal")
            raise TimeoutError("outcome unknown")
    class Executor:
        def dispatch(self, intent):
            calls.append("submit")
            return {"status": "uncertain"}

    sealer = lambda client, settings, digest, label: client.post("/api/seal", {"hash": digest})
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, UncertainSeal(), Executor(), Publisher(), sealer=sealer)
    assert runner.process(listing())["status"] == "seal_uncertain"
    assert runner.process(listing())["status"] == "seal_uncertain"
    assert calls == ["publish", "seal"]

    second = listing(listing_id=42, payload_hash="d" * 64)
    class API:
        def post(self, path, payload): return {"id": 88}
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, API(), Executor(), Publisher(), sealer=sealer)
    assert runner.process(second)["status"] == "submission_uncertain"
    assert runner.process(second)["status"] == "submission_uncertain"
    assert calls.count("submit") == 1


def test_definite_publish_failure_can_resume_from_saved_artifact(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize(); attempts = []
    class Publisher:
        def publish(self, artifact):
            attempts.append(artifact["hash"])
            if len(attempts) == 1: raise RuntimeError("not published")
            return artifact | {"commit": "e" * 40, "public_url": "https://example.test/evidence"}
    class API:
        def post(self, path, payload): return {"id": 90}
    class Executor:
        def dispatch(self, intent): return {"status": "sent"}
    sealer = lambda client, settings, digest, label: client.post("/api/seal", {"hash": digest})
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, API(), Executor(), Publisher(), sealer=sealer)

    assert runner.process(listing())["status"] == "publish_failed"
    assert runner.process(listing())["status"] == "submitted"
    assert attempts[0] == attempts[1]


def test_worker_sends_open_listing_details_to_opportunity_runner(tmp_path):
    from f916.loop import Worker
    db = Database(tmp_path / "state.db"); db.initialize(); seen = []
    class API:
        def get(self, path, params=None):
            if path == "/api/listings":
                return {"listings": [{"id": 41, "expiry": 1999999999}]}
            if path == "/api/listings/41":
                return listing(economics={"available_award_capacity": 1})
            return {}
    class Brain:
        last_status = "ok"
        def decide(self, *args): return []
    class Opportunities:
        def process(self, item): seen.append(item["listing_id"])

    Worker(Settings(data_dir=tmp_path), db, API(), Brain(), opportunity_runner=Opportunities()).cycle()
    assert seen == [41]


def test_evaluate_accepts_api_string_id_and_rejects_mismatch():
    # The live API serves id as the string "listing-<n>" alongside int listing_id.
    ok = evaluate_opportunity(listing(id="listing-41"))
    assert ok["classification"] == "supported"
    assert ok["listing_id"] == 41

    mismatch = evaluate_opportunity(listing(id="listing-99"))
    assert mismatch["classification"] == "unsupported"
    assert mismatch["reason"] == "invalid_listing_identity"

    id_only = evaluate_opportunity({"id": "listing-7", "title": "t", "condition": "c",
                                    "audit": {"skill": "gate-probe", "target": {"blocked_tokens": ["rm -rf"]}, "params": {}}})
    assert id_only["classification"] == "supported"
    assert id_only["listing_id"] == 7


def test_cached_unsupported_is_reevaluated_when_a_new_capability_supports_it(tmp_path):
    # A listing seen as unsupported before a capability shipped must re-open
    # once a fresh evaluation classifies it supported — not stay terminally stuck.
    db = Database(tmp_path / "state.db"); db.initialize(); calls = []
    item = listing()
    evaluation = evaluate_opportunity(item)
    assert evaluation["classification"] == "supported"
    class Publisher:
        def publish(self, artifact):
            calls.append("publish")
            return artifact | {"commit": "b" * 40, "public_url": "https://github.com/erku/erku-audit/blob/" + "b" * 40 + "/artifacts/" + artifact["hash"] + ".json"}
    class API:
        def post(self, path, payload): return {"id": 5}
    class Executor:
        def dispatch(self, intent): calls.append("submit"); return {"status": "sent", "response": {"id": 9}}
    sealer = lambda client, settings, digest, label: client.post("/api/seal", {"hash": digest})
    runner = OpportunityRunner(Settings(data_dir=tmp_path), db, API(), Executor(), Publisher(), sealer=sealer)
    # Pre-seed the stale terminal 'unsupported' state for this exact listing.
    key = runner._key(evaluation)
    db.set_setting(key, {"status": "unsupported", "classification": "unsupported"})

    result = runner.process(item)
    assert result["status"] == "submitted"
    assert "publish" in calls and "submit" in calls

    # A cached 'unsupported' that still evaluates unsupported stays terminal.
    db2 = Database(tmp_path / "s2.db"); db2.initialize()
    unsup = listing(audit={"skill": "unknown", "target": {}})
    ev2 = evaluate_opportunity(unsup)
    assert ev2["classification"] == "unsupported"
    r2 = OpportunityRunner(Settings(data_dir=tmp_path), db2, API(), Executor(), Publisher(), sealer=sealer)
    db2.set_setting(r2._key(ev2), {"status": "unsupported", "classification": "unsupported"})
    assert r2.process(unsup)["classification"] == "already_attempted"

    # A cached 'submitted' stays terminal (never re-submits).
    db3 = Database(tmp_path / "s3.db"); db3.initialize()
    r3 = OpportunityRunner(Settings(data_dir=tmp_path), db3, API(), Executor(), Publisher(), sealer=sealer)
    db3.set_setting(r3._key(evaluation), {"status": "submitted"})
    assert r3.process(item)["classification"] == "already_attempted"
