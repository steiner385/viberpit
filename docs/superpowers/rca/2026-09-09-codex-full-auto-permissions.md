# Codex full-auto permission prompts (#1481)

## Five whys

1. Routine network/Git/test commands prompt because full-auto mapped to workspace-write/on-request.
2. The native escalation handler routes to request_external, which deliberately never auto-approves.
3. PR #1452 added on-request because workspace-write/never blocked Git metadata writes, sockets and outbound access instead of completing work.
4. That fix optimized for recoverable sandbox failures, not the full-auto product promise of unattended execution.
5. Tests asserted the intermediate mapping instead of a real edit/test/commit/push workflow with zero routine approvals.

The operator explicitly approved full-auto mapping to danger-full-access/never on 2026-09-09. This removes the native filesystem/network sandbox under that selected mode. It does not authorize new business actions, enable disabled automations, override explicit governance denials, or widen the assistant/plan profiles.

## Fix and recurring-pattern audit

**Symptom:** the product reports autonomy while the native engine waits for approval or cannot complete work.

**Anti-pattern:** treating an approval setting as execution authority, and asserting requested settings without checking the native acknowledgement.

**Fix shape:** one canonical engine profile supplies both sandbox and approval policy; check native start/resume acknowledgements, block execution on mismatch, and acknowledge effective mode to the UI. Verify workflow outcomes with a real binary.

**Where to look:** engine profile mapping, external approval handlers, session recovery/plan toggles, automation launch eligibility, configuration descriptions, UI optimistic updates and role sandbox overrides.

Audited siblings: removed the obsolete unattended-network opt-in escalation; retained automation enablement gates and stricter modes; restored policy on runner recovery; retained queued prompts after failed transitions; removed the implementer role's read-only override so it inherits the session authority (reviewer remains read-only).

## Native lifecycle findings

The pinned 0.153.2 binary rejects resume before the first persisted turn. It can also return the original settings when resume requests a different policy for an already-loaded thread. Mocked successful resume responses hid both cases.

Policy transitions therefore require checking the returned settings. A known-empty thread can be replaced using the same start declaration; a persisted thread needs a supported history-preserving fork when loaded resume ignores overrides. Native probes also show that a fork drops goal metadata. The driver checks for native goals and background terminals before forking and must persist the new engine handle before accepting further work. Restarting the shared account process would disrupt other sessions and is not a safe mode-switch mechanism.

## Adoption

New full-auto threads receive the corrected profile. Existing threads receive it when resumed/rebuilt using this code; live mode changes apply at a turn boundary. Running old daemon code does not change merely because this PR exists. Deploy through the normal controller and let active turns drain; do not restart a busy daemon to force adoption.

## Verification

Evidence is recorded with the final PR. The opt-in test `server/tests/test_codex_full_auto_live.py` uses the pinned native binary, an isolated runtime home, and the production credential-authority client. It declines/counts any native approval requests, checks real workflow artifacts, and never copies auth.json. Its remote is a disposable local bare Git repository, not a GitHub PR service; actual GitHub push/PR evidence is the fixing PR itself.

Recorded final native verification: all five opt-in checks passed on codex-cli 0.153.2 (53.36 s): canonical forbidden-rule evaluation with allow control, native full-auto acknowledgement, two real model workflows across resume with zero routine approvals, history-preserving full-auto/plan/full-auto transitions, and the actual driver's empty-thread plan roundtrip. Credential material stayed in the existing mTLS/in-memory path; the test asserted no runner auth.json.

The forbidden-rule test verifies native policy evaluation. A model refusing to attempt a forbidden command is not proof of runtime enforcement, so no such claim is made. Active native goals or background terminals prevent a history fork rather than losing their state; a refused live change restores the previously verified mode so the user can finish or clear that state.

Independent whole-branch review passed after correcting startup reconciliation, deferred-mode ordering, repeated plan-toggle intent, guarded-transition recovery, and durable handle-save retry.

Final gates: Ruff and full mypy passed; core 681 passed; frontend typecheck/lint/build and 4,171 tests passed. Full server run had 8,616 passes and 33 skips; two unrelated API assertions expected 404 but the local frontend build enabled a SPA fallback returning 405. With the generated build moved out to match backend CI, both affected files passed all 45 tests without source changes.
