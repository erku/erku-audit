"""Task P -- LLM-authored `project_required` projects.

The model may author a small project's files, but that output is DATA that
is NEVER executed by this process: it is written through the builder's
existing safety gates, its tests run ONLY in the isolated `SandboxClient`,
and it is published + submitted ONLY if those tests pass. The whole path is
inert unless BOTH the broker is configured AND `project_llm_enabled` is
true. Everything here is mocked -- no real LLM/sandbox/broker/network call.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from f916.brain import Brain
from f916.builder import MAX_FILE_BYTES, MAX_FILES, build_project_from_files
from f916.config import Settings
from f916.db import Database
from f916.opportunities import OpportunityRunner

FIXED_NOW = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# f916.builder.build_project_from_files
# ---------------------------------------------------------------------------
def llm_spec(**updates):
    base = {
        "name": "erku-1f916-tool-77",
        "template": "python-tool",
        "title": "Audit Tool",
        "description": "LLM-authored tool.",
        "listing_id": 77,
        "grant_slug": None,
        "source_hash": "c" * 64,
        "content": {"listing_id": 77},
        "needs_llm": True,
    }
    return base | updates


def valid_files(**updates):
    base = {
        "README.md": "# Audit Tool\n",
        "src/tool/__init__.py": "def run():\n    return 1\n",
        "tests/test_tool.py": "def test_run_returns_one():\n    assert True\n",
    }
    return base | updates


def test_build_project_from_files_writes_manifest_with_supplied_files(tmp_path):
    manifest = build_project_from_files(llm_spec(), valid_files(), tmp_path, now=FIXED_NOW)
    project_dir = tmp_path / "erku-1f916-tool-77"
    assert manifest["name"] == "erku-1f916-tool-77"
    assert manifest["manifest_version"] == "1f916.project.v1"
    assert manifest["executes_community_commands"] is False
    paths = {f["path"] for f in manifest["files"]}
    assert paths == set(valid_files())
    assert (project_dir / "tests" / "test_tool.py").exists()
    on_disk = json.loads((project_dir / "project.manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest


def test_build_project_from_files_enforces_name_prefix(tmp_path):
    with pytest.raises(ValueError):
        build_project_from_files(llm_spec(name="not-prefixed"), valid_files(), tmp_path, now=FIXED_NOW)


def test_build_project_from_files_rejects_no_tests_directory(tmp_path):
    files = {"README.md": "# Tool\n", "src/tool/__init__.py": "def run():\n    return 1\n"}
    with pytest.raises(ValueError, match="no_tests"):
        build_project_from_files(llm_spec(), files, tmp_path, now=FIXED_NOW)


def test_build_project_from_files_enforces_secret_scan(tmp_path):
    files = valid_files(**{"config.py": "API_KEY=sk-ABCDEFGHIJKLMNOPQRSTUVWX1234\n"})
    with pytest.raises(ValueError, match="secret_like_content"):
        build_project_from_files(llm_spec(), files, tmp_path, now=FIXED_NOW)


def test_build_project_from_files_enforces_path_traversal(tmp_path):
    files = valid_files(**{"../escape.py": "x = 1\n"})
    with pytest.raises(ValueError):
        build_project_from_files(llm_spec(), files, tmp_path, now=FIXED_NOW)


def test_build_project_from_files_enforces_max_file_count(tmp_path):
    files = {f"f{i}.txt": "x" for i in range(MAX_FILES)}
    files["tests/test_x.py"] = "def test_ok():\n    assert True\n"
    with pytest.raises(ValueError):
        build_project_from_files(llm_spec(), files, tmp_path, now=FIXED_NOW)


def test_build_project_from_files_enforces_max_file_size(tmp_path):
    files = valid_files(**{"big.txt": "x" * (MAX_FILE_BYTES + 1)})
    with pytest.raises(ValueError):
        build_project_from_files(llm_spec(), files, tmp_path, now=FIXED_NOW)


def test_build_project_from_files_is_reproducible(tmp_path):
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    ws1.mkdir(); ws2.mkdir()
    files = valid_files()
    manifest1 = build_project_from_files(llm_spec(), dict(files), ws1, now=FIXED_NOW)
    manifest2 = build_project_from_files(llm_spec(), dict(files), ws2, now=FIXED_NOW)
    assert manifest1["tree_hash"] == manifest2["tree_hash"]
    m1 = {k: v for k, v in manifest1.items() if k != "created_utc"}
    m2 = {k: v for k, v in manifest2.items() if k != "created_utc"}
    assert m1 == m2


def test_build_project_from_files_never_executes_supplied_content(tmp_path):
    # A file that would blow up if it were ever imported/executed must still
    # build cleanly: build_project_from_files only writes bytes to disk.
    files = valid_files(**{"src/tool/__init__.py": "raise RuntimeError('should never run')\n"})
    manifest = build_project_from_files(llm_spec(), files, tmp_path, now=FIXED_NOW)
    assert manifest["name"] == "erku-1f916-tool-77"


# ---------------------------------------------------------------------------
# Brain.generate_project
# ---------------------------------------------------------------------------
def project_spec_payload():
    return {"title": "Audit Tool", "description": "Build a small audit tool.",
            "content": {"listing_id": 70}}


def test_generate_project_returns_bounded_files_on_success(tmp_path):
    files = {"README.md": "# Tool\n", "tests/test_tool.py": "def test_ok():\n    assert True\n"}
    def handler(request):
        return httpx.Response(200, json={
            "message": {"content": json.dumps({"files": files})},
            "prompt_eval_count": 5, "eval_count": 5,
        })
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    result = brain.generate_project(project_spec_payload())
    assert result == {"files": files}
    assert db.get_setting("llm_usage")["tokens"] == 10


def test_generate_project_coerces_scalars_and_drops_non_string_entries(tmp_path):
    raw_files = {
        "tests/test_tool.py": "def test_ok():\n    assert True\n",
        "count.txt": 42,
        "flag.txt": True,
        "bad.json": {"a": 1},
        "also_bad.txt": None,
    }
    def handler(request):
        return httpx.Response(200, json={
            "message": {"content": json.dumps({"files": raw_files})},
            "prompt_eval_count": 1, "eval_count": 1,
        })
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    result = brain.generate_project(project_spec_payload())
    assert result["files"]["tests/test_tool.py"] == "def test_ok():\n    assert True\n"
    assert result["files"]["count.txt"] == "42"
    assert result["files"]["flag.txt"] == "True"
    assert "bad.json" not in result["files"]
    assert "also_bad.txt" not in result["files"]


def test_generate_project_rejects_too_many_files(tmp_path):
    files = {f"f{i}.py": "x" for i in range(MAX_FILES + 1)}
    files["tests/test_x.py"] = "def test_ok():\n    assert True\n"
    def handler(request):
        return httpx.Response(200, json={
            "message": {"content": json.dumps({"files": files})},
            "prompt_eval_count": 1, "eval_count": 1,
        })
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_project(project_spec_payload()) == {}


def test_generate_project_rejects_oversized_file(tmp_path):
    files = {"tests/test_x.py": "def test_ok():\n    assert True\n", "big.txt": "x" * (MAX_FILE_BYTES + 1)}
    def handler(request):
        return httpx.Response(200, json={
            "message": {"content": json.dumps({"files": files})},
            "prompt_eval_count": 1, "eval_count": 1,
        })
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_project(project_spec_payload()) == {}


def test_generate_project_rejects_malformed_files_shape(tmp_path):
    def handler(request):
        return httpx.Response(200, json={
            "message": {"content": json.dumps({"files": "not-an-object"})},
            "prompt_eval_count": 1, "eval_count": 1,
        })
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_project(project_spec_payload()) == {}


def test_generate_project_never_raises_on_non_json_content(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"message": {"content": "not json at all"},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_project(project_spec_payload()) == {}


def test_generate_project_never_raises_on_http_error(tmp_path):
    def handler(request):
        return httpx.Response(500)
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_project(project_spec_payload()) == {}


def test_generate_project_blocked_by_budget_never_makes_a_request(tmp_path):
    called = False
    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(500)
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = Settings(data_dir=tmp_path, llm_daily_budget_usd=0.000001, llm_token_limits_enabled=True,
                         llm_input_usd_per_million=1, llm_output_usd_per_million=1)
    brain = Brain(settings, db, transport=httpx.MockTransport(handler))
    assert brain.generate_project(project_spec_payload()) == {}
    assert not called


def test_generate_project_blocked_while_rate_limited_never_makes_a_request(tmp_path):
    called = False
    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(500)
    db = Database(tmp_path / "state.db"); db.initialize()
    db.set_setting("llm_retry_state", {"blocked_until": 9999999999.0})
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_project(project_spec_payload()) == {}
    assert not called


# ---------------------------------------------------------------------------
# OpportunityRunner.run_project -- LLM branch
# ---------------------------------------------------------------------------
def llm_listing(**updates):
    base = {
        "listing_id": 70,
        "title": "Build a small audit tool",
        "condition": "Implement a fix and open a pull request for the audit tool.",
        "payload_hash": "f" * 64,
    }
    return base | updates


def broker_settings(tmp_path, **updates):
    return Settings(data_dir=tmp_path, broker_url="https://broker.internal.test",
                     broker_token="worker-broker-secret-do-not-leak",
                     project_llm_enabled=True, **updates)


class NeverCalled:
    """Blows up on ANY attribute access, call, or construction -- used to
    assert a collaborator is never touched on a given code path."""

    def __getattr__(self, name):
        raise AssertionError(f"unexpected call: {name}")

    def __call__(self, *args, **kwargs):
        raise AssertionError("unexpected construction/call")


class FakeBrain:
    def __init__(self, files=None):
        self.files = files
        self.calls = []

    def generate_project(self, spec):
        self.calls.append(spec)
        if self.files is None:
            return {}
        return {"files": dict(self.files)}


class FakeSandboxClient:
    def __init__(self, settings, *, passed=True):
        self.settings = settings
        self.passed = passed
        self.calls = []

    def run(self, project_name, files, *, test_path="."):
        self.calls.append({"project_name": project_name, "files": dict(files), "test_path": test_path})
        return {"passed": self.passed, "timed_out": False}


class SandboxFactory:
    def __init__(self, passed=True):
        self.passed = passed
        self.instances = []

    def __call__(self, settings):
        instance = FakeSandboxClient(settings, passed=self.passed)
        self.instances.append(instance)
        return instance


class FakeBrokerClient:
    def __init__(self, base_url, token):
        self.base_url, self.token = base_url, token
        self.repos_created = []
        self.published = []

    def create_repo(self, name):
        self.repos_created.append(name)
        return {"name": name, "html_url": f"https://github.com/erku-test/{name}", "default_branch": "main"}

    def publish(self, name, manifest, file_contents):
        self.published.append({"name": name, "tree_hash": manifest["tree_hash"], "file_contents": dict(file_contents)})
        return {"repo": name, "commits": [{"path": p} for p in file_contents]}


class BrokerFactory:
    def __init__(self, cls=FakeBrokerClient):
        self.cls = cls
        self.instances = []

    def __call__(self, base_url, token):
        instance = self.cls(base_url, token)
        self.instances.append(instance)
        return instance


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


def make_runner(settings, db, *, brain, sandbox_client_cls, broker_client_cls=None,
                 executor=None, publisher=None, seal_api=None):
    return OpportunityRunner(
        settings, db, seal_api or FakeSealAPI(), executor or FakeExecutor(),
        publisher or FakePublisher(), sealer=fake_sealer,
        broker_client_cls=broker_client_cls if broker_client_cls is not None else NeverCalled(),
        brain=brain, sandbox_client_cls=sandbox_client_cls,
    )


TOOL_FILES = {"README.md": "# Tool\n", "tests/test_tool.py": "def test_ok():\n    assert True\n"}


def test_llm_branch_inert_when_project_llm_enabled_is_false(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = Settings(data_dir=tmp_path, broker_url="https://broker.internal.test",
                         broker_token="worker-broker-secret-do-not-leak", project_llm_enabled=False)
    runner = OpportunityRunner(settings, db, NeverCalled(), NeverCalled(), NeverCalled(),
                                broker_client_cls=NeverCalled(), brain=NeverCalled(),
                                sandbox_client_cls=NeverCalled())

    result = runner.process(llm_listing())

    assert result["status"] == "project_llm_disabled"
    assert not (Path(settings.data_dir) / "projects").exists()


def test_llm_branch_happy_path_builds_tests_publishes_and_submits(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    brain = FakeBrain(files=TOOL_FILES)
    sandbox_factory = SandboxFactory(passed=True)
    broker_factory = BrokerFactory()
    executor = FakeExecutor()
    publisher = FakePublisher()
    seal_api = FakeSealAPI()
    runner = make_runner(settings, db, brain=brain, sandbox_client_cls=sandbox_factory,
                          broker_client_cls=broker_factory, executor=executor,
                          publisher=publisher, seal_api=seal_api)

    result = runner.process(llm_listing())

    assert result["status"] == "project_submitted"
    assert len(brain.calls) == 1
    assert len(sandbox_factory.instances) == 1
    sandbox_call = sandbox_factory.instances[0].calls[0]
    assert sandbox_call["project_name"] == "erku-1f916-tool-70"
    assert sandbox_call["test_path"] == "tests"
    assert set(sandbox_call["files"]) == set(TOOL_FILES)
    assert len(broker_factory.instances) == 1
    broker = broker_factory.instances[0]
    assert broker.repos_created == ["erku-1f916-tool-70"]
    assert len(broker.published) == 1
    assert set(broker.published[0]["file_contents"]) == set(TOOL_FILES)
    assert publisher.calls, "evidence artifact must be published"
    assert seal_api.calls, "evidence artifact must be sealed"
    assert len(executor.dispatched) == 1
    assert executor.dispatched[0].action == "submit"
    project_dir = Path(settings.data_dir) / "projects" / "erku-1f916-tool-70"
    assert (project_dir / "tests" / "test_tool.py").exists()


def test_llm_branch_failed_sandbox_tests_never_publishes_or_submits(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    brain = FakeBrain(files={"tests/test_tool.py": "def test_fail():\n    assert False\n"})
    sandbox_factory = SandboxFactory(passed=False)
    runner = make_runner(settings, db, brain=brain, sandbox_client_cls=sandbox_factory,
                          broker_client_cls=NeverCalled(), executor=NeverCalled(),
                          publisher=NeverCalled(), seal_api=NeverCalled())

    result = runner.process(llm_listing(listing_id=71, payload_hash="1" * 64))

    assert result["status"] == "project_tests_failed"
    assert len(sandbox_factory.instances) == 1


def test_llm_branch_secret_laden_generated_file_is_rejected_before_sandbox(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    files = {
        "tests/test_tool.py": "def test_ok():\n    assert True\n",
        "config.py": "API_KEY=sk-ABCDEFGHIJKLMNOPQRSTUVWX1234\n",
    }
    brain = FakeBrain(files=files)
    runner = make_runner(settings, db, brain=brain, sandbox_client_cls=NeverCalled(),
                          broker_client_cls=NeverCalled(), executor=NeverCalled(),
                          publisher=NeverCalled(), seal_api=NeverCalled())

    result = runner.process(llm_listing(listing_id=72, payload_hash="2" * 64))

    assert result["status"] == "project_build_rejected"


def test_llm_branch_empty_generation_is_terminal(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    brain = FakeBrain(files=None)
    runner = make_runner(settings, db, brain=brain, sandbox_client_cls=NeverCalled(),
                          broker_client_cls=NeverCalled(), executor=NeverCalled(),
                          publisher=NeverCalled(), seal_api=NeverCalled())

    result = runner.process(llm_listing(listing_id=73, payload_hash="3" * 64))

    assert result["status"] == "project_generation_empty"


def test_llm_branch_one_attempt_dedup(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    brain = FakeBrain(files=TOOL_FILES)
    sandbox_factory = SandboxFactory(passed=True)
    broker_factory = BrokerFactory()
    executor = FakeExecutor()
    runner = make_runner(settings, db, brain=brain, sandbox_client_cls=sandbox_factory,
                          broker_client_cls=broker_factory, executor=executor)

    first = runner.process(llm_listing(listing_id=74, payload_hash="4" * 64))
    second = runner.process(llm_listing(listing_id=74, payload_hash="4" * 64))

    assert first["status"] == "project_submitted"
    assert second["status"] == "project_submitted"
    assert len(brain.calls) == 1
    assert len(sandbox_factory.instances) == 1
    assert len(broker_factory.instances) == 1
    assert len(executor.dispatched) == 1


def test_llm_branch_uncertain_submit_is_terminal_and_never_retried(tmp_path):
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    brain = FakeBrain(files=TOOL_FILES)
    sandbox_factory = SandboxFactory(passed=True)
    broker_factory = BrokerFactory()
    executor = FakeExecutor(result={"status": "uncertain"})
    runner = make_runner(settings, db, brain=brain, sandbox_client_cls=sandbox_factory,
                          broker_client_cls=broker_factory, executor=executor)

    first = runner.process(llm_listing(listing_id=75, payload_hash="5" * 64))
    second = runner.process(llm_listing(listing_id=75, payload_hash="5" * 64))

    assert first["status"] == "project_uncertain"
    assert second["status"] == "project_uncertain"
    assert len(executor.dispatched) == 1
    assert len(brain.calls) == 1
    assert len(sandbox_factory.instances) == 1


def test_llm_branch_deterministic_path_is_unaffected(tmp_path):
    # A non-needs_llm project_required listing must still take the exact
    # original B1 deterministic path (rail-window-report template) -- brain
    # and sandbox are never touched.
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = broker_settings(tmp_path)
    broker_factory = BrokerFactory()
    executor = FakeExecutor()
    runner = make_runner(settings, db, brain=NeverCalled(), sandbox_client_cls=NeverCalled(),
                          broker_client_cls=broker_factory, executor=executor)
    listing = {
        "listing_id": 90,
        "title": "Fix the rail window tracker repository",
        "condition": "Implement a fix and open a pull request for the rail window dashboard.",
        "payload_hash": "9" * 64,
        "funder": "acme",
    }

    result = runner.process(listing)

    assert result["status"] == "project_submitted"
    assert broker_factory.instances[0].repos_created == ["erku-1f916-rail-window-90"]
