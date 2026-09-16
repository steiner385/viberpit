"""Fresh, read-only implementation admission evidence shared by preview and dispatch."""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select

from server.db import db_session
from server.db.models import AutomationOperation, Project, ProjectRepo
from server.pm.repo_project import RepoOwnership, gather_repo_identities, resolve_repo_ownership

from .context import AutomationContext
from .product_policy import ImplementationConstraint, ImplementationDecision, decide_implementation, effective_policy
from .target_identity import valid_repo_name


class ProductMissing(LookupError):
    pass


@dataclass(frozen=True)
class ProductCoverage:
    ownership: dict[str, RepoOwnership]
    policies: dict[str, dict | None]
    observed_at: str


async def gather_product_coverage(
    repos: Collection[str] | None = None, *, requested_product: str | None = None,
    verified_repos: Mapping[str, str | None] | None = None,
) -> ProductCoverage:
    """Resolve identities outside DB sessions, then read ownership and policy anew."""
    async with db_session() as db:
        project = None
        if requested_product is not None:
            project = await db.scalar(select(Project).where(Project.slug == requested_product))
            if project is None:
                raise ProductMissing(requested_product)
        registrations = (await db.execute(select(ProjectRepo.project_id, ProjectRepo.repo))).all()
        requested = sorted(set(repos) if repos is not None else {
            repo for pid, repo in registrations if project is not None and pid == project.id and repo})
        candidates = {repo for _, repo in registrations if repo} | set(requested)
    identities = await gather_repo_identities(candidates, verified_repos=verified_repos) if requested else {}
    async with db_session() as db:
        if requested_product is not None:
            project = await db.scalar(select(Project).where(Project.slug == requested_product))
            if project is None:
                raise ProductMissing(requested_product)
            if repos is None:
                # A new registration has no identity evidence and remains unresolved.
                requested = sorted(set((await db.scalars(select(ProjectRepo.repo).where(
                    ProjectRepo.project_id == project.id, ProjectRepo.repo.isnot(None)))).all()))
        ownership = await resolve_repo_ownership(db, requested, canonical_repos=identities)
        policies = {slug: raw for slug, raw in (await db.execute(
            select(Project.slug, Project.automation_policy))).all() if slug is not None}
    return ProductCoverage(ownership, policies, datetime.now(timezone.utc).isoformat())


async def read_dispatch_accounting(
    *, now: datetime | None = None, automation_id: str = "backlog-runner",
) -> dict[str, datetime]:
    """Durable admitted dispatches within the rolling day; corrupt evidence is unknown."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    async with db_session() as db:
        rows = (await db.scalars(select(AutomationOperation).where(
            AutomationOperation.owner == automation_id,
            AutomationOperation.updated_at >= cutoff.replace(tzinfo=None),
        ))).all()
        result = {}
        for row in rows:
            stamp = (row.provenance or {}).get("runner_dispatch", {}).get("accounted_at")
            if stamp is None:
                continue
            at = datetime.fromisoformat(stamp)
            if at.tzinfo is None:
                raise ValueError("Dispatch accounting timestamp has no timezone")
            if at > cutoff:
                result[row.token] = at
    return result


@dataclass(frozen=True)
class ImplementationSnapshot:
    coverage: ProductCoverage
    runner_mode: str | None
    config: dict
    states: dict[str, dict] | None
    accounting: dict[str, datetime] | None

    def decide(
        self, repo: str, *, requested_product: str | None = None,
        trigger: Literal["automatic", "manual"] = "automatic",
        effect: Literal["implementation", "proposal", "dispatch"] = "implementation",
        existing_slot: bool = False, existing_token: str | None = None,
        prerequisites: tuple[ImplementationConstraint, ...] = (),
    ) -> tuple[ImplementationDecision, dict]:
        from .backlog_runner import in_flight_by_product, is_in_flight

        owner = self.coverage.ownership.get(repo)
        raw = self.coverage.policies.get(owner.owner.slug) if owner and owner.owner else None
        policy = effective_policy(raw)
        resolved_effect: Literal["implementation", "proposal"] = (
            "proposal" if effect == "dispatch" and trigger == "automatic"
            and policy["dispatch"] == "propose" else
            "implementation" if effect == "dispatch" else effect)
        product_count = None
        if self.states is not None:
            product_count = in_flight_by_product(self.states).get(
                owner.owner.slug if owner and owner.owner else "", 0)
        daily_count = None if self.accounting is None else len(self.accounting)
        if daily_count is not None and existing_token in (self.accounting or {}):
            daily_count -= 1
        occupied = bool(existing_slot and existing_token and self.states is not None
                        and any(state.get("ownership_token") == existing_token
                                and owner and owner.owner
                                and state.get("product") == owner.owner.slug
                                and is_in_flight(state) for state in self.states.values()))
        decision = decide_implementation(
            ownership=owner, requested_product_slug=requested_product, policy=raw,
            trigger=trigger, effect=resolved_effect, runner_mode=self.runner_mode,
            global_count=(sum(is_in_flight(s) for s in self.states.values())
                          if self.states is not None else None),
            global_cap=self.config.get("max_concurrent"), product_count=product_count,
            daily_count=daily_count, daily_cap=self.config.get("max_spawns_per_day"),
            observed_at=self.coverage.observed_at, existing_slot=occupied,
            prerequisites=prerequisites,
            remaining_checks=("target_readiness", "dependencies", "attempts", "human_holds",
                              "engine_policy", "security", "target_ownership"),
        )
        return decision, policy


async def gather_implementation_snapshot(
    repos: Collection[str] | None = None, *, requested_product: str | None = None,
    context: AutomationContext | None = None,
    verified_repos: Mapping[str, str | None] | None = None,
) -> ImplementationSnapshot:
    from .backlog_runner import BacklogRunner, fold_global_mode
    from .config_store import read_automation_config

    coverage = await gather_product_coverage(
        repos, requested_product=requested_product, verified_repos=verified_repos)
    mode, config = await read_automation_config(
        BacklogRunner, modes=("off", "on", "propose", "auto"), default_mode="on",
        log_key="backlog_runner")
    try:
        states = await (context or AutomationContext(BacklogRunner.id)).list_target_states()
    except Exception:
        states = None
    try:
        accounting = await read_dispatch_accounting()
    except Exception:
        accounting = None
    return ImplementationSnapshot(coverage, fold_global_mode(mode), config, states, accounting)


async def evaluate_implementation(
    repo: str, *, requested_product: str | None = None,
    trigger: Literal["automatic", "manual"] = "automatic",
    effect: Literal["implementation", "proposal", "dispatch"] = "implementation",
    existing_slot: bool = False, existing_token: str | None = None,
    context: AutomationContext | None = None,
    verified_repos: Mapping[str, str | None] | None = None,
    target_refs: Collection[str] | None = None,
) -> tuple[ImplementationDecision, dict]:
    repos = {repo}
    invalid = False
    for ref in target_refs or ():
        target_repo, separator, number = ref.rpartition("#")
        if not separator or not number.isdigit() or int(number) <= 0 or not valid_repo_name(target_repo):
            invalid = True
        else:
            repos.add(target_repo)
    snapshot = await gather_implementation_snapshot(
        repos, requested_product=requested_product, context=context, verified_repos=verified_repos)
    prerequisites: list[ImplementationConstraint] = []
    primary = snapshot.coverage.ownership.get(repo)
    if invalid:
        prerequisites.append(ImplementationConstraint(
            "targets_invalid", "refused", "Every batch issue must identify a valid repository and issue number."))
    for candidate in snapshot.coverage.ownership.values():
        if candidate.state != "unique" or not candidate.canonical_repo:
            prerequisites.append(ImplementationConstraint(
                "targets_unresolved", "unknown", "Every batch target must have verified unique product ownership."))
            break
        if (primary is not None and primary.canonical_repo
                and candidate.canonical_repo.casefold() != primary.canonical_repo.casefold()):
            prerequisites.append(ImplementationConstraint(
                "targets_mismatch", "refused", "A single pull request batch cannot span repositories."))
            break
    return snapshot.decide(repo, requested_product=requested_product, trigger=trigger,
                           effect=effect, existing_slot=existing_slot, existing_token=existing_token,
                           prerequisites=tuple(prerequisites))
