"""Brain.generate_skill (Task X, tier 2) -- the model authors a candidate
PURE `(target, params) -> dict` skill function plus pytest tests, seeded
ONLY from a capability gap's STRUCTURED fields (never raw listing prose).
Everything here is mocked via httpx.MockTransport: no real network/LLM call
is ever made. Nothing returned is imported or exec'd here or by the caller
-- see f916.selfext / f916.codescan for what happens to it downstream."""
import json

import httpx

from f916.brain import MAX_FILE_BYTES, Brain, SKILL_RESPONSE_SCHEMA
from f916.config import Settings
from f916.db import Database


def gap_payload(**updates):
    base = {"class_key": "acme|rail derivation", "funder": "acme",
            "sample_title": "Break the rail derivation totals", "suggestion": "skill"}
    return base | updates


def test_generate_skill_returns_func_name_source_tests_on_success(tmp_path):
    body = {"func_name": "check_rail_thing",
            "source": "def check_rail_thing(target, params):\n    return {'status': 'consistent'}\n",
            "tests": "def test_ok():\n    assert True\n"}
    def handler(request):
        return httpx.Response(200, json={
            "message": {"content": json.dumps(body)},
            "prompt_eval_count": 5, "eval_count": 5,
        })
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    result = brain.generate_skill(gap_payload())
    assert result == body
    assert db.get_setting("llm_usage")["tokens"] == 10


def test_generate_skill_request_carries_only_structured_gap_fields_not_raw_body(tmp_path):
    captured = {}
    body = {"func_name": "check_x", "source": "def check_x(target, params):\n    return {}\n",
            "tests": "def test_ok():\n    assert True\n"}
    def handler(request):
        captured["content"] = json.loads(request.content)["messages"][1]["content"]
        return httpx.Response(200, json={"message": {"content": json.dumps(body)},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    gap = gap_payload(body="SECRET RAW LISTING PROSE THAT MUST NEVER BE SENT",
                       description="also raw prose")
    brain.generate_skill(gap)
    assert "class_key" in captured["content"] or "acme" in captured["content"]
    assert "SECRET RAW LISTING PROSE" not in captured["content"]
    assert "also raw prose" not in captured["content"]


def test_generate_skill_uses_the_skill_response_schema_as_the_ollama_format(tmp_path):
    captured = {}
    body = {"func_name": "check_x", "source": "def check_x(target, params):\n    return {}\n",
            "tests": "def test_ok():\n    assert True\n"}
    def handler(request):
        captured["format"] = json.loads(request.content)["format"]
        return httpx.Response(200, json={"message": {"content": json.dumps(body)},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    brain.generate_skill(gap_payload())
    assert captured["format"] == SKILL_RESPONSE_SCHEMA


def test_generate_skill_rejects_malformed_shape(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"message": {"content": json.dumps({"func_name": "x"})},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_skill(gap_payload()) == {}


def test_generate_skill_rejects_non_identifier_func_name(tmp_path):
    body = {"func_name": "not a valid identifier", "source": "def x(target, params):\n    return {}\n",
            "tests": "def test_ok():\n    assert True\n"}
    def handler(request):
        return httpx.Response(200, json={"message": {"content": json.dumps(body)},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_skill(gap_payload()) == {}


def test_generate_skill_rejects_oversized_source(tmp_path):
    body = {"func_name": "check_x", "source": "x" * (MAX_FILE_BYTES + 1),
            "tests": "def test_ok():\n    assert True\n"}
    def handler(request):
        return httpx.Response(200, json={"message": {"content": json.dumps(body)},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_skill(gap_payload()) == {}


def test_generate_skill_never_raises_on_non_json_content(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"message": {"content": "not json at all"},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_skill(gap_payload()) == {}


def test_generate_skill_never_raises_on_http_error(tmp_path):
    def handler(request):
        return httpx.Response(500)
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_skill(gap_payload()) == {}


def test_generate_skill_blocked_by_budget_never_makes_a_request(tmp_path):
    called = False
    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(500)
    db = Database(tmp_path / "state.db"); db.initialize()
    settings = Settings(data_dir=tmp_path, llm_daily_budget_usd=0.000001, llm_token_limits_enabled=True,
                         llm_input_usd_per_million=1, llm_output_usd_per_million=1)
    brain = Brain(settings, db, transport=httpx.MockTransport(handler))
    assert brain.generate_skill(gap_payload()) == {}
    assert not called


def test_generate_skill_blocked_while_rate_limited_never_makes_a_request(tmp_path):
    called = False
    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(500)
    db = Database(tmp_path / "state.db"); db.initialize()
    db.set_setting("llm_retry_state", {"blocked_until": 9999999999.0})
    brain = Brain(Settings(data_dir=tmp_path), db, transport=httpx.MockTransport(handler))
    assert brain.generate_skill(gap_payload()) == {}
    assert not called


def test_generate_skill_never_logs_the_api_key(tmp_path):
    body = {"func_name": "check_x", "source": "def check_x(target, params):\n    return {}\n",
            "tests": "def test_ok():\n    assert True\n"}
    def handler(request):
        return httpx.Response(200, json={"message": {"content": json.dumps(body)},
                                          "prompt_eval_count": 1, "eval_count": 1})
    db = Database(tmp_path / "state.db"); db.initialize()
    brain = Brain(Settings(data_dir=tmp_path, api_key="sk-super-secret-value"), db,
                  transport=httpx.MockTransport(handler))
    brain.generate_skill(gap_payload())
    for event in db.events("llm"):
        assert "sk-super-secret-value" not in json.dumps(event)
