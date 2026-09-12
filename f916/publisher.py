"""Publish already-sanitized evidence with a repository-scoped SSH deploy key."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


class Publisher:
    def __init__(self, settings, runner=subprocess.run):
        self.settings, self.runner = settings, runner
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", settings.github_repo):
            raise ValueError("Invalid GitHub repository")
        self.root = (Path(settings.data_dir) / "public-repo").resolve()
        self.key = (Path(settings.data_dir) / "github-deploy-ed25519").resolve()
        self.known_hosts = (Path(settings.data_dir) / "github_known_hosts").resolve()
        self.runtime_key = (Path(tempfile.gettempdir()) / "f916-github-deploy-ed25519").resolve()

    def _prepare_key(self):
        if not self.key.is_file() or not self.known_hosts.is_file(): raise RuntimeError("Publisher key is not configured")
        shutil.copyfile(self.key,self.runtime_key)
        self.runtime_key.chmod(0o600)

    def _env(self):
        env = os.environ.copy()
        env.update({
            "GIT_SSH_COMMAND": f'ssh -i "{self.runtime_key}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="{self.known_hosts}"',
            "GIT_AUTHOR_NAME":"erku-audit", "GIT_AUTHOR_EMAIL":"erku-audit@users.noreply.github.com",
            "GIT_COMMITTER_NAME":"erku-audit", "GIT_COMMITTER_EMAIL":"erku-audit@users.noreply.github.com",
        })
        return env

    def _run(self, args, cwd=None, check=True):
        return self.runner(args, cwd=cwd, env=self._env(), check=check,
                           capture_output=True, text=True, timeout=90)

    def publish(self, artifact):
        digest = artifact.get("hash", "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest): raise ValueError("Invalid artifact hash")
        files = artifact.get("evidence_files") or []
        if len(files) != 1: raise ValueError("Exactly one evidence file required")
        source = Path(files[0]).resolve()
        allowed = (Path(self.settings.data_dir) / "artifacts").resolve()
        if not source.is_relative_to(allowed) or not source.is_file(): raise ValueError("Evidence outside artifact directory")
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest: raise ValueError("Artifact hash mismatch")
        self._prepare_key()

        remote = f"git@github.com:{self.settings.github_repo}.git"
        if not (self.root / ".git").is_dir():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            self._run(["git","clone","--depth","1",remote,str(self.root)])
        self._run(["git","fetch","origin","main"],cwd=self.root)
        self._run(["git","reset","--hard","origin/main"],cwd=self.root)
        destination = self.root / "artifacts" / f"{digest}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source,destination)
        self._run(["git","add","--",f"artifacts/{digest}.json"],cwd=self.root)
        changed=self._run(["git","diff","--cached","--quiet"],cwd=self.root,check=False).returncode != 0
        if changed:
            self._run(["git","commit","-m",f"Publish audit artifact {digest[:12]}"],cwd=self.root)
            pushed=self._run(["git","push","origin","HEAD:main"],cwd=self.root,check=False)
            if pushed.returncode != 0:
                # One bounded retry handles a concurrent source-code push.
                self._run(["git","fetch","origin","main"],cwd=self.root)
                self._run(["git","rebase","origin/main"],cwd=self.root)
                self._run(["git","push","origin","HEAD:main"],cwd=self.root)
        commit=self._run(["git","log","-1","--format=%H","--",f"artifacts/{digest}.json"],cwd=self.root).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{40}",commit): raise RuntimeError("Published commit cannot be resolved")
        base=f"https://github.com/{self.settings.github_repo}"
        return {**artifact,"commit":commit,"public_url":f"{base}/blob/{commit}/artifacts/{digest}.json",
                "raw_url":f"https://raw.githubusercontent.com/{self.settings.github_repo}/{commit}/artifacts/{digest}.json"}
