import json
import uuid
from types import SimpleNamespace

import pytest

from f916.sandbox_client import SandboxClient


def _settings(tmp_path, timeout=5):
    return SimpleNamespace(sandbox_jobs_dir=tmp_path / 'jobs', sandbox_timeout_seconds=timeout)


def test_run_rejects_path_traversal(tmp_path):
    client = SandboxClient(_settings(tmp_path))
    with pytest.raises(ValueError):
        client.run('erku-1f916-demo', {'../escape.py': 'x = 1\n'})


def test_run_rejects_absolute_paths(tmp_path):
    client = SandboxClient(_settings(tmp_path))
    with pytest.raises(ValueError):
        client.run('erku-1f916-demo', {'/etc/passwd': 'x = 1\n'})
    with pytest.raises(ValueError):
        client.run('erku-1f916-demo', {'\\windows\\evil.py': 'x = 1\n'})


def test_run_rejects_unsafe_paths_before_writing_anything(tmp_path):
    settings = _settings(tmp_path)
    client = SandboxClient(settings)
    with pytest.raises(ValueError):
        client.run('erku-1f916-demo', {'ok.py': 'x = 1\n', '../escape.py': 'x = 2\n'})
    assert not settings.sandbox_jobs_dir.exists()


def test_run_writes_project_files_and_job_json_then_returns_result(tmp_path, monkeypatch):
    settings = _settings(tmp_path, timeout=5)
    client = SandboxClient(settings)

    fixed_id = 'fixedjobid'
    monkeypatch.setattr(uuid, 'uuid4', lambda: SimpleNamespace(hex=fixed_id))

    job_dir = settings.sandbox_jobs_dir / fixed_id
    expected_result = {'id': fixed_id, 'passed': True, 'returncode': 0, 'summary': 'ok'}

    calls = {'n': 0}

    def fake_sleep(seconds):
        calls['n'] += 1
        if calls['n'] == 1:
            (job_dir / 'result.json').write_text(json.dumps(expected_result), encoding='utf-8')

    monkeypatch.setattr('f916.sandbox_client.time.sleep', fake_sleep)

    result = client.run(
        'erku-1f916-demo',
        {'tests/test_x.py': 'def test_ok():\n    assert True\n'},
        test_path='tests',
    )

    assert (job_dir / 'project' / 'tests' / 'test_x.py').read_text(encoding='utf-8') == (
        'def test_ok():\n    assert True\n'
    )
    job_json = json.loads((job_dir / 'job.json').read_text(encoding='utf-8'))
    assert job_json['test_path'] == 'tests'
    assert job_json['project_name'] == 'erku-1f916-demo'
    assert result == expected_result
    assert calls['n'] == 1


def test_run_times_out_cleanly_when_no_result_appears(tmp_path):
    settings = _settings(tmp_path, timeout=0)
    client = SandboxClient(settings)

    result = client.run('erku-1f916-demo', {'test_x.py': 'def test_ok():\n    assert True\n'})

    assert result == {'passed': False, 'timed_out': True}
