"""Operator bootstrap for the GitHub broker's credential.

Run this on the OPERATOR's own host (NOT inside a container, and never
as part of the image build). It copies the token already held by the
locally-authenticated `gh` CLI into a git-ignored secret file that the
broker container mounts read-only via `GITHUB_TOKEN_FILE`.

The token is never printed, logged, or returned — only the destination
path and the resulting file's byte length are reported on success.

Usage:
    python scripts/broker_bootstrap.py
    python scripts/broker_bootstrap.py --dest secrets/github_token
"""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
from pathlib import Path

DEFAULT_DEST = Path("secrets/github_token")


def fetch_gh_token() -> str:
    """Read the current token from the `gh` CLI. Never prints it."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        raise SystemExit("`gh` CLI not found. Install GitHub CLI and run `gh auth login` first.")
    if result.returncode != 0:
        raise SystemExit("`gh auth token` failed. Run `gh auth login` first.")
    token = result.stdout.strip()
    if not token:
        raise SystemExit("`gh auth token` returned an empty token.")
    return token


def write_secret(token: str, dest: Path) -> int:
    """Write `token` to `dest` with 0600 perms (best-effort on non-POSIX). Returns byte length."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        pass
    try:
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # platforms without POSIX permission bits (e.g. some Windows filesystems)
    return dest.stat().st_size


def ensure_gitignored(secrets_dir: Path) -> None:
    """Make sure `secrets_dir` (or an ancestor) is git-ignored, adding a rule if not."""
    gitignore = Path(".gitignore")
    dir_name = secrets_dir.as_posix().rstrip("/") + "/"
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    already_ignored = any(line.strip().rstrip("/") == dir_name.rstrip("/") for line in existing)
    if already_ignored:
        return
    with gitignore.open("a", encoding="utf-8") as handle:
        if existing and existing[-1].strip() != "":
            handle.write("\n")
        handle.write(dir_name + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                         help="Destination secret file path (default: secrets/github_token)")
    args = parser.parse_args()

    token = fetch_gh_token()
    ensure_gitignored(args.dest.parent)
    size = write_secret(token, args.dest)
    del token  # do not linger in a local past this point

    print(f"Wrote GitHub token to {args.dest} ({size} bytes).")


if __name__ == "__main__":
    main()
