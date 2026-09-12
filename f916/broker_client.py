"""Thin, injectable HTTP client for the worker -> broker path (Task B1).

Mirrors `broker.github.GitHubClient`'s shape: a narrow surface (create a
repo, publish a manifest), an injectable transport so tests never make a
real network call, and a hard rule that the broker bearer token is never
placed in a log line, a returned value, or an exception message.
"""
from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 30.0


class BrokerClientError(RuntimeError):
    """Raised when a broker HTTP call does not succeed.

    Messages here must never include the bearer token (or any response
    body that could echo it back) -- only the method, path, and status
    code (or exception type, for a transport-level failure).
    """


class BrokerClient:
    """Minimal wrapper around the two broker routes the worker needs."""

    def __init__(self, base_url: str, token: str, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        if not base_url:
            raise ValueError("BrokerClient requires a non-empty base_url")
        if not token:
            raise ValueError("BrokerClient requires a non-empty token")
        self._token = token
        self._client = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

    # -- internals ---------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, path: str, json_body: dict) -> dict:
        try:
            response = self._client.post(path, json=json_body, headers=self._headers())
        except httpx.HTTPError as exc:
            # Deliberately drop the original exception (which may embed
            # request headers, i.e. the token) from the chain and message.
            raise BrokerClientError(f"broker request failed: POST {path!r} ({type(exc).__name__})") from None
        if not (200 <= response.status_code < 300):
            raise BrokerClientError(f"broker request failed: POST {path!r} (status {response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise BrokerClientError(f"broker returned a non-JSON response: POST {path!r}") from None

    # -- public API ----------------------------------------------------
    def create_repo(self, name: str) -> dict:
        """Create (or, idempotently, fetch) a bounded, prefix-enforced repo."""
        return self._post("/repos", {"name": name})

    def publish(self, name: str, manifest: dict, file_contents: dict) -> dict:
        """Publish a validated project manifest plus its file contents."""
        body = {**manifest, "file_contents": file_contents}
        return self._post(f"/repos/{name}/publish", body)

    def close(self) -> None:
        self._client.close()
