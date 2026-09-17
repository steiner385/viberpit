"""Committed-artifact layout (constitution VI: single source of truth).

Pure: no imports from server.db / server.automations / server.sessions, so the
stage engine's neighbours (``explain.py``) can import it and stay total.
"""
from __future__ import annotations

SDLC_DOCS_DIR = "docs/sdlc"


def artifact_path(slug: str, kind: str) -> str:
    """Repo-relative path of an initiative's committed artifact — the one place
    the intake/synthesizer loops, the prompt builders and the consequence
    explainer agree on layout."""
    return f"{SDLC_DOCS_DIR}/{slug}/{kind}.md"
