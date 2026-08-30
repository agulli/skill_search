from skill_engine.parse import (
    classify_path,
    parse_skill,
    quality_score,
    skill_slug_from_path,
)

GOOD = """---
name: pdf-processing
description: Extract text, tables and form fields from PDF documents, and fill or merge them.
license: Apache-2.0
allowed-tools: Read, Bash, Write
metadata:
  category: documents
---

# PDF Processing

Use this skill when working with PDF files.

## Extracting text

Run `scripts/extract.py` to pull text out of a PDF. See [the reference](references/forms.md)
for the form-field API.
"""


def test_parses_required_fields():
    s = parse_skill(GOOD, "skills/pdf-processing/SKILL.md")
    assert s.valid, s.problems
    assert s.name == "pdf-processing"
    assert s.description.startswith("Extract text")
    assert s.license == "Apache-2.0"
    assert s.allowed_tools == ["Read", "Bash", "Write"]
    assert s.metadata == {"category": "documents"}
    assert s.heading == "PDF Processing"
    assert "scripts/extract.py" in s.resources
    assert "references/forms.md" in s.resources
    assert not s.body.startswith("---")


def test_underscore_and_hyphen_keys_are_equivalent():
    s = parse_skill(
        "---\nname: x-y\ndescription: " + "d" * 60 + "\nallowed_tools: [Read]\n---\n\n"
        + "body " * 30
    )
    assert s.allowed_tools == ["Read"]


def test_rejects_missing_frontmatter():
    s = parse_skill("# Just a readme\n\nNothing structured here at all, really.")
    assert not s.valid
    assert "no YAML frontmatter" in s.invalid_reason


def test_bad_name_slug_is_a_warning_not_a_rejection():
    s = parse_skill("---\nname: Not A Slug\ndescription: " + "d" * 60 + "\n---\n\n" + "b" * 100)
    assert s.valid  # still a usable skill, just off-spec
    assert "slug" in s.notes


def test_overlong_description_stays_indexed():
    """Real first-party skills exceed the 1024-char limit; dropping them is worse
    than indexing them with a lower score."""
    s = parse_skill("---\nname: ok\ndescription: " + "d" * 1100 + "\n---\n\n" + "b" * 100)
    assert s.valid
    assert "1024" in s.notes
    assert not s.invalid_reason


def test_warnings_cost_quality_points():
    clean = parse_skill("---\nname: ok-name\ndescription: " + "d" * 200 + "\n---\n\n" + "b" * 500)
    scruffy = parse_skill("---\nname: Bad Name\ndescription: " + "d" * 1100 + "\n---\n\n" + "b" * 500)
    assert quality_score(clean, stars=100) > quality_score(scruffy, stars=100)


def test_rejects_file_with_no_required_fields():
    s = parse_skill("---\ntitle: something else\n---\n\n" + "b" * 100)
    assert not s.valid
    assert "missing name" in s.invalid_reason
    assert "missing description" in s.invalid_reason


def test_rejects_broken_yaml():
    s = parse_skill("---\nname: [unclosed\n---\n\nbody text here that is long enough")
    assert not s.valid
    assert "invalid YAML" in s.invalid_reason


def test_survives_non_mapping_frontmatter():
    s = parse_skill("---\n- a\n- b\n---\n\nbody text here that is long enough to pass")
    assert not s.valid
    assert "not a mapping" in s.invalid_reason


def test_unknown_keys_are_preserved():
    s = parse_skill(
        "---\nname: ok\ndescription: " + "d" * 60 + "\nfuture-field: 42\n---\n\n" + "b" * 100
    )
    assert s.valid
    assert s.extra["future-field"] == 42


def test_content_hash_is_stable_and_content_addressed():
    assert parse_skill(GOOD).content_hash == parse_skill(GOOD).content_hash
    assert parse_skill(GOOD).content_hash != parse_skill(GOOD + "x").content_hash


def test_classify_path():
    assert classify_path("SKILL.md") == "root"
    assert classify_path("skills/foo/SKILL.md") == "skills-dir"
    assert classify_path(".claude/skills/foo/SKILL.md") == "claude-project"
    assert classify_path("plugins/bar/skills/foo/SKILL.md") == "plugin"
    assert classify_path("docs/random/SKILL.md") == "other"


def test_slug_falls_back_to_directory():
    assert skill_slug_from_path("skills/pdf-processing/SKILL.md") == "pdf-processing"


def test_quality_score_ordering():
    s = parse_skill(GOOD)
    popular = quality_score(s, stars=5000, days_since_push=3, has_license=True)
    obscure = quality_score(s, stars=0, days_since_push=900)
    forked = quality_score(s, stars=5000, days_since_push=3, has_license=True, is_fork=True)
    duped = quality_score(s, stars=5000, days_since_push=3, has_license=True,
                          duplicate_count=50)
    assert popular > obscure
    assert popular > forked
    assert popular > duped
    assert 0 <= obscure <= 100 and 0 <= popular <= 100
