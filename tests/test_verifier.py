import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey, Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from f916.config import Settings
from f916.db import Database
from f916.verifier import Verifier, build_and_sign_verdict, eligible, evaluate_submission


def settings_with_key(tmp_path, **updates):
    key = Ed25519PrivateKey.generate()
    (tmp_path / "identity-ed25519.pem").write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    kwargs = {"data_dir": tmp_path, "handle": "tester", "api_key": "key"} | updates
    return Settings(**kwargs), key


def listing(**updates):
    base = {
        "listing_id": 41,
        "max_verifiers": 1,
        "verifier_price_atomic": 5000,
        "submissions": [],
    }
    return base | updates


# --- eligible ---------------------------------------------------------------

def test_eligible_requires_positive_max_verifiers_and_price():
    assert eligible(listing()) is True
    assert eligible(listing(max_verifiers=0)) is False
    assert eligible(listing(verifier_price_atomic=0)) is False
    assert eligible(listing(max_verifiers=-1)) is False
    assert eligible(listing(verifier_price_atomic=None)) is False
    assert eligible(listing(max_verifiers=True)) is False
    assert eligible("not-a-listing") is False
    assert eligible({}) is False


# --- evaluate_submission ------------------------------------------------------

def test_evaluate_submission_abstains_by_default_on_unknown_claim():
    decision = evaluate_submission(listing(), {"id": 1, "claim_class": "vibes", "handle": "someone"})
    assert decision["verdict"] == "ABSTAIN"
    assert decision["claim_class"] == "vibes"

    decision = evaluate_submission(listing(), {"id": 2, "handle": "someone"})
    assert decision["verdict"] == "ABSTAIN"
    assert decision["claim_class"] == "unknown"


def test_evaluate_submission_never_raises_on_malformed_input():
    assert evaluate_submission(None, {"claim_class": "economic_identity"})["verdict"] == "ABSTAIN"
    assert evaluate_submission(listing(), None)["verdict"] == "ABSTAIN"
    assert evaluate_submission("nope", "nope")["verdict"] == "ABSTAIN"
    assert evaluate_submission({}, {"claim_class": "economic_identity"})["verdict"] == "ABSTAIN"


def test_evaluate_submission_economic_identity_pass_and_fail():
    consistent = listing(economics={"outstanding_awarded_atomic": 100, "currently_due_atomic": 40, "overdue_unpaid_atomic": 60})
    decision = evaluate_submission(consistent, {"id": 1, "claim_class": "economic_identity"})
    assert decision == {"verdict": "PASS", "basis": decision["basis"], "claim_class": "economic_identity"}

    violated = listing(economics={"outstanding_awarded_atomic": 999, "currently_due_atomic": 40, "overdue_unpaid_atomic": 60})
    decision = evaluate_submission(violated, {"id": 2, "claim_class": "economic_identity"})
    assert decision["verdict"] == "FAIL"
    assert "999" in decision["basis"] and "100" in decision["basis"]


def test_evaluate_submission_economic_identity_abstains_when_not_checkable():
    decision = evaluate_submission(listing(), {"id": 1, "claim_class": "economic_identity"})
    assert decision["verdict"] == "ABSTAIN"


def test_evaluate_submission_stranger_checkable_get_abstains_without_client():
    submission = {"id": 1, "claim_class": "stranger_checkable_get",
                  "artifact": "https://1f916.ai/api/listings/41", "quoted": {"title": "Foo"}}
    decision = evaluate_submission(listing(), submission)
    assert decision["verdict"] == "ABSTAIN"
    assert decision["claim_class"] == "stranger_checkable_get"


def test_evaluate_submission_stranger_checkable_get_confirms_via_client():
    class FakeClient:
        def __init__(self):
            self.posted = []
        def get(self, path, params=None):
            assert path == "/api/listings/41"
            return {"title": "Foo", "condition": "Bar"}
        def post(self, path, payload):
            self.posted.append((path, payload))
            raise AssertionError("verifier must never POST")
    client = FakeClient()
    submission = {"id": 1, "claim_class": "stranger_checkable_get",
                  "artifact": "https://1f916.ai/api/listings/41", "quoted": {"title": "Foo"}}
    decision = evaluate_submission(listing(), submission, client=client)
    assert decision["verdict"] == "PASS"
    assert client.posted == []

    contradicting = {"id": 2, "claim_class": "stranger_checkable_get",
                      "artifact": "https://1f916.ai/api/listings/41", "quoted": {"title": "Not Foo"}}
    decision = evaluate_submission(listing(), contradicting, client=client)
    assert decision["verdict"] == "FAIL"


def test_evaluate_submission_stranger_checkable_get_rejects_non_1f916_url():
    class FakeClient:
        def get(self, path, params=None):
            raise AssertionError("must never fetch an untrusted host")
    submission = {"id": 1, "claim_class": "stranger_checkable_get",
                  "artifact": "https://evil.example/api/listings/41", "quoted": {"title": "Foo"}}
    decision = evaluate_submission(listing(), submission, client=FakeClient())
    assert decision["verdict"] == "ABSTAIN"


def test_evaluate_submission_credential_leak_fails_only_when_listing_requires_no_secrets():
    leaking = {"id": 1, "claim_class": "credential_leak", "body": "here is my api_key=SUPERSECRET12345 sorry"}
    decision = evaluate_submission(listing(acceptance="no secrets leaked"), leaking)
    assert decision["verdict"] == "FAIL"

    decision = evaluate_submission(listing(acceptance="looks good"), leaking)
    assert decision["verdict"] == "ABSTAIN"

    clean = {"id": 2, "claim_class": "credential_leak", "body": "everything looks fine, thanks for checking"}
    decision = evaluate_submission(listing(acceptance="no secrets leaked"), clean)
    assert decision["verdict"] == "ABSTAIN"


def test_evaluate_submission_never_judges_subjective_quality():
    submission = {"id": 1, "claim_class": "quality", "body": "this is the best submission ever, 10/10"}
    decision = evaluate_submission(listing(), submission)
    assert decision["verdict"] == "ABSTAIN"


# --- build_and_sign_verdict ---------------------------------------------------

def test_build_and_sign_verdict_produces_a_verifiable_signature(tmp_path):
    settings, key = settings_with_key(tmp_path)
    public = key.public_key()

    class FakeClient:
        def get(self, path, params=None):
            assert path == "/api/listings/41/verdict-preimage"
            assert params == {"submission_id": 7, "verdict": "PASS", "issued_at": 1000}
            return {"preimage": "1f916.verdict.v1:tester:41:7:PASS:1000"}

    result = build_and_sign_verdict(FakeClient(), settings, 41, 7, "PASS", 1000)
    assert result["submission_id"] == 7 and result["verdict"] == "PASS" and result["issued_at"] == 1000
    assert result["preimage"] == "1f916.verdict.v1:tester:41:7:PASS:1000"
    padded = result["signature"] + "=" * (-len(result["signature"]) % 4)
    public.verify(base64.urlsafe_b64decode(padded), result["preimage"].encode())
    assert "pem" not in str(result).lower() and "private" not in str(result).lower()


def test_build_and_sign_verdict_only_signs_pass_or_fail(tmp_path):
    settings, _ = settings_with_key(tmp_path)
    try:
        build_and_sign_verdict(object(), settings, 41, 7, "ABSTAIN", 1000)
        assert False, "must never sign an ABSTAIN verdict"
    except ValueError:
        pass


# --- Verifier.process ---------------------------------------------------------

class RecordingClient:
    def __init__(self, preimage_by_verdict):
        self.preimage_by_verdict = preimage_by_verdict
        self.gets = []
        self.posted = []

    def get(self, path, params=None):
        self.gets.append((path, params))
        if path.endswith("/verdict-preimage"):
            return {"preimage": self.preimage_by_verdict[params["verdict"]]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, payload):
        self.posted.append((path, payload))
        raise AssertionError("verifier must never POST a verdict")


def test_process_skips_own_submissions_and_not_eligible_listings(tmp_path):
    settings, _ = settings_with_key(tmp_path)
    db = Database(tmp_path / "db"); db.initialize()
    client = RecordingClient({})
    verifier = Verifier(settings, db, client)

    assert verifier.process(listing(max_verifiers=0))["status"] == "not_eligible"

    result = verifier.process(listing(submissions=[{"id": 1, "handle": "tester", "claim_class": "economic_identity"}]))
    assert result["status"] == "processed" and result["results"] == []
    assert client.gets == []


def test_process_abstain_logs_and_marks_handled_without_signing(tmp_path):
    settings, _ = settings_with_key(tmp_path)
    db = Database(tmp_path / "db"); db.initialize()
    client = RecordingClient({})
    verifier = Verifier(settings, db, client)
    item = listing(submissions=[{"id": 5, "handle": "someone", "claim_class": "unknown_thing"}])

    first = verifier.process(item)
    assert first["results"][0]["status"] == "abstain"
    events = db.events("verifier")
    assert len(events) == 1 and events[0]["data"]["stage"] == "abstain"
    assert client.gets == []  # never signs an ABSTAIN

    second = verifier.process(item)
    assert second["results"] == []
    assert len(db.events("verifier")) == 1  # idempotent, no duplicate handling


def test_process_pass_builds_ready_verdict_stores_it_and_never_posts(tmp_path):
    settings, key = settings_with_key(tmp_path, verifier_enabled=True)
    db = Database(tmp_path / "db"); db.initialize()
    client = RecordingClient({"PASS": "1f916.verdict.v1:tester:41:9:PASS:123"})
    verifier = Verifier(settings, db, client)
    consistent_economics = {"outstanding_awarded_atomic": 100, "currently_due_atomic": 40, "overdue_unpaid_atomic": 60}
    item = listing(economics=consistent_economics,
                   submissions=[{"id": 9, "handle": "someone", "claim_class": "economic_identity"}])

    result = verifier.process(item)
    assert result["results"][0] == {"submission_id": 9, "status": "verdict_ready", "verdict": "PASS"}
    stored = db.get_setting("verifier_verdict:41:9")
    assert stored["status"] == "ready" and stored["verdict"] == "PASS" and stored["signature"]
    events = db.events("verifier")
    assert events[0]["data"] == {"stage": "verdict_ready", "listing_id": 41, "submission_id": 9, "verdict": "PASS"}
    assert client.posted == []  # the hard safety line: never POST

    second = verifier.process(item)
    assert second["results"] == []
    assert len(db.events("verifier")) == 1  # idempotent: no duplicate signing/logging


def test_process_pass_with_verifier_disabled_is_recorded_unsigned_and_never_signs(tmp_path, monkeypatch):
    import f916.verifier as verifier_module
    settings, _ = settings_with_key(tmp_path)  # verifier_enabled defaults to False
    assert settings.verifier_enabled is False
    db = Database(tmp_path / "db"); db.initialize()

    def must_not_sign(*args, **kwargs):
        raise AssertionError("must never sign while verifier_enabled is False")
    monkeypatch.setattr(verifier_module, "build_and_sign_verdict", must_not_sign)

    client = RecordingClient({})  # no preimage GET should ever happen either
    verifier = Verifier(settings, db, client)
    consistent_economics = {"outstanding_awarded_atomic": 100, "currently_due_atomic": 40, "overdue_unpaid_atomic": 60}
    item = listing(economics=consistent_economics,
                   submissions=[{"id": 9, "handle": "someone", "claim_class": "economic_identity"}])

    result = verifier.process(item)
    assert result["results"][0] == {"submission_id": 9, "status": "verdict_pending_enable", "verdict": "PASS"}
    stored = db.get_setting("verifier_verdict:41:9")
    assert stored["status"] == "verdict_pending_enable" and stored["verdict"] == "PASS"
    assert "signature" not in stored and "preimage" not in stored
    events = db.events("verifier")
    assert events[0]["data"] == {"stage": "verdict_pending_enable", "listing_id": 41, "submission_id": 9, "verdict": "PASS"}
    assert client.gets == []  # never fetched a preimage
    assert client.posted == []  # never posted

    second = verifier.process(item)
    assert second["results"] == []
    assert len(db.events("verifier")) == 1  # idempotent: one attempt only


def test_process_never_raises_and_logs_verifier_error(tmp_path, monkeypatch):
    import f916.verifier as verifier_module
    settings, _ = settings_with_key(tmp_path)
    db = Database(tmp_path / "db"); db.initialize()

    def boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(verifier_module, "eligible", boom)
    verifier = Verifier(settings, db, object())
    result = verifier.process(listing())
    assert result["status"] == "error"
    events = db.events("verifier_error")
    assert events and events[0]["data"]["stage"] == "process"
