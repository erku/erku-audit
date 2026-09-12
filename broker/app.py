"""FastAPI app for the least-privilege GitHub broker.

The broker is the ONLY component that ever holds the real GitHub token
(read from `GITHUB_TOKEN_FILE`, a mounted secret — never a plain env
var). The worker authenticates to the broker with a separate bearer
credential (`BROKER_TOKEN`). Neither the worker nor the LLM behind it
ever sees the GitHub token.

The API is intentionally narrow:
  - `POST /repos`                    create a bounded, prefix-enforced public repo
  - `POST /repos/{name}/publish`     publish a validated manifest + files
  - `GET  /healthz`                  liveness, no auth

There is no delete route, no visibility-change route, and no route that
accepts arbitrary git/shell commands. Every mutating route requires the
`BROKER_TOKEN` bearer and only ever touches repos this broker itself
created (tracked in a small JSON state file).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from broker.github import GitHubClient
# Reuse Task 4's builder constants so the two modules can never drift
# apart on what counts as a valid repo name / manifest shape.
from f916.builder import MANIFEST_VERSION, MAX_FILES, NAME_RE

MAX_PUBLISH_PAYLOAD_BYTES = 1 * 1024 * 1024  # 1 MiB, per brief


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@dataclass
class BrokerSettings:
    broker_token: str
    max_repos: int = 5
    state_file: Path = Path("./data/broker_state.json")
    github_token_file: Optional[str] = None

    @classmethod
    def from_env(cls) -> "BrokerSettings":
        return cls(
            broker_token=os.environ.get("BROKER_TOKEN", ""),
            max_repos=int(os.environ.get("BROKER_MAX_REPOS", "5")),
            state_file=Path(os.environ.get("BROKER_STATE_FILE", "./data/broker_state.json")),
            github_token_file=os.environ.get("GITHUB_TOKEN_FILE"),
        )


def _load_github_client_from_settings(settings: BrokerSettings) -> GitHubClient:
    if not settings.github_token_file:
        raise RuntimeError("GITHUB_TOKEN_FILE must be set (no github_client was injected)")
    token = Path(settings.github_token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN_FILE is empty")
    return GitHubClient(token=token)


# --------------------------------------------------------------------------
# State (which repos this broker created)
# --------------------------------------------------------------------------
def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"repos": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"repos": {}}
    if not isinstance(data, dict):
        return {"repos": {}}
    data.setdefault("repos", {})
    return data


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Manifest validation helpers (mirrors f916.builder._tree_hash exactly)
# --------------------------------------------------------------------------
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_hash(file_hashes: list[tuple[str, str]]) -> str:
    hasher = hashlib.sha256()
    for path, digest in sorted(file_hashes, key=lambda item: item[0]):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(digest.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _is_safe_relative_path(rel_path: str) -> bool:
    if not rel_path or rel_path.startswith("/") or rel_path.startswith("\\"):
        return False
    p = PurePosixPath(rel_path)
    if p.is_absolute():
        return False
    if ".." in p.parts:
        return False
    if any(part in ("", ".") for part in p.parts):
        return False
    return True


class ManifestFileEntry(BaseModel):
    path: str
    sha256: str
    bytes: int


class PublishRequest(BaseModel):
    """The shared `1f916.project.v1` manifest.

    NOTE on schema: the brief describes the publish body as "the shared
    project manifest + {"files": {"<path>": "<content>"}}". The pinned
    manifest schema itself already defines `files` as the list of
    per-file hash records ({path, sha256, bytes}) — reusing the same key
    for actual file *contents* would silently collide in a single JSON
    object (a second `files` key would just clobber the first when the
    body is parsed). To keep the pinned manifest shape intact byte-for-
    byte, actual file contents travel in a sibling field named
    `file_contents` instead. This is flagged as a concern in the task
    report.
    """

    manifest_version: str
    name: str
    template: str
    listing_id: Optional[str] = None
    grant_slug: Optional[str] = None
    created_utc: str
    source_hash: str
    files: list[ManifestFileEntry]
    tree_hash: str
    generator: str
    executes_community_commands: bool
    file_contents: dict[str, str]


class CreateRepoRequest(BaseModel):
    name: str = Field(pattern=NAME_RE)


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------
def create_app(settings: BrokerSettings | None = None, github_client: GitHubClient | None = None) -> FastAPI:
    state_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal settings, github_client
        if settings is None:
            settings = BrokerSettings.from_env()
        if github_client is None:
            github_client = _load_github_client_from_settings(settings)
        yield

    app = FastAPI(title="1f916 GitHub broker", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def _require_auth(request: Request) -> None:
        authorization = request.headers.get("authorization")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization[len("Bearer "):]
        if not settings or not settings.broker_token or not secrets.compare_digest(token, settings.broker_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/repos")
    async def create_repo(request: Request):
        # Auth is checked before the body is even parsed, so a malformed
        # body from an unauthenticated caller never gets a 422 hint.
        _require_auth(request)
        raw = await request.body()
        try:
            payload = json.loads(raw)
            body = CreateRepoRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError):
            raise HTTPException(status_code=422, detail="invalid request body")
        name = body.name
        with state_lock:
            state = _load_state(settings.state_file)
            existing = state["repos"].get(name)
            if existing is not None:
                return existing
            if len(state["repos"]) >= settings.max_repos:
                raise HTTPException(status_code=409, detail="repo quota exceeded")
            try:
                repo = github_client.create_public_repo(name)
            except Exception as exc:  # noqa: BLE001 - never let GitHubClientError text leak details beyond type
                raise HTTPException(status_code=502, detail=f"repo creation failed ({type(exc).__name__})") from None
            record = {
                "name": name,
                "full_name": repo.get("full_name", name),
                "html_url": repo.get("html_url"),
                "default_branch": repo.get("default_branch") or "main",
                "created_utc": _now_iso(),
                "has_content": False,
            }
            state["repos"][name] = record
            _save_state(settings.state_file, state)
        return JSONResponse(status_code=201, content=record)

    @app.post("/repos/{name}/publish")
    async def publish(name: str, request: Request):
        _require_auth(request)

        raw = await request.body()
        if len(raw) > MAX_PUBLISH_PAYLOAD_BYTES:
            raise HTTPException(status_code=422, detail="payload too large")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="invalid json body")
        try:
            manifest = PublishRequest.model_validate(payload)
        except ValidationError:
            raise HTTPException(status_code=422, detail="invalid manifest")

        with state_lock:
            state = _load_state(settings.state_file)
            record = state["repos"].get(name)
            if record is None:
                raise HTTPException(status_code=404, detail="unknown repo")

            if manifest.manifest_version != MANIFEST_VERSION:
                raise HTTPException(status_code=422, detail="unsupported manifest_version")
            if manifest.name != name:
                raise HTTPException(status_code=422, detail="manifest name does not match repo")
            if len(manifest.files) > MAX_FILES or len(manifest.file_contents) > MAX_FILES:
                raise HTTPException(status_code=422, detail="too many files")

            declared_paths = {entry.path for entry in manifest.files}
            content_paths = set(manifest.file_contents.keys())
            if declared_paths != content_paths:
                raise HTTPException(status_code=422, detail="file set does not match manifest")

            for rel_path in content_paths:
                if not _is_safe_relative_path(rel_path):
                    raise HTTPException(status_code=422, detail="unsafe file path")

            recomputed_hashes: list[tuple[str, str]] = []
            for entry in manifest.files:
                content = manifest.file_contents[entry.path]
                encoded = content.encode("utf-8")
                digest = _sha256_hex(encoded)
                if digest != entry.sha256 or len(encoded) != entry.bytes:
                    raise HTTPException(status_code=422, detail="file content does not match declared hash")
                recomputed_hashes.append((entry.path, digest))

            if _tree_hash(recomputed_hashes) != manifest.tree_hash:
                raise HTTPException(status_code=422, detail="tree_hash mismatch")

            default_branch = record.get("default_branch") or "main"
            has_content = bool(record.get("has_content"))
            head_branch = default_branch if not has_content else f"broker-publish-{manifest.tree_hash[:12]}"

            commits = []
            try:
                for rel_path in sorted(manifest.file_contents):
                    content = manifest.file_contents[rel_path]
                    content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
                    result = github_client.put_file(
                        name, rel_path, content_b64,
                        message=f"1f916 publish: {rel_path}", branch=head_branch,
                    )
                    commits.append({"path": rel_path, "sha": (result.get("commit") or {}).get("sha")})

                pull_request = None
                if has_content:
                    pr = github_client.create_pull(
                        name, head=head_branch, base=default_branch,
                        title=f"1f916 publish {manifest.tree_hash[:12]}",
                    )
                    pull_request = {"number": pr.get("number"), "html_url": pr.get("html_url")}
            except Exception as exc:  # noqa: BLE001 - never let raw GitHub error text leak
                raise HTTPException(status_code=502, detail=f"publish failed ({type(exc).__name__})") from None

            record["has_content"] = True
            record["last_tree_hash"] = manifest.tree_hash
            record["last_published_utc"] = _now_iso()
            state["repos"][name] = record
            _save_state(settings.state_file, state)

        return {
            "repo": name,
            "branch": head_branch,
            "base": default_branch,
            "commits": commits,
            "pull_request": pull_request,
        }

    return app


app = create_app()
