"""Canonical deploy-environment classifier — the single source of truth for
mapping a raw GitHub deployment-environment name to a metric bucket.

GitHub environment names are free-form and repo-specific: ``steiner385/ccswitch``
uses a bare ``"production"`` while ``cairnea/KinDash`` namespaces two apps as
``"kindash / production"``, ``"kindash / qa"``, ``"kindash / admin-production"``,
etc. The old ``"production" in deployed_envs`` exact-match silently failed on the
namespaced names, stranding every merged KinDash PR in "awaiting prod" forever.

Buckets (SPEC — flat 3-way):

- ``"admin"`` — ANY admin env (``admin``, ``admin-qa``, ``admin-production``);
  admin wins over prod so ``admin-production`` is admin, not prod.
- ``"prod"`` — the main app's production (``production`` / ``prod``).
- ``"qa"`` — the main app's qa / staging.
- ``None`` — matches nothing (preview/ad-hoc envs); excluded from all metrics.

Both the corrected ``deployed`` bool (``pr_status``) and the per-repo backlog
summary (``deploy_status``) route through this one function — never re-match env
strings inline (that is the parallel-implementation trap this module closes).
"""
from __future__ import annotations

import re
from typing import Literal

EnvCategory = Literal["qa", "prod", "admin"]

# Split a name into tokens on the separators GitHub / users put between segments
# ("kindash / admin-production" → {kindash, admin, production}). Token membership
# (not substring) avoids false hits like "administration" or "reproduce".
_SEP = re.compile(r"[\s/_-]+")

_PROD_TOKENS = frozenset({"prod", "production"})
_QA_TOKENS = frozenset({"qa", "staging", "stage"})


def classify_env(name: str) -> EnvCategory | None:
    """Map a raw deploy-environment name to its bucket, or None if unrecognized."""
    tokens = set(_SEP.split(name.strip().lower()))
    if "admin" in tokens:          # admin wins: admin-production → admin
        return "admin"
    if _PROD_TOKENS & tokens:
        return "prod"
    if _QA_TOKENS & tokens:
        return "qa"
    return None
