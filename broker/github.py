"""Thin, injectable GitHub client wrapper.

`GitHubClient` exposes exactly the four operations the broker needs and
nothing else: create a public repo, look one up, write a file via the
contents API, and open a pull request. There is deliberately no
"delete", "update visibility", or "run a command" method — the narrow
surface IS the security boundary.

The client takes its token and an optional injectable `transport` (an
`httpx.BaseTransport`) so tests can pass a fake/mock transport and make
*zero* real network calls. The token is only ever placed in the
`Authorization` header of outgoing requests; it is never included in a
log line, an exception message, or a returned value.
"""
from __future__ import annotations

import httpx

DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0


class GitHubClientError(RuntimeError):
    """Raised when a GitHub API call does not succeed.

    Messages here must never include the token or full request/response
    bodies (which could echo it back) — only the method, path, and
    status code.
    """


class GitHubClient:
    """Minimal wrapper around the subset of the GitHub REST API the broker uses."""

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        if not token:
            raise ValueError("GitHubClient requires a non-empty token")
        self._token = token
        self._client = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)
        self._owner: str | None = None

    # -- internals ---------------------------------------------------
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return self._client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            # Deliberately drop the original exception (which may embed
            # request headers, i.e. the token) from the chain and message.
            raise GitHubClientError(f"GitHub request failed: {method} {url!r} ({type(exc).__name__})") from None

    def _owner_login(self) -> str:
        if self._owner is None:
            response = self._request("GET", "/user")
            if response.status_code != 200:
                raise GitHubClientError(f"could not resolve authenticated user (status {response.status_code})")
            self._owner = response.json()["login"]
        return self._owner

    # -- public API ----------------------------------------------------
    def create_public_repo(self, name: str) -> dict:
        """Create a new PUBLIC repository under the authenticated account."""
        response = self._request("POST", "/user/repos", json={"name": name, "private": False})
        if response.status_code != 201:
            raise GitHubClientError(f"create_public_repo({name!r}) failed (status {response.status_code})")
        return response.json()

    def get_repo(self, name: str) -> dict | None:
        """Return the repo's metadata, or None if it does not exist."""
        owner = self._owner_login()
        response = self._request("GET", f"/repos/{owner}/{name}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GitHubClientError(f"get_repo({name!r}) failed (status {response.status_code})")
        return response.json()

    def put_file(self, name: str, path: str, content_b64: str, message: str, branch: str) -> dict:
        """Create or update a single file via the GitHub contents API."""
        owner = self._owner_login()
        payload = {"message": message, "content": content_b64, "branch": branch}
        response = self._request("PUT", f"/repos/{owner}/{name}/contents/{path}", json=payload)
        if response.status_code not in (200, 201):
            raise GitHubClientError(f"put_file({name!r}, {path!r}) failed (status {response.status_code})")
        return response.json()

    def create_pull(self, name: str, head: str, base: str, title: str) -> dict:
        """Open a pull request from `head` into `base`."""
        owner = self._owner_login()
        payload = {"head": head, "base": base, "title": title}
        response = self._request("POST", f"/repos/{owner}/{name}/pulls", json=payload)
        if response.status_code != 201:
            raise GitHubClientError(f"create_pull({name!r}) failed (status {response.status_code})")
        return response.json()

    def close(self) -> None:
        self._client.close()
