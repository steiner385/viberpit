"""Committed-artifact layout (constitution VI: single source of truth).

Pure: no imports from server.db / server.automations / server.sessions, so the
stage engine's neighbours (``explain.py``) can import it and stay total.
"""
from __future__ import annotations

SDLC_DOCS_DIR = "docs/sdlc"

#: Named artifact-path conventions a product may opt into (#2416) via
#: ``automation_policy.artifact_layout`` (``server/automations/product_policy.py``).
#: Every template takes the same two values every existing caller already has —
#: ``{slug}``/``{kind}`` — so adding a layout never means adding a caller
#: parameter beyond the ``layout`` name itself.
ARTIFACT_LAYOUTS: dict[str, str] = {
    "docs_sdlc": f"{SDLC_DOCS_DIR}/{{slug}}/{{kind}}.md",
    # spec-kit's own convention (``specs/<NNN>-<slug>/{spec,plan,tasks}.md``) —
    # the ``NNN-`` prefix, when a product uses one, already lives in ``slug``
    # itself (the initiative slug IS the spec-kit feature directory name).
    "speckit": "specs/{slug}/{kind}.md",
}

DEFAULT_ARTIFACT_LAYOUT = "docs_sdlc"


def artifact_path(slug: str, kind: str, layout: str | None = None) -> str:
    """Repo-relative path of an initiative's committed artifact — the one place
    the intake/synthesizer loops, the prompt builders and the consequence
    explainer agree on layout.

    *layout* is the owning product's configured convention (an
    ``ARTIFACT_LAYOUTS`` key). ``None`` or an unrecognized name falls back to
    ``DEFAULT_ARTIFACT_LAYOUT`` rather than raising — a stale or unset layout
    name must degrade to the historical path, not break every artifact path
    the loop resolves."""
    template = ARTIFACT_LAYOUTS.get(layout or DEFAULT_ARTIFACT_LAYOUT,
                                    ARTIFACT_LAYOUTS[DEFAULT_ARTIFACT_LAYOUT])
    return template.format(slug=slug, kind=kind)
