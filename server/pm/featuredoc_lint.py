"""Directory-level feature-doc validator (issue #2166).

Ships as the single source of truth for whether ``docs/product/features/*.md``
will be fully indexed by pm-sync. It reuses :func:`parse_feature_doc`
(``server/pm/featuredoc.py``) and :func:`plan_feature_index`
(``server/pm/feature_index.py``) — the exact functions ``pm_sync._feature_index``
calls — so the lint and the indexer cannot disagree about which doc is broken;
a check re-implemented here instead of reused would drift the moment either
side changed.

Scope: every check below answers "would the indexer silently DROP or silently
COLLIDE this doc", which is the failure shape behind the KinDash incident this
issue cites (a bad doc vanishes from the map with only a log line to notice
it by). It is deliberately narrower than KinDash's own repo-local
``feature-doc-lint.sh``, whose ``owner:``/``added:``/root-category/kebab-title/
line-cap checks are that repo's OWN style policy, not fields the indexer
(``server/pm/featuredoc.py``) reads or enforces — this repo's own
``docs/product/features/*.md`` carry no ``owner:``/``added:`` and use the
archaeology ``links: {code: [...], prs: [...]}`` dict shape, so a lint that
required KinDash's dialect would fail ViberPit's own docs. A product that
wants that stricter house style still can — the KinDash script is free to stay
in KinDash and layer its style rules on top of a clean run of this one.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from server.pm.feature_index import plan_feature_index
from server.pm.featuredoc import FeatureDoc, FeatureDocError, parse_feature_doc

DOCS_DIR = "docs/product/features"


@dataclass(frozen=True)
class LintError:
    path: str
    message: str


@dataclass(frozen=True)
class LintResult:
    checked: int
    errors: tuple[LintError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def lint_feature_docs(root: str | Path) -> LintResult:
    """Validate every ``*.md`` under ``<root>/docs/product/features/``.

    A missing or empty docs directory is a deliberate fail-open (mirrors
    KinDash's own lint): a repo that has not adopted the capability map yet
    must not be blocked by this gate.
    """
    docs_dir = Path(root) / DOCS_DIR
    errors: list[LintError] = []
    parsed: list[tuple[str, FeatureDoc]] = []
    seen_slugs: dict[str, str] = {}

    paths = sorted(docs_dir.glob("*.md")) if docs_dir.is_dir() else []
    for doc_path in paths:
        rel = f"{DOCS_DIR}/{doc_path.name}"
        text = doc_path.read_text(encoding="utf-8")
        try:
            doc = parse_feature_doc(text, path=rel)
        except FeatureDocError as e:
            errors.append(LintError(rel, str(e)))
            continue
        if doc.slug in seen_slugs:
            errors.append(LintError(
                rel, f"duplicate slug '{doc.slug}' — already claimed by {seen_slugs[doc.slug]}"))
            continue
        seen_slugs[doc.slug] = rel
        parsed.append((rel, doc))

    # plan_feature_index is the SAME planner pm_sync applies; a doc whose
    # parent it cannot resolve is not dropped (it lands at root) but the
    # warning it emits there is worth failing CI over rather than only
    # discovering it in the product's roadmap-hygiene notice after merge.
    plan = plan_feature_index([], parsed)
    for warning in plan.warnings:
        slug, _, detail = warning.partition(": ")
        errors.append(LintError(seen_slugs.get(slug, slug), detail or warning))

    return LintResult(checked=len(paths), errors=tuple(errors))


def format_errors(result: LintResult) -> list[str]:
    """GitHub Actions annotation lines, one per error — the same
    ``::error file=...::message`` shape KinDash's shell lint used, so a CI run
    switching to this CLI shows identical-looking annotations."""
    return [f"::error file={e.path}::feature-doc-lint: {e.message}" for e in result.errors]
