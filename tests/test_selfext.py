"""f916.selfext -- the self-extension engine.

Tier 1 (auto-templates) only routes a gap's class to an EXISTING
allowlisted pure skill, bounded by a hard cap. Tier 2 (skill proposals)
generates candidate skills via a (fake, in these tests) Brain, AST-scans
them, and runs their tests ONLY in the (fake, in these tests) isolated
sandbox -- the generated source is DATA that this process never imports or
execs, and nothing here merges or redeploys live code. No real LLM,
sandbox, or network call is made anywhere in this file.
"""
import inspect
import json
import sys
from pathlib import Path

import pytest

from f916 import selfext
from f916.config import Settings
from f916.db import Database
from f916.skills import SKILLS

PURE_SOURCE = """def check_rail_thing(target, params):
    value = target.get('value', 0)
    return {'status': 'consistent', 'value': value}
"""

IMPURE_SOURCE = """import os


def check_rail_thing(target, params):
    return {'status': 'consistent', 'path': os.getcwd()}
"""

TESTS_SOURCE = """def test_ok():
    assert True
"""


def make_settings(tmp_path, **overrides):
    return Settings(data_dir=tmp_path / "data", **overrides)


def make_db(tmp_path):
    db = Database(tmp_path / "state.db")
    db.initialize()
    return db


class FakeBrain:
    def __init__(self, result=None):
        self.result = result if result is not None else {}
        self.calls = []

    def generate_skill(self, gap):
        self.calls.append(gap)
        return dict(self.result) if self.result else {}


def make_sandbox_client_cls(passed=True):
    calls = []

    class FakeSandboxClient:
        def __init__(self, settings):
            self.settings = settings

        def run(self, project_name, files, *, test_path="."):
            calls.append({"project_name": project_name, "files": dict(files), "test_path": test_path})
            return {"passed": passed}

    FakeSandboxClient.calls = calls
    return FakeSandboxClient


def template_gap(class_key="acme|rail derivation", funder="acme",
                  sample_title="Break the rail derivation totals"):
    return {"class_key": class_key, "funder": funder, "sample_title": sample_title, "suggestion": "template"}


def skill_gap(class_key="acme|novel class", funder="acme", sample_title="A brand new bounty class"):
    return {"class_key": class_key, "funder": funder, "sample_title": sample_title, "suggestion": "skill"}


# ---------------------------------------------------------------------------
# Tier 1 -- auto-templates (written ONLY to config/auto_templates.json;
# config/bounty_templates.json, the operator-curated allowlist, is never
# read or written by this engine)
# ---------------------------------------------------------------------------
def test_tier1_adds_template_to_auto_store_not_operator_file(tmp_path, monkeypatch):
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)

    settings = make_settings(tmp_path, self_extend_max_templates=8)
    db = make_db(tmp_path)
    brain = FakeBrain()

    summary = selfext.act_on_gaps([template_gap()], settings, db, brain)

    assert summary == {"templates_added": 1, "proposals": 0}
    auto_templates = json.loads(auto_path.read_text(encoding="utf-8"))
    assert isinstance(auto_templates, list) and len(auto_templates) == 1
    added = auto_templates[0]
    assert added["skill"] in SKILLS
    assert added["skill"] == "rail-derivation-check"
    assert added["builder"] == "rail_derivation_check"
    assert isinstance(added["title_contains"], list) and added["title_contains"]

    events = db.events("self_extend")
    assert events[0]["data"] == {"tier": "template", "class_key": "acme|rail derivation",
                                  "skill": "rail-derivation-check", "status": "added"}


def test_tier1_never_reads_or_writes_the_operator_bounty_templates_file(tmp_path, monkeypatch):
    """Hard safety line: config/bounty_templates.json is operator-curated and
    must never be mutated by the engine. Uses the REAL operator file path
    (only the separate auto store is redirected to a temp file) and checks
    it is byte-for-byte unchanged, still holding exactly the 5 curated
    templates, after the engine adds an auto template."""
    from f916.templates import _DEFAULT_PATH

    before = _DEFAULT_PATH.read_text(encoding="utf-8")
    before_entries = json.loads(before)
    assert len(before_entries) == 5

    auto_path = tmp_path / "auto_templates.json"
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)
    settings = make_settings(tmp_path, self_extend_max_templates=8)
    db = make_db(tmp_path)

    summary = selfext.act_on_gaps([template_gap()], settings, db, FakeBrain())

    assert summary == {"templates_added": 1, "proposals": 0}
    after = _DEFAULT_PATH.read_text(encoding="utf-8")
    assert after == before  # byte-for-byte untouched
    assert len(json.loads(after)) == 5
    # The new template landed in the separate auto store instead.
    assert len(json.loads(auto_path.read_text(encoding="utf-8"))) == 1


def test_tier1_routes_quote_stranger_census_to_rail_report(tmp_path, monkeypatch):
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)
    settings = make_settings(tmp_path, self_extend_max_templates=8)
    db = make_db(tmp_path)

    gap = template_gap(class_key="acme|award slot census", sample_title="Award slot census")
    selfext.act_on_gaps([gap], settings, db, FakeBrain())
    auto_templates = json.loads(auto_path.read_text(encoding="utf-8"))
    assert auto_templates[0]["skill"] == "rail-report"
    assert auto_templates[0]["builder"] == "rail_self_report"


def test_tier1_routes_cadence_to_batch_cadence(tmp_path, monkeypatch):
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)
    settings = make_settings(tmp_path, self_extend_max_templates=8)
    db = make_db(tmp_path)

    gap = template_gap(class_key="acme|settlement cadence", sample_title="Settlement batch cadence")
    selfext.act_on_gaps([gap], settings, db, FakeBrain())
    auto_templates = json.loads(auto_path.read_text(encoding="utf-8"))
    assert auto_templates[0]["skill"] == "batch-cadence"
    assert auto_templates[0]["builder"] == "batch_cadence"


def test_tier1_skips_when_no_existing_skill_fits(tmp_path, monkeypatch):
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)
    settings = make_settings(tmp_path, self_extend_max_templates=8)
    db = make_db(tmp_path)

    gap = template_gap(class_key="acme|completely unrelated class", sample_title="Nothing matches here")
    summary = selfext.act_on_gaps([gap], settings, db, FakeBrain())

    assert summary == {"templates_added": 0, "proposals": 0}
    assert json.loads(auto_path.read_text(encoding="utf-8")) == []
    assert db.events("self_extend")[0]["data"]["status"] == "no_existing_skill"


def test_tier1_skipped_when_over_cap(tmp_path, monkeypatch):
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text(json.dumps([{"id": "existing", "skill": "rail-report",
                                       "builder": "rail_self_report", "title_contains": ["x"]}]),
                          encoding="utf-8")
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)
    settings = make_settings(tmp_path, self_extend_max_templates=1)
    db = make_db(tmp_path)

    summary = selfext.act_on_gaps([template_gap()], settings, db, FakeBrain())

    assert summary == {"templates_added": 0, "proposals": 0}
    auto_templates = json.loads(auto_path.read_text(encoding="utf-8"))
    assert len(auto_templates) == 1  # unchanged
    assert db.events("self_extend")[0]["data"]["status"] == "capped"


def test_tier1_is_idempotent_per_class_key(tmp_path, monkeypatch):
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)
    settings = make_settings(tmp_path, self_extend_max_templates=8)
    db = make_db(tmp_path)
    gap = template_gap()

    first = selfext.act_on_gaps([gap], settings, db, FakeBrain())
    second = selfext.act_on_gaps([gap], settings, db, FakeBrain())

    assert first == {"templates_added": 1, "proposals": 0}
    assert second == {"templates_added": 0, "proposals": 0}
    auto_templates = json.loads(auto_path.read_text(encoding="utf-8"))
    assert len(auto_templates) == 1


# ---------------------------------------------------------------------------
# Tier 2 -- sandboxed, AST-scanned skill proposals
# ---------------------------------------------------------------------------
def test_tier2_pure_source_produces_proposed_status_without_automerge(tmp_path):
    settings = make_settings(tmp_path, self_extend_automerge=False, self_extend_max_proposals_per_day=5)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=True)

    summary = selfext.act_on_gaps([skill_gap()], settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert summary == {"templates_added": 0, "proposals": 1}
    assert len(sandbox_cls.calls) == 1
    assert sandbox_cls.calls[0]["files"]["check_rail_thing.py"] == PURE_SOURCE
    assert sandbox_cls.calls[0]["files"]["tests/test_gen.py"] == TESTS_SOURCE
    assert sandbox_cls.calls[0]["test_path"] == "tests"

    events = db.events("self_extend")
    assert events[0]["data"]["tier"] == "skill"
    assert events[0]["data"]["scan_ok"] is True
    assert events[0]["data"]["tests_passed"] is True
    assert events[0]["data"]["status"] == "proposed"

    proposal_keys = [k for k in _all_setting_keys(db) if k.startswith("selfext_proposal:")]
    assert len(proposal_keys) == 1
    record = db.get_setting(proposal_keys[0])
    assert record["scan_ok"] is True
    assert record["tests_passed"] is True
    assert record["status"] == "proposed"
    assert record["source"] == PURE_SOURCE

    # A human-reviewable file copy is written under data_dir, never executed.
    proposal_dir = Path(settings.data_dir) / "selfext-proposals"
    written = list(proposal_dir.rglob("check_rail_thing.py"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == PURE_SOURCE


def test_tier2_ready_to_merge_only_when_automerge_scan_and_tests_all_pass(tmp_path, monkeypatch):
    # Nothing in this path may shell out to git/docker to merge or deploy.
    import subprocess
    def _forbidden(*a, **k):
        raise AssertionError("selfext must never invoke subprocess")
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    settings = make_settings(tmp_path, self_extend_automerge=True, self_extend_max_proposals_per_day=5)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=True)

    summary = selfext.act_on_gaps([skill_gap()], settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert summary["proposals"] == 1
    assert db.events("self_extend")[0]["data"]["status"] == "ready_to_merge"


def test_tier2_impure_source_is_scan_rejected_and_sandbox_never_called(tmp_path):
    settings = make_settings(tmp_path, self_extend_max_proposals_per_day=5)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": IMPURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=True)

    summary = selfext.act_on_gaps([skill_gap()], settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert summary == {"templates_added": 0, "proposals": 0}
    assert sandbox_cls.calls == []  # sandbox must never run untrusted-scanned source
    data = db.events("self_extend")[0]["data"]
    assert data["status"] == "scan_rejected"
    assert data["scan_ok"] is False
    assert data["reasons"]
    assert not any(k.startswith("selfext_proposal:") for k in _all_setting_keys(db))


def test_tier2_generation_empty_is_logged_and_skipped(tmp_path):
    settings = make_settings(tmp_path, self_extend_max_proposals_per_day=5)
    db = make_db(tmp_path)
    brain = FakeBrain({})  # simulates a blocked/failed/malformed LLM call
    sandbox_cls = make_sandbox_client_cls(passed=True)

    summary = selfext.act_on_gaps([skill_gap()], settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert summary == {"templates_added": 0, "proposals": 0}
    assert sandbox_cls.calls == []
    assert db.events("self_extend")[0]["data"]["proposed"] == "generation_empty"


def test_tier2_failing_sandbox_tests_never_yield_ready_to_merge(tmp_path):
    settings = make_settings(tmp_path, self_extend_automerge=True, self_extend_max_proposals_per_day=5)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=False)

    summary = selfext.act_on_gaps([skill_gap()], settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert summary == {"templates_added": 0, "proposals": 1}
    data = db.events("self_extend")[0]["data"]
    assert data["tests_passed"] is False
    assert data["status"] == "proposed"
    assert data["status"] != "ready_to_merge"


def test_tier2_respects_per_day_proposal_cap(tmp_path):
    settings = make_settings(tmp_path, self_extend_max_proposals_per_day=1)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=True)

    gaps = [skill_gap(class_key="acme|class one"), skill_gap(class_key="acme|class two")]
    summary = selfext.act_on_gaps(gaps, settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert summary["proposals"] == 1
    assert len(brain.calls) == 1  # the model is never called once the daily cap is hit
    assert len(sandbox_cls.calls) == 1
    statuses = [e["data"].get("status") for e in db.events("self_extend")]
    assert "capped" in statuses


def test_tier2_cap_persists_across_calls_via_logged_events(tmp_path):
    settings = make_settings(tmp_path, self_extend_max_proposals_per_day=1)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=True)

    selfext.act_on_gaps([skill_gap(class_key="acme|class one")], settings, db, brain, sandbox_client_cls=sandbox_cls)
    summary2 = selfext.act_on_gaps([skill_gap(class_key="acme|class two")], settings, db, FakeBrain(
        {"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE}
    ), sandbox_client_cls=sandbox_cls)

    assert summary2 == {"templates_added": 0, "proposals": 0}


def test_tier2_is_idempotent_per_class_key(tmp_path):
    settings = make_settings(tmp_path, self_extend_max_proposals_per_day=5)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=True)
    gap = skill_gap()

    selfext.act_on_gaps([gap], settings, db, brain, sandbox_client_cls=sandbox_cls)
    selfext.act_on_gaps([gap], settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert len(brain.calls) == 1
    assert len(sandbox_cls.calls) == 1


# ---------------------------------------------------------------------------
# Cross-cutting safety
# ---------------------------------------------------------------------------
def test_act_on_gaps_never_raises_on_malformed_gaps(tmp_path, monkeypatch):
    auto_path = tmp_path / "auto_templates.json"
    auto_path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(selfext, "_AUTO_TEMPLATES_PATH", auto_path)
    settings = make_settings(tmp_path)
    db = make_db(tmp_path)

    summary = selfext.act_on_gaps(
        [None, "not-a-dict", {}, {"class_key": "k", "suggestion": "template"},
         {"class_key": "k2", "suggestion": "unknown"}],
        settings, db, FakeBrain(),
    )
    assert set(summary) == {"templates_added", "proposals"}


def test_act_on_gaps_tolerates_non_list_input(tmp_path):
    settings = make_settings(tmp_path)
    db = make_db(tmp_path)
    assert selfext.act_on_gaps(None, settings, db, FakeBrain()) == {"templates_added": 0, "proposals": 0}
    assert selfext.act_on_gaps("nope", settings, db, FakeBrain()) == {"templates_added": 0, "proposals": 0}


def test_selfext_source_never_imports_or_execs_generated_code():
    """Static safety-net for the hard line: generated source is DATA and is
    never imported/exec'd in this process -- only ever handed to the
    isolated sandbox client."""
    src = Path(inspect.getfile(selfext)).read_text(encoding="utf-8")
    for forbidden in ("\nexec(", " exec(", "\neval(", " eval(", "__import__", "importlib.import_module", "\ncompile("):
        assert forbidden not in src, f"selfext.py must never contain {forbidden!r}"


def test_generated_func_name_is_never_defined_in_this_process(tmp_path):
    settings = make_settings(tmp_path, self_extend_max_proposals_per_day=5)
    db = make_db(tmp_path)
    brain = FakeBrain({"func_name": "check_rail_thing", "source": PURE_SOURCE, "tests": TESTS_SOURCE})
    sandbox_cls = make_sandbox_client_cls(passed=True)

    before_modules = set(sys.modules)
    selfext.act_on_gaps([skill_gap()], settings, db, brain, sandbox_client_cls=sandbox_cls)

    assert set(sys.modules) == before_modules
    assert "check_rail_thing" not in globals()
    import builtins
    assert not hasattr(builtins, "check_rail_thing")


def _all_setting_keys(db):
    with db.connect() as c:
        return [row[0] for row in c.execute("SELECT key FROM settings").fetchall()]
