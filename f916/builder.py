"""Deterministic, bounded project builder.

Turns a normalized project spec into a small, safe, reproducible project
tree built from allowlisted templates. This module never executes
model-generated (or any other) shell commands: rendering is pure string
templating with escaping, and nothing here calls subprocess, os.system,
eval, or exec.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from invariants import redact

NAME_RE = r'^erku-1f916-[a-z0-9][a-z0-9-]{0,50}$'
MAX_FILES = 40
MAX_TOTAL_BYTES = 512_000
MAX_FILE_BYTES = 128_000
TEMPLATES = ('python-tool', 'static-report')
MANIFEST_VERSION = '1f916.project.v1'
GENERATOR = 'f916.builder'

_NAME_RE = re.compile(NAME_RE)
_SOURCE_HASH_RE = re.compile(r'^[0-9a-f]{64}$')
_PRIVATE_KEY_RE = re.compile(r'\b0x[0-9a-fA-F]{64}\b')
_SK_TOKEN_RE = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}\b')


def _fail(reason: str) -> None:
    raise ValueError(reason)


def _validate_spec(spec: dict) -> None:
    if not isinstance(spec, dict):
        _fail('spec_not_a_dict')
    name = spec.get('name')
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        _fail('invalid_name')
    template = spec.get('template')
    if template not in TEMPLATES:
        _fail('invalid_template')
    if not isinstance(spec.get('title', ''), str):
        _fail('invalid_title')
    if not isinstance(spec.get('description', ''), str):
        _fail('invalid_description')
    listing_id = spec.get('listing_id')
    if listing_id is not None and not isinstance(listing_id, int):
        _fail('invalid_listing_id')
    grant_slug = spec.get('grant_slug')
    if grant_slug is not None and not isinstance(grant_slug, str):
        _fail('invalid_grant_slug')
    source_hash = spec.get('source_hash')
    if source_hash is not None and (not isinstance(source_hash, str) or not _SOURCE_HASH_RE.fullmatch(source_hash)):
        _fail('invalid_source_hash')


def _check_secret_like(text: str) -> None:
    if redact(text) != text:
        _fail('secret_like_content')
    if _PRIVATE_KEY_RE.search(text) or _SK_TOKEN_RE.search(text):
        _fail('secret_like_content')


def _provenance_line(spec: dict) -> str:
    listing_id = spec.get('listing_id')
    grant_slug = spec.get('grant_slug')
    source_hash = spec.get('source_hash')
    parts = []
    parts.append(f'listing_id: {listing_id}' if listing_id is not None else 'listing_id: none')
    parts.append(f'grant_slug: {grant_slug}' if grant_slug is not None else 'grant_slug: none')
    parts.append(f'source_hash: {source_hash}' if source_hash is not None else 'source_hash: none')
    return ' | '.join(parts)


def _license_text() -> str:
    # No year is embedded here on purpose: tree_hash must be independent of
    # created_utc, and a year derived from `now` would leak timestamp-derived
    # content into every hashed file across a calendar-year boundary.
    return (
        'MIT License\n\nCopyright (c) erku-1f916\n\n'
        'Permission is hereby granted, free of charge, to any person obtaining a copy '
        'of this software and associated documentation files (the "Software"), to deal '
        'in the Software without restriction, including without limitation the rights '
        'to use, copy, modify, merge, publish, distribute, sublicense, and/or sell '
        'copies of the Software, and to permit persons to whom the Software is '
        'furnished to do so, subject to the following conditions:\n\n'
        'The above copyright notice and this permission notice shall be included in all '
        'copies or substantial portions of the Software.\n\n'
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR '
        'IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, '
        'FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE '
        'AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER '
        'LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, '
        'OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE '
        'SOFTWARE.\n'
    )


def _readme_text(spec: dict, extra: str = '') -> str:
    title = spec.get('title', '') or spec['name']
    description = spec.get('description', '')
    body = (
        f'# {title}\n\n'
        f'{description}\n\n'
        '## Provenance\n\n'
        f'- {_provenance_line(spec)}\n'
        '- generator: f916.builder\n'
        '- This project executes no community-supplied commands. All content below\n'
        '  was rendered as data via pure string templating; nothing here was\n'
        '  executed to produce it.\n'
    )
    if extra:
        body += f'\n{extra}\n'
    return body


def _py_identifier(text: str) -> str:
    candidate = re.sub(r'[^0-9a-zA-Z_]', '_', text)
    if not candidate or candidate[0].isdigit():
        candidate = f'_{candidate}'
    return candidate


def _render_python_tool(spec: dict) -> dict:
    name = spec['name']
    pkgname = name.replace('-', '_')
    content = spec.get('content') or {}
    if not isinstance(content, dict):
        _fail('invalid_content')
    func_name = _py_identifier(str(content.get('function_name', 'run')))
    # The value is rendered as a *string literal* (repr), never interpreted
    # or executed. It is pure data embedded in a docstring-free constant.
    value = content.get('value', spec.get('title', name))
    value_literal = repr(str(value))

    files: dict[str, str] = {}
    files['README.md'] = _readme_text(spec)
    files['LICENSE'] = _license_text()
    files['pyproject.toml'] = (
        '[build-system]\n'
        'requires = ["setuptools>=70"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\n'
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        f'description = {json.dumps(spec.get("description", ""))}\n'
        'requires-python = ">=3.9"\n'
    )
    files[f'src/{pkgname}/__init__.py'] = (
        '"""Generated by f916.builder. Data-derived module; nothing here is executed '
        'from spec input beyond string templating."""\n\n'
        f'VALUE = {value_literal}\n\n\n'
        f'def {func_name}():\n'
        f'    """Return the configured value for {name}."""\n'
        '    return VALUE\n'
    )
    files['tests/test_smoke.py'] = (
        f'from {pkgname} import {func_name}, VALUE\n\n\n'
        f'def test_{func_name}_returns_configured_value():\n'
        f'    assert {func_name}() == VALUE\n'
        f'    assert isinstance({func_name}(), str)\n'
    )
    return files


def _render_static_report(spec: dict) -> dict:
    name = spec['name']
    content = spec.get('content') or {}
    if not isinstance(content, dict):
        _fail('invalid_content')
    # Serialize deterministically; this is the single source of truth for
    # both index.html and data/report.json.
    report = {
        'name': name,
        'title': spec.get('title', ''),
        'description': spec.get('description', ''),
        'fields': content,
    }
    report_json = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n'

    title_html = html.escape(str(spec.get('title', '')))
    description_html = html.escape(str(spec.get('description', '')))
    rows = []
    for key in sorted(content.keys()):
        rows.append(
            f'      <tr><th>{html.escape(str(key))}</th>'
            f'<td>{html.escape(str(content[key]))}</td></tr>'
        )
    rows_html = '\n'.join(rows)

    files: dict[str, str] = {}
    files['README.md'] = _readme_text(spec, extra='See `index.html` and `data/report.json`.')
    files['LICENSE'] = _license_text()
    files['index.html'] = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<title>{title_html}</title>\n</head>\n<body>\n'
        f'  <h1>{title_html}</h1>\n'
        f'  <p>{description_html}</p>\n'
        '  <table>\n'
        f'{rows_html}\n'
        '  </table>\n'
        '</body>\n</html>\n'
    )
    files['data/report.json'] = report_json
    files['tests/test_report.py'] = (
        'import json\n'
        'from pathlib import Path\n\n\n'
        'def test_report_json_parses_and_matches_declared_fields():\n'
        "    data = json.loads((Path(__file__).parent.parent / 'data' / 'report.json').read_text(encoding='utf-8'))\n"
        f'    assert data["name"] == {json.dumps(name)}\n'
        f'    assert data["title"] == {json.dumps(spec.get("title", ""))}\n'
        f'    assert data["fields"] == {json.dumps(content, sort_keys=True)}\n'
    )
    return files


def _reject_if_symlinked(base_dir: Path, target: Path) -> None:
    """Reject `target`, or any ancestor between it and `base_dir`, that is a
    symlink (including a dangling one). Checked on the *unresolved* path so a
    symlink is never silently followed — `Path.resolve()` would otherwise
    walk straight through it before we get a chance to look."""
    if target.is_symlink():
        _fail('symlink_target_rejected')
    for parent in target.parents:
        if parent == base_dir:
            break
        if parent.is_symlink():
            _fail('symlink_target_rejected')


def _check_paths(project_dir: Path, files: dict[str, str]) -> None:
    if len(files) > MAX_FILES:
        _fail('too_many_files')
    total = 0
    resolved_root = project_dir.resolve()
    for rel_path, text in files.items():
        if rel_path.startswith('/') or rel_path.startswith('\\'):
            _fail('absolute_path_rejected')
        p = Path(rel_path)
        if p.is_absolute():
            _fail('absolute_path_rejected')
        if '..' in p.parts:
            _fail('path_traversal_rejected')
        unresolved_target = project_dir / p
        _reject_if_symlinked(project_dir, unresolved_target)
        resolved_target = unresolved_target.resolve()
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError:
            _fail('path_escapes_workspace')
        encoded = text.encode('utf-8')
        if len(encoded) > MAX_FILE_BYTES:
            _fail('file_too_large')
        total += len(encoded)
    if total > MAX_TOTAL_BYTES:
        _fail('total_size_exceeded')


def _tree_hash(file_hashes: list[tuple[str, str]]) -> str:
    hasher = hashlib.sha256()
    for path, digest in sorted(file_hashes, key=lambda item: item[0]):
        hasher.update(path.encode('utf-8'))
        hasher.update(b'\x00')
        hasher.update(digest.encode('utf-8'))
        hasher.update(b'\n')
    return hasher.hexdigest()


def build_project(spec: dict, workspace: str | Path, *, now=None) -> dict:
    """Validate spec, render the template into `workspace/<name>/`, and return the
    manifest dict (also written as `project.manifest.json` in that dir).
    spec keys: 'name' (must match NAME_RE), 'template' (in TEMPLATES),
    'title' (str), 'description' (str), optional 'listing_id' (int), 'grant_slug'
    (str), 'source_hash' (64-hex), plus template-specific 'content' fields.
    Enforce every rule below; raise ValueError with a clear reason on any breach.
    Deterministic given the same spec + now."""
    _validate_spec(spec)
    if now is None:
        now = datetime.now(timezone.utc)

    name = spec['name']
    template = spec['template']
    workspace_path = Path(workspace).resolve()
    project_dir = (workspace_path / name)
    # Path.is_symlink() returns True for a symlink even when it dangles
    # (points at a nonexistent target), where .exists() would return False
    # and follow the link. Never gate this on .exists().
    if project_dir.is_symlink():
        _fail('symlink_target_rejected')
    project_dir_resolved = project_dir.resolve()
    try:
        project_dir_resolved.relative_to(workspace_path)
    except ValueError:
        _fail('path_escapes_workspace')

    render_spec = dict(spec)

    if template == 'python-tool':
        files = _render_python_tool(render_spec)
    elif template == 'static-report':
        files = _render_static_report(render_spec)
    else:  # pragma: no cover - guarded by _validate_spec
        _fail('invalid_template')

    for rel_path, text in files.items():
        _check_secret_like(text)

    _check_paths(project_dir, files)

    project_dir.mkdir(parents=True, exist_ok=True)
    file_records = []
    file_hashes = []
    for rel_path in sorted(files):
        text = files[rel_path]
        encoded = text.encode('utf-8')
        target = project_dir / rel_path
        # Re-check right before creating directories: _check_paths ran
        # earlier, and an ancestor could have been swapped for a symlink in
        # the window between that validation pass and this write (TOCTOU),
        # or by an earlier iteration of this very loop. Checking again here
        # also means we never call mkdir(parents=True) through a symlink.
        _reject_if_symlinked(project_dir, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check once more after mkdir: exist_ok=True silently no-ops on
        # an already-existing path, so a swap that landed exactly during the
        # mkdir call would otherwise go unnoticed until the write below.
        _reject_if_symlinked(project_dir, target)
        target.write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        file_records.append({'path': rel_path, 'sha256': digest, 'bytes': len(encoded)})
        file_hashes.append((rel_path, digest))

    file_records.sort(key=lambda rec: rec['path'])
    tree_hash = _tree_hash(file_hashes)

    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'name': name,
        'template': template,
        'listing_id': spec.get('listing_id'),
        'grant_slug': spec.get('grant_slug'),
        'created_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source_hash': spec.get('source_hash'),
        'files': file_records,
        'tree_hash': tree_hash,
        'generator': GENERATOR,
        'executes_community_commands': False,
    }

    manifest_json = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    (project_dir / 'project.manifest.json').write_bytes(manifest_json.encode('utf-8'))

    return manifest
