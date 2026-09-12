"""Deterministic, offline qualification of `project_required` listings
against an operator-curated project-template allowlist (Task B1).

No network access happens here, and nothing here ever raises: a qualifying
listing plus a matching template produces a normalized project spec for
`f916.builder.build_project`; anything else, or any doubt at all, produces
`None`. The allowlist -- not the listing author -- is the trust boundary:
a template only matches on structured signals (an optional `funder`
equality and required `title_contains` substrings against the listing's
own `title` field), never by interpreting free text into parameters.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .builder import NAME_RE, TEMPLATES

DEFAULT_TEMPLATES_PATH = Path(__file__).resolve().parents[1] / "config" / "project_templates.json"

_NAME_RE = re.compile(NAME_RE)
_SOURCE_HASH_RE = re.compile(r'^[0-9a-f]{64}$')
_SANITIZE_RE = re.compile(r'[^a-z0-9-]+')


def load_project_templates(path=None) -> list:
    """Load the operator-curated project-template allowlist.

    Returns `[]` on any error -- missing file, invalid JSON, or an
    unexpected top-level shape -- rather than raising, since a broken
    allowlist file must fail closed (no templates match, nothing qualifies)
    and never crash the opportunity cycle.
    """
    target = Path(path) if path is not None else DEFAULT_TEMPLATES_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _template_matches(template: dict, listing: dict) -> bool:
    funder = template.get("funder")
    if funder is not None and listing.get("funder") != funder:
        return False
    terms = template.get("title_contains")
    if not isinstance(terms, list) or not terms:
        return False
    title = str(listing.get("title", "")).casefold()
    return all(isinstance(term, str) and term.casefold() in title for term in terms)


def _sanitize_name_component(text) -> str:
    return _SANITIZE_RE.sub('-', str(text).casefold()).strip('-')


def qualify(evaluation: dict, listing: dict, templates: list, *,
            existing_project_count: int, max_projects: int):
    """Return a normalized project spec for `build_project`, or `None`.

    Qualifies ONLY when: `evaluation['classification'] == 'project_required'`;
    some template in `templates` matches the listing by structured signals
    (funder equality, if the template specifies one, AND every
    `title_contains` substring present in the casefolded title); and
    `existing_project_count < max_projects`. Never raises -- any
    unexpected shape or doubt returns `None`.
    """
    try:
        if not isinstance(evaluation, dict) or evaluation.get("classification") != "project_required":
            return None
        if not isinstance(listing, dict) or not isinstance(templates, list):
            return None
        if not isinstance(existing_project_count, int) or isinstance(existing_project_count, bool):
            return None
        if not isinstance(max_projects, int) or isinstance(max_projects, bool):
            return None
        if existing_project_count >= max_projects:
            return None

        listing_id = evaluation.get("listing_id")
        if not isinstance(listing_id, int) or isinstance(listing_id, bool) or listing_id <= 0:
            return None
        source_hash = evaluation.get("source_hash")
        if not isinstance(source_hash, str) or not _SOURCE_HASH_RE.fullmatch(source_hash):
            return None

        matched = None
        for template in templates:
            if not isinstance(template, dict):
                continue
            if template.get("project_template") not in TEMPLATES:
                continue
            if _template_matches(template, listing):
                matched = template
                break
        if matched is None:
            return None

        suffix = _sanitize_name_component(matched.get("name_suffix", ""))
        if not suffix:
            return None
        name = f"erku-1f916-{suffix}-{listing_id}"
        if not _NAME_RE.fullmatch(name):
            return None

        title = listing.get("title")
        title = title if isinstance(title, str) and title else matched.get("id", name)
        description = f"Deterministic, read-only public report for listing {listing_id}."

        # Content is a snapshot of the listing's OWN structured fields only
        # -- no prose is parsed or interpreted, and nothing here is a claim
        # beyond "the listing reported this value".
        content = {"listing_id": listing_id}
        for field in ("funder", "economics", "payload_hash", "grant_slug"):
            if field in listing:
                content[field] = listing[field]

        return {
            "name": name,
            "template": matched["project_template"],
            "title": title,
            "description": description,
            "listing_id": listing_id,
            "source_hash": source_hash,
            "content": content,
        }
    except Exception:
        return None
