"""Worker-side client for the isolated sandbox test runner.

This module executes nothing itself: it is pure filesystem IPC. It drops a
project's files and a job descriptor into `settings.sandbox_jobs_dir`, then
polls for `result.json`, which is written by `sandbox/runner.py` running in
a separate, network-less, secret-less container. Nothing here talks to the
network or shells out.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

_POLL_INTERVAL_SECONDS = 0.05
_RESULT_WAIT_MARGIN_SECONDS = 1.0


def _check_safe_path(rel_path: str) -> None:
    """Reuse the same path-safety rule as f916.builder: no absolute paths,
    no leading slash/backslash, no '..' traversal segments."""
    if rel_path.startswith('/') or rel_path.startswith('\\'):
        raise ValueError('absolute_path_rejected')
    p = Path(rel_path)
    if p.is_absolute():
        raise ValueError('absolute_path_rejected')
    if '..' in p.parts:
        raise ValueError('path_traversal_rejected')


class SandboxClient:
    def __init__(self, settings):
        self.jobs_dir = Path(settings.sandbox_jobs_dir)
        self.timeout_seconds = settings.sandbox_timeout_seconds

    def run(self, project_name: str, files: dict, *, test_path: str = '.') -> dict:
        """Drop `files` and a job descriptor for `project_name` into a fresh
        job directory, then wait for the sandbox runner to write back
        `result.json`. Returns the parsed result dict, or
        `{'passed': False, 'timed_out': True}` if none appears in time.
        Raises ValueError if any path in `files` is unsafe."""
        for rel_path in files:
            _check_safe_path(rel_path)

        job_id = uuid.uuid4().hex
        job_dir = self.jobs_dir / job_id
        project_dir = job_dir / 'project'
        project_dir.mkdir(parents=True, exist_ok=True)

        for rel_path, content in files.items():
            target = project_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')

        job = {'id': job_id, 'project_name': project_name, 'test_path': test_path}
        (job_dir / 'job.json').write_text(json.dumps(job), encoding='utf-8')

        return self._wait_for_result(job_dir)

    def _wait_for_result(self, job_dir: Path) -> dict:
        deadline = time.monotonic() + self.timeout_seconds + _RESULT_WAIT_MARGIN_SECONDS
        result_path = job_dir / 'result.json'
        while time.monotonic() < deadline:
            if result_path.exists():
                try:
                    return json.loads(result_path.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    pass
            time.sleep(_POLL_INTERVAL_SECONDS)
        return {'passed': False, 'timed_out': True}
