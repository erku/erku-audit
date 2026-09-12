import json
import tempfile
import textwrap
from pathlib import Path

from sandbox.runner import run_job


def _make_job(tmp_path: Path, name: str, test_code: str, test_path: str | None = None) -> Path:
    job_dir = tmp_path / name
    project_dir = job_dir / 'project'
    project_dir.mkdir(parents=True)
    (project_dir / 'test_x.py').write_text(textwrap.dedent(test_code), encoding='utf-8')
    job = {'id': name}
    if test_path is not None:
        job['test_path'] = test_path
    (job_dir / 'job.json').write_text(json.dumps(job), encoding='utf-8')
    return job_dir


def test_run_job_passing_test_writes_passed_result(tmp_path):
    job_dir = _make_job(tmp_path, 'job-pass', """
        def test_ok():
            assert 1 + 1 == 2
    """)

    result = run_job(job_dir)

    assert result['id'] == 'job-pass'
    assert result['passed'] is True
    assert result['returncode'] == 0
    assert 'summary' in result


def test_run_job_failing_test_writes_failed_result(tmp_path):
    job_dir = _make_job(tmp_path, 'job-fail', """
        def test_fail():
            assert False
    """)

    result = run_job(job_dir)

    assert result['passed'] is False
    assert result['returncode'] != 0


def test_run_job_writes_result_json_atomically(tmp_path):
    job_dir = _make_job(tmp_path, 'job-atomic', """
        def test_ok():
            assert True
    """)

    result = run_job(job_dir)

    on_disk = json.loads((job_dir / 'result.json').read_text(encoding='utf-8'))
    assert on_disk == result
    assert not (job_dir / 'result.json.tmp').exists()


def test_run_job_respects_test_path_subdirectory(tmp_path):
    job_dir = tmp_path / 'job-subdir'
    tests_dir = job_dir / 'project' / 'tests'
    tests_dir.mkdir(parents=True)
    (tests_dir / 'test_y.py').write_text('def test_ok():\n    assert True\n', encoding='utf-8')
    (job_dir / 'job.json').write_text(json.dumps({'id': 'job-subdir', 'test_path': 'tests'}), encoding='utf-8')

    result = run_job(job_dir)

    assert result['passed'] is True


def test_run_job_never_raises_on_missing_job_files(tmp_path):
    job_dir = tmp_path / 'job-broken'
    job_dir.mkdir()

    result = run_job(job_dir)

    assert result['passed'] is False
    assert 'error' in result
    on_disk = json.loads((job_dir / 'result.json').read_text(encoding='utf-8'))
    assert on_disk == result


def test_run_job_test_cannot_escape_to_shared_jobs_volume(tmp_path):
    # `tmp_path` here stands in for the shared /data/sandbox-jobs volume:
    # it holds this job's dir plus a sibling "victim" job dir, exactly like
    # the real jobs volume holds every job side by side.
    victim_dir = tmp_path / 'job-victim'
    victim_project = victim_dir / 'project'
    victim_project.mkdir(parents=True)
    (victim_dir / 'job.json').write_text(json.dumps({'id': 'job-victim'}), encoding='utf-8')

    job_dir = _make_job(tmp_path, 'job-escape', """
        from pathlib import Path

        def test_attempts_relative_escape():
            # If pytest ran with cwd inside the shared jobs volume, this
            # would land next to a sibling job (e.g. overwrite its
            # result.json). It must instead land in a private tmpfs copy
            # that gets cleaned up, never in the shared volume.
            Path('../escaped.txt').write_text('pwned', encoding='utf-8')
            assert True
    """)

    result = run_job(job_dir)

    assert result['passed'] is True
    # No stray file anywhere under the shared jobs volume: not next to this
    # job, not in the victim's project dir, not in the volume root.
    assert not (tmp_path / 'escaped.txt').exists()
    assert not (job_dir / 'escaped.txt').exists()
    assert list(victim_project.iterdir()) == []
    # The runner (not the test process) still wrote the real result back.
    on_disk = json.loads((job_dir / 'result.json').read_text(encoding='utf-8'))
    assert on_disk == result


def test_run_job_copies_project_into_runners_own_tempdir(tmp_path, monkeypatch):
    captured = {}
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        captured['path'] = path
        return path

    monkeypatch.setattr('sandbox.runner.tempfile.mkdtemp', spy_mkdtemp)

    job_dir = _make_job(tmp_path, 'job-tempcopy', """
        def test_ok():
            assert True
    """)

    result = run_job(job_dir)

    assert result['passed'] is True
    assert captured.get('path'), 'run_job did not copy the project via tempfile.mkdtemp'
    work_dir = Path(captured['path'])
    assert str(work_dir).startswith(tempfile.gettempdir())
    # Cleaned up after the run: nothing left behind under /tmp.
    assert not work_dir.exists()
