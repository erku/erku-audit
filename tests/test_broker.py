"""Tests for the least-privilege GitHub broker.

Uses FastAPI's TestClient plus a FAKE GitHubClient (`FakeGitHubClient`
below) — no real HTTP/GitHub call is ever made. The fake also carries a
dummy "token"-shaped string so we can assert it never leaks into any
response body, mirroring the constraint that the real GitHub token must
never appear in a log, response, or exception message.

NOTE on the publish body shape: the pinned `1f916.project.v1` manifest
already defines `files` as the list of per-file hash records
({path, sha256, bytes}); actual file *contents* are carried in a
sibling `file_contents` field (see the docstring on
`broker.app.PublishRequest` for why the brief's literal "+ {"files":
{...content...}}}" phrasing would silently collide with that field).
"""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from broker.app import BrokerSettings, create_app

FAKE_GITHUB_TOKEN = "ghp_fake_super_secret_do_not_leak_0123456789"
BROKER_TOKEN = "test-broker-shared-secret"


class FakeGitHubClient:
    """In-memory stand-in for broker.github.GitHubClient. No network I/O."""

    def __init__(self, token: str = FAKE_GITHUB_TOKEN):
        self.token = token
        self.created: list[str] = []
        self.put_files: list[dict] = []
        self.pulls: list[dict] = []
        self._repos: dict[str, dict] = {}

    def create_public_repo(self, name: str) -> dict:
        self.created.append(name)
        record = {
            "name": name,
            "full_name": f"erku-test/{name}",
            "html_url": f"https://github.com/erku-test/{name}",
            "default_branch": "main",
            "private": False,
        }
        self._repos[name] = record
        return record

    def get_repo(self, name: str):
        return self._repos.get(name)

    def put_file(self, name: str, path: str, content_b64: str, message: str, branch: str) -> dict:
        self.put_files.append({"name": name, "path": path, "branch": branch, "message": message})
        return {"content": {"path": path}, "commit": {"sha": f"sha-{len(self.put_files)}"}}

    def create_pull(self, name: str, head: str, base: str, title: str) -> dict:
        self.pulls.append({"name": name, "head": head, "base": base, "title": title})
        return {"number": len(self.pulls), "html_url": f"https://github.com/erku-test/{name}/pull/{len(self.pulls)}"}


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tree_hash(file_meta: list[dict]) -> str:
    hasher = hashlib.sha256()
    for entry in sorted(file_meta, key=lambda item: item["path"]):
        hasher.update(entry["path"].encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(entry["sha256"].encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def build_manifest(name: str, files: dict[str, str], *, template: str = "python-tool") -> dict:
    file_meta = [
        {"path": path, "sha256": _sha256_hex(content), "bytes": len(content.encode("utf-8"))}
        for path, content in files.items()
    ]
    return {
        "manifest_version": "1f916.project.v1",
        "name": name,
        "template": template,
        "listing_id": None,
        "grant_slug": None,
        "created_utc": "2026-09-12T00:00:00Z",
        "source_hash": _sha256_hex("source"),
        "files": file_meta,
        "tree_hash": _tree_hash(file_meta),
        "generator": "f916.builder",
        "executes_community_commands": False,
        "file_contents": dict(files),
    }


@pytest.fixture
def broker(tmp_path):
    settings = BrokerSettings(
        broker_token=BROKER_TOKEN,
        max_repos=2,
        state_file=tmp_path / "broker_state.json",
        github_token_file=None,
    )
    fake = FakeGitHubClient()
    app = create_app(settings=settings, github_client=fake)
    with TestClient(app) as client:
        yield client, fake, settings


AUTH = {"Authorization": f"Bearer {BROKER_TOKEN}"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def test_healthz_requires_no_auth(broker):
    client, _fake, _settings = broker
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_missing_bearer_rejected(broker):
    client, fake, _settings = broker
    response = client.post("/repos", json={"name": "erku-1f916-demo"})
    assert response.status_code == 401
    assert fake.created == []


def test_invalid_bearer_rejected(broker):
    client, fake, _settings = broker
    response = client.post(
        "/repos", json={"name": "erku-1f916-demo"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert fake.created == []


# ---------------------------------------------------------------------------
# POST /repos
# ---------------------------------------------------------------------------
def test_create_repo_valid_name(broker):
    client, fake, _settings = broker
    response = client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "erku-1f916-demo"
    assert body["default_branch"] == "main"
    assert fake.created == ["erku-1f916-demo"]


def test_create_repo_bad_prefix_rejected(broker):
    client, fake, _settings = broker
    response = client.post("/repos", json={"name": "not-the-right-prefix"}, headers=AUTH)
    assert response.status_code == 422
    assert fake.created == []


def test_create_repo_quota_exceeded(broker):
    client, fake, settings = broker
    assert settings.max_repos == 2
    assert client.post("/repos", json={"name": "erku-1f916-one"}, headers=AUTH).status_code == 201
    assert client.post("/repos", json={"name": "erku-1f916-two"}, headers=AUTH).status_code == 201
    response = client.post("/repos", json={"name": "erku-1f916-three"}, headers=AUTH)
    assert response.status_code == 409
    assert fake.created == ["erku-1f916-one", "erku-1f916-two"]


def test_create_repo_idempotent(broker):
    client, fake, _settings = broker
    first = client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    assert first.status_code == 201
    second = client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    assert second.status_code == 200
    assert second.json()["name"] == "erku-1f916-demo"
    assert fake.created == ["erku-1f916-demo"]  # no second GitHub call


# ---------------------------------------------------------------------------
# POST /repos/{name}/publish
# ---------------------------------------------------------------------------
def test_publish_success(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    manifest = build_manifest("erku-1f916-demo", {"README.md": "hello world\n", "main.py": "print('hi')\n"})

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["repo"] == "erku-1f916-demo"
    assert body["branch"] == "main"
    assert body["pull_request"] is None
    assert len(fake.put_files) == 2
    assert {c["path"] for c in fake.put_files} == {"README.md", "main.py"}


def test_publish_second_time_opens_pr(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    first_manifest = build_manifest("erku-1f916-demo", {"README.md": "v1\n"})
    assert client.post("/repos/erku-1f916-demo/publish", json=first_manifest, headers=AUTH).status_code == 200

    second_manifest = build_manifest("erku-1f916-demo", {"README.md": "v2\n"})
    response = client.post("/repos/erku-1f916-demo/publish", json=second_manifest, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["pull_request"] is not None
    assert len(fake.pulls) == 1


def test_publish_tampered_file_rejected(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    manifest = build_manifest("erku-1f916-demo", {"README.md": "hello world\n"})
    manifest["file_contents"]["README.md"] = "tampered content\n"  # sha256 no longer matches

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest, headers=AUTH)

    assert response.status_code == 422
    assert fake.put_files == []


def test_broker_imports_tree_hash_from_builder_not_a_local_copy():
    """Regression guard for the tree_hash duplication/drift issue: broker/app.py
    must import f916.builder's _tree_hash (the manifest integrity check) rather
    than reimplementing it, so the two can never silently diverge."""
    from f916 import builder as f916_builder
    from broker import app as broker_app

    assert broker_app._builder_tree_hash is f916_builder._tree_hash


def test_publish_tree_hash_mismatch_rejected(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    manifest = build_manifest("erku-1f916-demo", {"README.md": "hello world\n"})
    manifest["tree_hash"] = "0" * 64

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest, headers=AUTH)

    assert response.status_code == 422
    assert fake.put_files == []


def test_publish_path_traversal_rejected(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    manifest = build_manifest("erku-1f916-demo", {"../escape.txt": "malicious\n"})

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest, headers=AUTH)

    assert response.status_code == 422
    assert fake.put_files == []


def test_publish_leading_slash_path_rejected(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    manifest = build_manifest("erku-1f916-demo", {"/etc/passwd": "malicious\n"})

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest, headers=AUTH)

    assert response.status_code == 422
    assert fake.put_files == []


def test_publish_unknown_repo_rejected(broker):
    client, fake, _settings = broker
    manifest = build_manifest("erku-1f916-never-created", {"README.md": "hi\n"})

    response = client.post("/repos/erku-1f916-never-created/publish", json=manifest, headers=AUTH)

    assert response.status_code == 404
    assert fake.put_files == []


def test_publish_oversized_payload_rejected(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    huge_content = "x" * (2 * 1024 * 1024)  # 2 MiB > 1 MiB cap
    manifest = build_manifest("erku-1f916-demo", {"big.txt": huge_content})

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest, headers=AUTH)

    assert response.status_code == 422
    assert fake.put_files == []


def test_publish_too_many_files_rejected(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    files = {f"file_{i}.txt": f"content {i}\n" for i in range(41)}  # over MAX_FILES=40
    manifest = build_manifest("erku-1f916-demo", files)

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest, headers=AUTH)

    assert response.status_code == 422
    assert fake.put_files == []


def test_publish_requires_auth(broker):
    client, fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    manifest = build_manifest("erku-1f916-demo", {"README.md": "hi\n"})

    response = client.post("/repos/erku-1f916-demo/publish", json=manifest)

    assert response.status_code == 401
    assert fake.put_files == []


# ---------------------------------------------------------------------------
# Explicit denials
# ---------------------------------------------------------------------------
def test_delete_method_not_allowed(broker):
    client, _fake, _settings = broker
    client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH)
    response = client.delete("/repos/erku-1f916-demo", headers=AUTH)
    assert response.status_code in (404, 405)


def test_delete_repos_collection_not_allowed(broker):
    client, _fake, _settings = broker
    response = client.delete("/repos", headers=AUTH)
    assert response.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------
def test_tokens_never_appear_in_any_response_body(broker):
    client, fake, settings = broker
    responses = [
        client.get("/healthz"),
        client.post("/repos", json={"name": "erku-1f916-demo"}),  # 401, no auth
        client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH),  # 201
        client.post("/repos", json={"name": "erku-1f916-demo"}, headers=AUTH),  # 200 idempotent
        client.post("/repos", json={"name": "bad name"}, headers=AUTH),  # 422
        client.post(
            "/repos/erku-1f916-demo/publish",
            json=build_manifest("erku-1f916-demo", {"README.md": "hi\n"}),
            headers=AUTH,
        ),  # 200
        client.post("/repos/erku-1f916-unknown/publish", json={}, headers=AUTH),  # 404/422
        client.delete("/repos/erku-1f916-demo", headers=AUTH),  # 404/405
    ]
    for response in responses:
        text = response.text
        assert fake.token not in text
        assert settings.broker_token not in text
