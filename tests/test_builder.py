import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from f916.builder import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    NAME_RE,
    TEMPLATES,
    build_project,
)
from f916.builder import _check_paths

FIXED_NOW = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def python_spec(**updates):
    base = {
        "name": "erku-1f916-tiny-tool",
        "template": "python-tool",
        "title": "Tiny Tool",
        "description": "A tiny deterministic tool.",
        "listing_id": 41,
        "grant_slug": None,
        "source_hash": "a" * 64,
        "content": {"function_name": "answer", "value": "42"},
    }
    base.update(updates)
    return base


def report_spec(**updates):
    base = {
        "name": "erku-1f916-tiny-report",
        "template": "static-report",
        "title": "Tiny Report",
        "description": "A tiny deterministic report.",
        "listing_id": None,
        "grant_slug": "grant-2024",
        "source_hash": "b" * 64,
        "content": {"score": 87, "verdict": "pass"},
    }
    base.update(updates)
    return base


def recompute_tree_hash(manifest):
    hasher = hashlib.sha256()
    for record in sorted(manifest["files"], key=lambda r: r["path"]):
        hasher.update(record["path"].encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(record["sha256"].encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


# --- name normalization / prefix enforcement -------------------------------

@pytest.mark.parametrize("bad_name", [
    "tiny-tool",                      # missing prefix
    "erku-1f916-",                    # empty after prefix
    "erku-1f916-Tiny",                # uppercase
    "erku-1f916-tiny_tool",           # underscore not allowed
    "erku-1f916-" + "a" * 60,         # too long
    "../erku-1f916-escape",           # traversal-shaped
    "/erku-1f916-abs",                # absolute-shaped
    "erku1f916-tiny",                 # missing separator
])
def test_bad_names_are_rejected(tmp_path, bad_name):
    with pytest.raises(ValueError):
        build_project(python_spec(name=bad_name), tmp_path, now=FIXED_NOW)


def test_good_name_is_accepted(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    assert manifest["name"] == "erku-1f916-tiny-tool"
    assert (tmp_path / "erku-1f916-tiny-tool").is_dir()


def test_name_regex_matches_spec_constant():
    import re
    assert NAME_RE == r'^erku-1f916-[a-z0-9][a-z0-9-]{0,50}$'
    assert re.fullmatch(NAME_RE, "erku-1f916-a")
    assert not re.fullmatch(NAME_RE, "erku-1f916-")


# --- template allowlist ------------------------------------------------------

def test_template_allowlist_constant():
    assert TEMPLATES == ("python-tool", "static-report")


def test_unknown_template_rejected(tmp_path):
    with pytest.raises(ValueError):
        build_project(python_spec(template="bash-script"), tmp_path, now=FIXED_NOW)


# --- path traversal / absolute path / symlink rejection ---------------------

def test_check_paths_rejects_traversal(tmp_path):
    project_dir = tmp_path / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    with pytest.raises(ValueError):
        _check_paths(project_dir, {"../escape.txt": "x"})


def test_check_paths_rejects_absolute_path(tmp_path):
    project_dir = tmp_path / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    with pytest.raises(ValueError):
        _check_paths(project_dir, {"/etc/passwd": "x"})


def test_check_paths_rejects_preexisting_symlink_leaf(tmp_path):
    project_dir = tmp_path / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret-elsewhere")
    link = project_dir / "README.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    with pytest.raises(ValueError):
        _check_paths(project_dir, {"README.md": "hello"})


def test_build_rejects_when_target_project_dir_is_symlink(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    link = workspace / "erku-1f916-tiny-tool"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    with pytest.raises(ValueError, match="symlink_target_rejected"):
        build_project(python_spec(), workspace, now=FIXED_NOW)


def test_build_rejects_dangling_symlink_project_dir(tmp_path):
    # A symlink at workspace/<name> pointing at a target that does NOT
    # exist: Path.exists() follows the link and returns False for a
    # dangling link, so a guard written as `.exists() and .is_symlink()`
    # is skipped entirely and resolve() would then happily follow the
    # link. is_symlink() alone must be used, unconditionally.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    link = workspace / "erku-1f916-tiny-tool"
    missing_target = tmp_path / "does-not-exist" / "also-missing"
    try:
        link.symlink_to(missing_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    assert not link.exists()  # confirms the link is genuinely dangling
    assert link.is_symlink()
    with pytest.raises(ValueError, match="symlink_target_rejected"):
        build_project(python_spec(), workspace, now=FIXED_NOW)


def test_reject_if_symlinked_detects_symlinked_ancestor(tmp_path):
    from f916.builder import _reject_if_symlinked

    project_dir = tmp_path / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    try:
        (project_dir / "src").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    with pytest.raises(ValueError, match="symlink_target_rejected"):
        _reject_if_symlinked(project_dir, project_dir / "src" / "pkg" / "__init__.py")


def test_build_rejects_symlinked_intermediate_directory(tmp_path):
    # Plant a symlinked intermediate directory (not the leaf file itself)
    # at a path the python-tool template is about to write into, and
    # confirm the whole build is refused rather than writing through it.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project_dir = workspace / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    try:
        (project_dir / "src").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    with pytest.raises(ValueError, match="symlink_target_rejected"):
        build_project(python_spec(), workspace, now=FIXED_NOW)
    # Nothing should have been written through the symlink.
    assert list(outside.iterdir()) == []


# --- file-count / size / total-size limits -----------------------------------

def test_check_paths_rejects_too_many_files(tmp_path):
    project_dir = tmp_path / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    files = {f"f{i}.txt": "x" for i in range(MAX_FILES + 1)}
    with pytest.raises(ValueError):
        _check_paths(project_dir, files)


def test_check_paths_rejects_oversized_file(tmp_path):
    project_dir = tmp_path / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    with pytest.raises(ValueError):
        _check_paths(project_dir, {"big.txt": "x" * (MAX_FILE_BYTES + 1)})


def test_check_paths_rejects_oversized_total(tmp_path):
    project_dir = tmp_path / "erku-1f916-tiny-tool"
    project_dir.mkdir()
    chunk = "x" * (MAX_FILE_BYTES)
    count = (MAX_TOTAL_BYTES // MAX_FILE_BYTES) + 2
    files = {f"f{i}.txt": chunk for i in range(min(count, MAX_FILES))}
    with pytest.raises(ValueError):
        _check_paths(project_dir, files)


# --- secret scanning ----------------------------------------------------------

def test_secret_scan_refuses_api_key_in_content(tmp_path):
    spec = python_spec(content={"function_name": "leak", "value": "sk-ABCDEFGHIJKLMNOPQRSTUVWX1234"})
    with pytest.raises(ValueError, match="secret_like_content"):
        build_project(spec, tmp_path, now=FIXED_NOW)


def test_secret_scan_refuses_private_key_in_content(tmp_path):
    spec = python_spec(content={"function_name": "leak", "value": "0x" + "a" * 64})
    with pytest.raises(ValueError, match="secret_like_content"):
        build_project(spec, tmp_path, now=FIXED_NOW)


def test_secret_scan_refuses_labelled_secret_pattern(tmp_path):
    spec = report_spec(content={"note": "api_key=abcdefghijklmnop"})
    with pytest.raises(ValueError, match="secret_like_content"):
        build_project(spec, tmp_path, now=FIXED_NOW)


def test_secret_scan_does_not_reject_benign_content(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    assert manifest["name"] == "erku-1f916-tiny-tool"


# --- reproducibility -----------------------------------------------------------

def test_build_is_reproducible_across_workspaces(tmp_path):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    spec = python_spec()
    manifest1 = build_project(dict(spec), ws1, now=FIXED_NOW)
    manifest2 = build_project(dict(spec), ws2, now=FIXED_NOW)

    assert manifest1["tree_hash"] == manifest2["tree_hash"]
    files1 = {f["path"]: f["sha256"] for f in manifest1["files"]}
    files2 = {f["path"]: f["sha256"] for f in manifest2["files"]}
    assert files1 == files2
    # created_utc is the only field allowed to vary; everything else (aside
    # from timestamp) must be byte-identical given the same spec + now.
    m1_without_ts = {k: v for k, v in manifest1.items() if k != "created_utc"}
    m2_without_ts = {k: v for k, v in manifest2.items() if k != "created_utc"}
    assert m1_without_ts == m2_without_ts


def test_tree_hash_independent_of_created_utc(tmp_path):
    # Different *calendar years* (not just a different time-of-day): no
    # rendered file (including LICENSE) may embed anything derived from
    # `now`, so created_utc must differ while tree_hash and every per-file
    # sha256 stay identical.
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    spec = report_spec()
    other_year = datetime(2030, 6, 1, 8, 30, 0, tzinfo=timezone.utc)
    manifest1 = build_project(dict(spec), ws1, now=FIXED_NOW)
    manifest2 = build_project(dict(spec), ws2, now=other_year)
    assert manifest1["created_utc"] != manifest2["created_utc"]
    assert manifest1["created_utc"].startswith("2024")
    assert manifest2["created_utc"].startswith("2030")
    assert manifest1["tree_hash"] == manifest2["tree_hash"]
    files1 = {f["path"]: f["sha256"] for f in manifest1["files"]}
    files2 = {f["path"]: f["sha256"] for f in manifest2["files"]}
    assert files1 == files2


def test_tree_hash_independent_of_created_utc_python_tool(tmp_path):
    # Same check for the other template, since LICENSE is rendered
    # independently in each template's render function.
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    spec = python_spec()
    other_year = datetime(1999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    manifest1 = build_project(dict(spec), ws1, now=FIXED_NOW)
    manifest2 = build_project(dict(spec), ws2, now=other_year)
    assert manifest1["created_utc"] != manifest2["created_utc"]
    assert manifest1["tree_hash"] == manifest2["tree_hash"]
    files1 = {f["path"]: f["sha256"] for f in manifest1["files"]}
    files2 = {f["path"]: f["sha256"] for f in manifest2["files"]}
    assert files1 == files2


def test_license_text_has_no_year(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    license_text = (tmp_path / manifest["name"] / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) erku-1f916" in license_text
    for year in ("2024", "2023", "2025", "1999", "2030"):
        assert year not in license_text


# --- manifest validity ----------------------------------------------------------

def test_manifest_schema_keys_present(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    expected_keys = {
        "manifest_version", "name", "template", "listing_id", "grant_slug",
        "created_utc", "source_hash", "files", "tree_hash", "generator",
        "executes_community_commands",
    }
    assert expected_keys <= manifest.keys()
    assert manifest["manifest_version"] == "1f916.project.v1"
    assert manifest["generator"] == "f916.builder"
    assert manifest["executes_community_commands"] is False
    assert manifest["created_utc"] == "2024-03-15T12:00:00Z"


def test_manifest_files_sorted_by_path(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    paths = [f["path"] for f in manifest["files"]]
    assert paths == sorted(paths)


def test_manifest_tree_hash_matches_recomputation(tmp_path):
    manifest = build_project(report_spec(), tmp_path, now=FIXED_NOW)
    assert manifest["tree_hash"] == recompute_tree_hash(manifest)


def test_manifest_written_to_disk_matches_return_value(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    on_disk = json.loads(
        (tmp_path / manifest["name"] / "project.manifest.json").read_text(encoding="utf-8")
    )
    assert on_disk == manifest


def test_manifest_file_sha256_matches_actual_file_bytes(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    project_dir = tmp_path / manifest["name"]
    for record in manifest["files"]:
        data = (project_dir / record["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
        assert len(data) == record["bytes"]


# --- template builds ------------------------------------------------------------

def test_python_tool_template_builds_expected_files(tmp_path):
    manifest = build_project(python_spec(), tmp_path, now=FIXED_NOW)
    project_dir = tmp_path / manifest["name"]
    paths = {f["path"] for f in manifest["files"]}
    assert "README.md" in paths
    assert "LICENSE" in paths
    assert "pyproject.toml" in paths
    assert "src/erku_1f916_tiny_tool/__init__.py" in paths
    assert "tests/test_smoke.py" in paths
    assert (project_dir / "tests" / "test_smoke.py").exists()
    readme = (project_dir / "README.md").read_text(encoding="utf-8")
    assert "listing_id: 41" in readme
    assert "source_hash" in readme
    assert "executes no community-supplied commands" in readme
    license_text = (project_dir / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "erku-1f916" in license_text
    init_text = (project_dir / "src" / "erku_1f916_tiny_tool" / "__init__.py").read_text(encoding="utf-8")
    assert "42" in init_text
    assert "subprocess" not in init_text
    assert "eval(" not in init_text
    assert "exec(" not in init_text


def test_static_report_template_builds_expected_files(tmp_path):
    manifest = build_project(report_spec(), tmp_path, now=FIXED_NOW)
    project_dir = tmp_path / manifest["name"]
    paths = {f["path"] for f in manifest["files"]}
    assert "README.md" in paths
    assert "LICENSE" in paths
    assert "index.html" in paths
    assert "data/report.json" in paths
    assert "tests/test_report.py" in paths
    assert (project_dir / "tests" / "test_report.py").exists()
    report = json.loads((project_dir / "data" / "report.json").read_text(encoding="utf-8"))
    assert report["fields"] == {"score": 87, "verdict": "pass"}
    assert report["name"] == "erku-1f916-tiny-report"


def test_static_report_escapes_html_in_spec_content(tmp_path):
    spec = report_spec(
        title="<script>alert(1)</script>",
        description="Safe & sound <b>bold</b>",
        content={"note": "<img src=x onerror=alert(1)>"},
    )
    manifest = build_project(spec, tmp_path, now=FIXED_NOW)
    project_dir = tmp_path / manifest["name"]
    html_text = (project_dir / "index.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "<img src=x onerror=alert(1)>" not in html_text


# --- never executes spec content -------------------------------------------------

def test_builder_module_never_uses_dangerous_calls():
    source = Path("f916/builder.py").read_text(encoding="utf-8")
    for forbidden in ("import subprocess", "subprocess.", "os.system(", "eval(", "exec("):
        assert forbidden not in source
