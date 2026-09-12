import re

from f916.builder import NAME_RE
from f916.project_policy import load_project_templates, qualify

SEED_TEMPLATE = {"id": "rail-window-report", "project_template": "static-report",
                  "title_contains": ["window"], "name_suffix": "rail-window"}


def evaluation(**updates):
    base = {"classification": "project_required", "listing_id": 41, "source_hash": "a" * 64}
    return base | updates


def listing(**updates):
    base = {"listing_id": 41, "title": "Build a rail window tracker", "condition": "Implement a fix and open a pull request."}
    return base | updates


# --- load_project_templates -------------------------------------------------

def test_load_project_templates_reads_the_seed_allowlist():
    templates = load_project_templates()
    assert any(t.get("id") == "rail-window-report" for t in templates)


def test_load_project_templates_returns_empty_list_on_missing_file(tmp_path):
    assert load_project_templates(tmp_path / "does-not-exist.json") == []


def test_load_project_templates_returns_empty_list_on_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_project_templates(path) == []


def test_load_project_templates_returns_empty_list_on_wrong_top_level_shape(tmp_path):
    path = tmp_path / "wrong.json"
    path.write_text('{"not": "a list"}', encoding="utf-8")
    assert load_project_templates(path) == []


# --- qualify -----------------------------------------------------------------

def test_qualify_matches_template_and_builds_a_normalized_spec():
    spec = qualify(evaluation(), listing(), [SEED_TEMPLATE], existing_project_count=0, max_projects=3)
    assert spec is not None
    assert spec["name"] == "erku-1f916-rail-window-41"
    assert re.fullmatch(NAME_RE, spec["name"])
    assert spec["template"] == "static-report"
    assert spec["listing_id"] == 41
    assert spec["source_hash"] == "a" * 64
    assert spec["content"]["listing_id"] == 41
    # No free text (title/condition prose) is parsed into the content payload.
    assert "condition" not in spec["content"]
    assert "Implement a fix" not in str(spec["content"])


def test_qualify_returns_none_when_not_project_required():
    for classification in ("supported", "unsupported", None, "project_required_typo"):
        assert qualify(evaluation(classification=classification), listing(), [SEED_TEMPLATE],
                        existing_project_count=0, max_projects=3) is None


def test_qualify_returns_none_when_no_template_matches_title():
    non_matching = listing(title="Build a lighthouse tracker")
    assert qualify(evaluation(), non_matching, [SEED_TEMPLATE], existing_project_count=0, max_projects=3) is None


def test_qualify_returns_none_when_over_or_at_quota():
    assert qualify(evaluation(), listing(), [SEED_TEMPLATE], existing_project_count=3, max_projects=3) is None
    assert qualify(evaluation(), listing(), [SEED_TEMPLATE], existing_project_count=4, max_projects=3) is None


def test_qualify_allows_under_quota():
    assert qualify(evaluation(), listing(), [SEED_TEMPLATE], existing_project_count=2, max_projects=3) is not None


def test_qualify_requires_matching_funder_when_template_specifies_one():
    template = {**SEED_TEMPLATE, "funder": "acme"}
    assert qualify(evaluation(), listing(funder="acme"), [template], existing_project_count=0, max_projects=3) is not None
    assert qualify(evaluation(), listing(funder="other"), [template], existing_project_count=0, max_projects=3) is None
    assert qualify(evaluation(), listing(), [template], existing_project_count=0, max_projects=3) is None


def test_qualify_sanitizes_name_suffix_to_satisfy_builder_name_re():
    weird_template = {"id": "x", "project_template": "static-report",
                       "title_contains": ["window"], "name_suffix": "Rail Window!! "}
    spec = qualify(evaluation(), listing(), [weird_template], existing_project_count=0, max_projects=3)
    assert spec is not None
    assert re.fullmatch(NAME_RE, spec["name"])


def test_qualify_never_raises_on_malformed_input():
    assert qualify(None, None, None, existing_project_count=0, max_projects=3) is None
    assert qualify({}, {}, "not-a-list", existing_project_count=0, max_projects=3) is None
    assert qualify(evaluation(listing_id="not-an-int"), listing(), [SEED_TEMPLATE],
                    existing_project_count=0, max_projects=3) is None
    assert qualify(evaluation(source_hash="short"), listing(), [SEED_TEMPLATE],
                    existing_project_count=0, max_projects=3) is None
    assert qualify(evaluation(), listing(), [{"project_template": "not-a-real-template",
                    "title_contains": ["window"], "name_suffix": "x"}],
                    existing_project_count=0, max_projects=3) is None
