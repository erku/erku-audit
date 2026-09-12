"""Full autopilot integration tests for the Task B1 project pipeline: a
project_required listing is qualified against the operator's template
allowlist, built by f916.builder, "published" to a repo through a FAKE
broker client, evidenced+sealed through the existing (faked) audit
publisher/sealer, and submitted through a fake executor -- with NO real
network call anywhere in the process.
"""
import json
from pathlib import Path

from f916.config import Settings
from f916.db import Database
from f916.opportunities import OpportunityRunner

REPO_HTML_URL_PREFIX = "https://github.com/erku-test/"


def project_listing(**updates):
    base = {
        "listing_id": 55,
        "title": "Fix the rail window tracker repository",
        "condition": "Implement a fix and open a pull request for the rail window dashboard.",
        "payload_hash": "e" * 64,
        "funder": "acme",
        "economics": {"available_award_capacity": 1},
    }
    return base | updates


def broker_settings(tmp_path, **updates):
    return Settings(data_dir=tmp_path, broker_url="https://broker.internal.test",
                     broker_token="worker-broker-secret-do-not-leak", **updates)


class FakeBrokerClient:
    """Stands in for f916.broker_client.BrokerClient. No network I/O."""

    def __init__(self, base_url, token):
        self.base_url, self.token = base_url, token
        self.repos_created = []
        self.published = []

    def create_repo(self, name):
        self.repos_created.append(name)
        return {"name": name, "html_url": f"{REPO_HTML_URL_PREFIX}{name}", "default_branch": "main"}

    def publish(self, name, manifest, file_contents):
        self.published.append({"name": name, "tree_hash": manifest["tree_hash"], "file_contents": dict(file_contents)})
        return {"repo": name, "commits": [{"path": p} for p in file_contents]}


class UncertainPublishBrokerClient(FakeBrokerClient):
    def publish(self, name, manifest, file_contents):
        super().publish(name, manifest, file_contents)
        raise TimeoutError("broker publish outcome unknown")


class BrokerFactory:
    """Records every BrokerClient construction so tests can assert it is
    only ever constructed once per successful (or terminally-uncertain)
    listing -- i.e. the broker is never called again once run_project has
    reached a terminal state."""

    def __init__(self, cls=FakeBrokerClient):
        self.cls = cls
        self.instances = []

    def __call__(self, base_url, token):
        instance = self.cls(base_url, token)
        self.instances.append(instance)
        return instance


class NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected call: {name}")

    def __call__(self, *args, **kwargs):
        raise AssertionError("unexpected construction/call of a broker client")


class FakePublisher:
    def __init__(self):
        self.calls = []

    def publish(self, artifact):
        self.calls.append(artifact["hash"])
        digest, commit = artifact["hash"], "f" * 40
        return {**artifact, "commit": commit,
                "public_url": f"https://github.com/erku/erku-audit/blob/{commit}/artifacts/{digest}.json"}


class FakeSealAPI:
    def __init__(self):
        self.calls = []

    def post(self, path, payload):
        self.calls.append(payload["hash"])
        return {"id": 501}


def fake_sealer(client, settings, digest, label):
    return client.post("/api/seal", {"hash": digest, "label": label})


class FakeExecutor:
    def __init__(self, result=None):
        self.dispatched = []
        self.result = result or {"status": "sent", "response": {"id": 1}}

    def dispatch(self, intent):
        self.dispatched.append(intent)
        return self.result


def make_runner(settings, db, *, broker_client_cls=None, executor=None, publisher=None, seal_api=None):
    return OpportunityRunner(
        settings, db, seal_api or FakeSealAPI(), executor or FakeExecutor(),
        publisher or FakePublisher(), sealer=fake_sealer,
        **({"broker_client_cls": broker_client_cls} if broker_client_cls is not None else {}),
    )


# ---------------------------------------------------------------------------
# Full happy path
# ---------------------------------------------------------------------------
def test_full_autopilot_qualifies_builds_publishes_evidences_and_submits(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    factory = BrokerFactory()
    seal_api = FakeSealAPI()
    publisher = FakePublisher()
    executor = FakeExecutor()
    runner = make_runner(settings, db, broker_client_cls=factory, executor=executor,
                          publisher=publisher, seal_api=seal_api)

    result = runner.process(project_listing())

    assert result["status"] == "project_submitted"
    assert len(factory.instances) == 1
    broker = factory.instances[0]
    assert broker.repos_created == ["erku-1f916-rail-window-55"]
    assert len(broker.published) == 1
    published_files = broker.published[0]["file_contents"]
    assert set(published_files) == {"README.md", "LICENSE", "index.html", "data/report.json", "tests/test_report.py"}
    # Content is derived only from the listing's own structured fields.
    report = json.loads(published_files["data/report.json"])
    assert report["fields"]["listing_id"] == 55
    assert report["fields"]["funder"] == "acme"
    assert "Implement a fix" not in published_files["data/report.json"]
    assert "Implement a fix" not in published_files["index.html"]

    assert publisher.calls, "evidence artifact must be published"
    assert seal_api.calls, "evidence artifact must be sealed"

    assert len(executor.dispatched) == 1
    intent = executor.dispatched[0]
    assert intent.action == "submit"
    assert intent.listing_id == 55
    assert f"{REPO_HTML_URL_PREFIX}erku-1f916-rail-window-55" in intent.artifact
    assert "sha256:" in intent.artifact and "commit:" in intent.artifact and "seal:501" in intent.artifact

    # The manifest was actually written to disk under the project workspace.
    project_dir = Path(settings.data_dir) / "projects" / "erku-1f916-rail-window-55"
    manifest = json.loads((project_dir / "project.manifest.json").read_text(encoding="utf-8"))
    assert manifest["listing_id"] == 55
    assert manifest["name"] == "erku-1f916-rail-window-55"

    # The quota tracker recorded the new repo.
    assert db.get_setting("project_repos", []) == ["erku-1f916-rail-window-55"]


# ---------------------------------------------------------------------------
# Inert when broker is not configured
# ---------------------------------------------------------------------------
def test_inert_when_broker_url_and_token_are_unset(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = Settings(data_dir=tmp_path)  # broker_url == '' and broker_token == '' by default
    assert not settings.broker_url and not settings.broker_token

    class Bomb:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected call: {name}")

    runner = OpportunityRunner(settings, db, Bomb(), Bomb(), Bomb())
    result = runner.process(project_listing())

    assert result["status"] == "project_required"
    assert not (Path(settings.data_dir) / "projects").exists()
    assert not (Path(settings.data_dir) / "artifacts").exists()


def test_inert_when_only_one_of_broker_url_or_token_is_set(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()

    class Bomb:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected call: {name}")

    only_url = Settings(data_dir=tmp_path, broker_url="https://broker.internal.test")
    runner = OpportunityRunner(only_url, db, Bomb(), Bomb(), Bomb())
    assert runner.process(project_listing(listing_id=61, payload_hash="1" * 64))["status"] == "project_required"

    only_token = Settings(data_dir=tmp_path, broker_token="some-token")
    runner = OpportunityRunner(only_token, db, Bomb(), Bomb(), Bomb())
    assert runner.process(project_listing(listing_id=62, payload_hash="2" * 64))["status"] == "project_required"


# ---------------------------------------------------------------------------
# One-attempt dedup
# ---------------------------------------------------------------------------
def test_one_attempt_dedup_on_repeated_process_calls(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    factory = BrokerFactory()
    executor = FakeExecutor()
    runner = make_runner(settings, db, broker_client_cls=factory, executor=executor)

    first = runner.process(project_listing())
    second = runner.process(project_listing())

    assert first["status"] == "project_submitted"
    assert second["status"] == "project_submitted"
    assert len(factory.instances) == 1
    assert len(factory.instances[0].repos_created) == 1
    assert len(factory.instances[0].published) == 1
    assert len(executor.dispatched) == 1


# ---------------------------------------------------------------------------
# Uncertain outcomes are terminal and never retried
# ---------------------------------------------------------------------------
def test_uncertain_broker_publish_is_terminal_and_never_retried(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    factory = BrokerFactory(cls=UncertainPublishBrokerClient)
    executor = FakeExecutor()
    runner = make_runner(settings, db, broker_client_cls=factory, executor=executor)

    first = runner.process(project_listing())
    second = runner.process(project_listing())

    assert first["status"] == "project_uncertain"
    assert second["status"] == "project_uncertain"
    assert len(factory.instances) == 1  # never reconstructed / retried
    assert len(factory.instances[0].published) == 1  # publish attempted exactly once
    assert executor.dispatched == []  # never reached submission


def test_uncertain_submit_is_terminal_and_never_retried(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    factory = BrokerFactory()
    executor = FakeExecutor(result={"status": "uncertain"})
    runner = make_runner(settings, db, broker_client_cls=factory, executor=executor)

    first = runner.process(project_listing())
    second = runner.process(project_listing())

    assert first["status"] == "project_uncertain"
    assert second["status"] == "project_uncertain"
    assert len(factory.instances) == 1
    assert len(executor.dispatched) == 1  # submit attempted exactly once, never repeated


# ---------------------------------------------------------------------------
# Unqualified listings never build or call the broker
# ---------------------------------------------------------------------------
def test_unqualified_listing_records_terminal_state_without_building(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    non_matching = project_listing(title="Fix the tracker repository")  # no "window" substring
    runner = OpportunityRunner(settings, db, NeverCalled(), NeverCalled(), NeverCalled(),
                                broker_client_cls=NeverCalled())

    result = runner.process(non_matching)

    assert result["status"] == "project_unqualified"
    assert not (Path(settings.data_dir) / "projects").exists()


def test_over_quota_listing_records_unqualified_without_building(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    db.set_setting("project_repos", ["erku-1f916-rail-window-1", "erku-1f916-rail-window-2", "erku-1f916-rail-window-3"])
    settings = broker_settings(tmp_path, max_projects=3)
    runner = OpportunityRunner(settings, db, NeverCalled(), NeverCalled(), NeverCalled(),
                                broker_client_cls=NeverCalled())

    result = runner.process(project_listing())

    assert result["status"] == "project_unqualified"
    assert not (Path(settings.data_dir) / "projects").exists()


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------
def test_broker_token_never_appears_in_any_logged_event(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    factory = BrokerFactory()
    runner = make_runner(settings, db, broker_client_cls=factory)

    runner.process(project_listing())

    for event in db.events(limit=1000):
        assert settings.broker_token not in json.dumps(event)
