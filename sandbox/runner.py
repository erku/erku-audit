"""Isolated sandbox test runner (container entrypoint).

This module runs INSIDE the `sandbox-runner` container: no network, no
secrets, read-only filesystem outside the jobs volume. It communicates with
the worker ONLY through the shared jobs directory on disk (file IPC) —
there is no import of f916/worker code and no credential ever reaches this
process. It polls `SANDBOX_JOBS_DIR` for job directories the worker has
dropped (see f916/sandbox_client.py), runs each project's tests with
pytest, and writes the outcome back as `result.json`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 2
SUMMARY_TAIL_LINES = 40
SUMMARY_MAX_CHARS = 4000


def _timeout_seconds() -> int:
    try:
        return int(os.getenv('SANDBOX_TIMEOUT_SECONDS', str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _truncate_summary(text: str) -> str:
    tail = '\n'.join(text.splitlines()[-SUMMARY_TAIL_LINES:])
    if len(tail) > SUMMARY_MAX_CHARS:
        tail = tail[-SUMMARY_MAX_CHARS:]
    return tail


def _write_result_atomic(job_dir: Path, result: dict) -> None:
    tmp_path = job_dir / 'result.json.tmp'
    final_path = job_dir / 'result.json'
    tmp_path.write_text(json.dumps(result), encoding='utf-8')
    os.replace(tmp_path, final_path)


def run_job(job_dir: Path) -> dict:
    """Run one job's tests and write `result.json` atomically.

    Reads `job_dir/job.json` (`{'id':.., 'test_path': 'tests' (optional)}`),
    runs pytest against `job_dir/project/<test_path or '.'>` with cwd set to
    the project dir, and writes back a result dict. Never raises: any
    failure while reading the job or running the subprocess is captured as
    a failed result instead of propagating out of the poll loop.
    """
    job_dir = Path(job_dir)
    job_id = job_dir.name
    try:
        job = json.loads((job_dir / 'job.json').read_text(encoding='utf-8'))
        job_id = job.get('id', job_id)
        test_path = job.get('test_path') or '.'
        project_dir = job_dir / 'project'
        target = project_dir / test_path
        timeout = _timeout_seconds()
        try:
            proc = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q', str(target)],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            summary = _truncate_summary((proc.stdout or '') + (proc.stderr or ''))
            result = {
                'id': job_id,
                'passed': proc.returncode == 0,
                'returncode': proc.returncode,
                'summary': summary,
            }
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else ''
            err = exc.stderr if isinstance(exc.stderr, str) else ''
            result = {
                'id': job_id,
                'passed': False,
                'timed_out': True,
                'returncode': None,
                'summary': _truncate_summary(out + err),
            }
    except Exception as exc:  # noqa: BLE001 - never raise out of the poll loop
        result = {
            'id': job_id,
            'passed': False,
            'error': type(exc).__name__,
        }
    _write_result_atomic(job_dir, result)
    return result


def _pending_jobs(jobs_dir: Path):
    for entry in sorted(jobs_dir.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / 'result.json').exists():
            continue
        if not (entry / 'job.json').exists():
            continue
        if not (entry / 'project').is_dir():
            continue
        yield entry


def main() -> None:
    jobs_dir = Path(os.getenv('SANDBOX_JOBS_DIR', 'sandbox-jobs'))
    jobs_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            for job_dir in _pending_jobs(jobs_dir):
                run_job(job_dir)
        except Exception:  # noqa: BLE001 - keep polling no matter what
            pass
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
