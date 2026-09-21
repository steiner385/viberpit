"""Directory-level feature-doc validator tests (issue #2166)."""
from server.pm.featuredoc_lint import format_errors, lint_feature_docs

GOOD_A = """---
slug: a
title: A
state: live
---
# A

body
"""

GOOD_B = """---
slug: b
title: B
state: proposed
parent: a
---
# B

body
"""


def _write(tmp_path, name, text):
    d = tmp_path / "docs" / "product" / "features"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def test_no_docs_directory_passes_with_nothing_checked(tmp_path):
    result = lint_feature_docs(tmp_path)
    assert result.ok
    assert result.checked == 0


def test_valid_docs_pass(tmp_path):
    _write(tmp_path, "a.md", GOOD_A)
    _write(tmp_path, "b.md", GOOD_B)
    result = lint_feature_docs(tmp_path)
    assert result.ok
    assert result.checked == 2


def test_unparseable_doc_is_reported_with_path_and_reason(tmp_path):
    _write(tmp_path, "bad.md", "---\nslug: bad\n")  # unterminated fence
    result = lint_feature_docs(tmp_path)
    assert not result.ok
    assert result.checked == 1
    (e,) = result.errors
    assert e.path == "docs/product/features/bad.md"
    assert "unterminated" in e.message


def test_one_bad_doc_does_not_hide_a_good_one(tmp_path):
    _write(tmp_path, "a.md", GOOD_A)
    _write(tmp_path, "bad.md", "---\nslug: bad\n")
    result = lint_feature_docs(tmp_path)
    assert result.checked == 2
    assert [e.path for e in result.errors] == ["docs/product/features/bad.md"]


def test_duplicate_slug_across_two_docs_is_an_error(tmp_path):
    # Docs are visited in path-sorted order, so "a.md" claims the slug first
    # and "a2.md" is reported as the duplicate.
    _write(tmp_path, "a.md", GOOD_A)
    dup = GOOD_A.replace("# A", "# A Again")
    _write(tmp_path, "a2.md", dup)
    result = lint_feature_docs(tmp_path)
    assert not result.ok
    (e,) = result.errors
    assert e.path == "docs/product/features/a2.md"
    assert "duplicate slug 'a'" in e.message
    assert "docs/product/features/a.md" in e.message


def test_unknown_parent_is_reported(tmp_path):
    orphan = GOOD_B.replace("parent: a", "parent: does-not-exist")
    _write(tmp_path, "b.md", orphan)
    result = lint_feature_docs(tmp_path)
    assert not result.ok
    (e,) = result.errors
    assert e.path == "docs/product/features/b.md"
    assert "parent 'does-not-exist' not found" in e.message


def test_format_errors_uses_github_actions_annotation_shape(tmp_path):
    _write(tmp_path, "bad.md", "---\nslug: bad\n")
    result = lint_feature_docs(tmp_path)
    (line,) = format_errors(result)
    assert line.startswith("::error file=docs/product/features/bad.md::feature-doc-lint: ")


def test_archaeology_style_docs_with_no_owner_or_added_still_pass(tmp_path):
    """ViberPit's own docs/product/features/*.md carry no owner:/added: and use
    the {code: [...], prs: [...]} links dict — the lint must not require a
    stricter dialect than the indexer itself accepts (module docstring)."""
    text = """---
slug: dogfood
title: Dogfood
state: delivering
links:
  code: [server/pm/featuredoc.py]
  prs: ['#2166']
---
# Dogfood

body
"""
    _write(tmp_path, "dogfood.md", text)
    result = lint_feature_docs(tmp_path)
    assert result.ok
