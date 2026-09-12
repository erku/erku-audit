"""Tests for f916.broker_client.BrokerClient -- the worker -> broker HTTP
client used by the autonomous project pipeline (Task B1).

Every test uses an httpx.MockTransport, so no real HTTP call is ever made.
The token used here is a dummy, distinctive string so we can assert it
never leaks into an exception message.
"""
import json

import httpx
import pytest

from f916.broker_client import BrokerClient, BrokerClientError

TOKEN = "worker-broker-shared-secret-do-not-leak-0123456789"


def _client(handler, **kwargs) -> BrokerClient:
    return BrokerClient("https://broker.internal.test", TOKEN, transport=httpx.MockTransport(handler), **kwargs)


def test_create_repo_sends_bearer_and_returns_json_record():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"name": "erku-1f916-demo", "html_url": "https://github.com/erku-test/erku-1f916-demo"})

    result = _client(handler).create_repo("erku-1f916-demo")

    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["path"] == "/repos"
    assert seen["body"] == {"name": "erku-1f916-demo"}
    assert result["name"] == "erku-1f916-demo"


def test_publish_posts_manifest_merged_with_file_contents():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"repo": "erku-1f916-demo", "commits": [{"path": "README.md"}]})

    manifest = {"manifest_version": "1f916.project.v1", "name": "erku-1f916-demo",
                "template": "static-report", "files": [{"path": "README.md", "sha256": "a" * 64, "bytes": 5}]}
    result = _client(handler).publish("erku-1f916-demo", manifest, {"README.md": "hello"})

    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["path"] == "/repos/erku-1f916-demo/publish"
    assert seen["body"]["name"] == "erku-1f916-demo"
    assert seen["body"]["file_contents"] == {"README.md": "hello"}
    assert seen["body"]["files"] == manifest["files"]  # manifest fields pass through untouched
    assert result["repo"] == "erku-1f916-demo"


@pytest.mark.parametrize("status", [401, 404, 409, 422, 500, 502])
def test_non_2xx_status_raises_brokerclienterror_without_leaking_token(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    with pytest.raises(BrokerClientError) as excinfo:
        _client(handler).create_repo("erku-1f916-demo")
    assert TOKEN not in str(excinfo.value)
    assert str(status) in str(excinfo.value)


def test_transport_error_raises_brokerclienterror_and_drops_original_exception_chain():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(BrokerClientError) as excinfo:
        _client(handler).publish("erku-1f916-demo", {"name": "erku-1f916-demo"}, {})
    assert TOKEN not in str(excinfo.value)
    # The original httpx exception (which could embed the Authorization
    # header) must never be chained onto the raised error.
    assert excinfo.value.__cause__ is None


def test_non_json_response_raises_brokerclienterror_without_leaking_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(BrokerClientError) as excinfo:
        _client(handler).create_repo("erku-1f916-demo")
    assert TOKEN not in str(excinfo.value)


def test_requires_non_empty_base_url_and_token():
    with pytest.raises(ValueError):
        BrokerClient("", TOKEN)
    with pytest.raises(ValueError):
        BrokerClient("https://broker.internal.test", "")


def test_zero_real_network_calls_are_made(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a real network transport was invoked")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", explode)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"ok": True})

    # This must succeed purely via the injected MockTransport, never
    # touching the real httpx.HTTPTransport patched above.
    result = _client(handler).create_repo("erku-1f916-demo")
    assert result == {"ok": True}
