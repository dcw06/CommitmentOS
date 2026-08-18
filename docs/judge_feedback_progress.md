# Judge-feedback pass: audit evidence projection and presentation clarity

Date: 2026-08-18
Status: LIVE-VERIFIED on revision `commitmentos-00055-ks8` (owner deployed
via the release gate; see "Live verification" below)
Baseline: tag `sandbox-hardened-baseline` — the verified hardened deployment
this pass builds on. Roll back to that tag if anything here misbehaves.

An external judge-style review of the live sandbox confirmed the product
behavior end to end and identified one substantive gap: the activity display
did not substantiate that the production architecture actually executed. This
pass addresses that and the review's presentation findings. No control-loop,
policy, planner, or executor behavior changed.

## Audit evidence projection (the substantive change)

- Sandbox activity events now carry an explicit-allowlist evidence
  projection (`engine._evidence_projection`): correlation id (audit
  `trace_id`), actor, observation id, commitment/plan revision, policy
  reason, planner run id and version (joined from `planner_runs`), stable
  calendar event id, outbox status, moved/preserved block counts, and
  repair/decision latency. Referenced outbox actions are joined from
  `action_outbox` and expose idempotency key, execution status, action type,
  stable event id, expected `If-Match` etag, and observed response etag.
- Fields are copied key-by-key from the allowlist — the raw payload is never
  spread — so evidence excerpts, prompts, model input/output, headers, and
  provider response bodies are excluded by construction. Pinned by
  `TestAuditEvidenceProjection` in `backend/tests/integration/
  test_sandbox_flow.py`: allowlist containment, architecture substantiation
  (etags, idempotency keys, planner version, block counts), and a
  message-body redaction check. The allowlist is duplicated in the test on
  purpose: adding a field must be a deliberate two-place decision.
- The frontend renders this as an expandable "session execution evidence"
  block per activity entry; the full 40-event window is shown (scrollable)
  instead of the last 12.

## Presentation and truthfulness

- Execution-boundary copy: the sandbox banner now reads "real reconciliation
  and policy code running against session-scoped inbox, calendar, and
  persistence adapters" — precise about what is real and what is
  session-scoped.
- Interpretation provenance labels are unchanged (still truthful); each now
  has one visible sentence of explanation, e.g. live-cached: "a real Gemini
  interpretation previously produced and semantically validated for this
  card; the deterministic workflow runs fresh for every action."
- No-op replanning is narrated: accepting a counterparty deadline that leaves
  every block valid shows "Feasibility was recalculated; all existing blocks
  remained valid, so the plan was preserved unchanged." A changed plan gets
  the changed-plan variant. Engine-level, so the API carries it.
- Distinct pending labels: `pendingStage` distinguishes
  `awaiting_effort_confirmation` from `awaiting_plan_approval` on the
  commitment view (backend-derived from the pending approval type).
- Calendar/date display: sandbox blocks show full ranges with date and zone
  ("Tue, Sep 15, 9:00 – 10:00 AM PDT"), deadlines show date+time+zone, and
  the live dashboard's shared formatters gained weekday and timezone
  (`formatRange` in `frontend/src/ui.tsx`). Seeded demo strings are authored
  and unchanged.
- Session vs seeded evidence is labeled: sandbox activity is "session
  execution evidence"; the demo Activity page labels expanded payloads
  "Seeded demonstration evidence" and its note says where genuine per-action
  evidence lives.
- `docs/proof_index.md` indexes every measured claim (revision + exact frozen
  evidence file); linked from the README and the sandbox's guided
  conclusion.

Deliberately not done: a guided completion step (the review ranked it
schedule-permitting; "Mark this done" remains available and step 9 points at
it).

## Local verification

```text
.venv/bin/pytest                 321 passed (315 baseline + 6 new)
.venv/bin/ruff check .           clean
cd frontend && npm run build     clean
cd frontend && npm test          1 passed
Chromium UI audit (sandbox + demo, recorded interpretations):
  boundary copy, interpretation explainer, deadline/calendar date+zone,
  no-op replan narration, evidence fields (correlation id, policy reason,
  planner version, idempotency key, stable event id, expected/observed
  etag, repair latency), pending-stage label, seeded-evidence labels,
  390px no horizontal overflow, zero console errors — all green
```

## Live verification (2026-08-18, revision `commitmentos-00055-ks8`)

The owner deployed through `scripts/deploy_commitmentos.py --deploy`
(release gate: session affinity, two-instance cap, latest traffic, live
sandbox-model probe). Verified against the serving revision:

- **Security probes 73/73 green**, all six groups — evidence
  `docs/sandbox_evidence/security_probes_20260818t153546.json`; a second
  consecutive run was also 73/73. The `/demo` surface remains mutation-free
  (its 27-probe matrix is part of the run).
- **Authored story end to end via the API**: live Gemini on the first card
  (`interpretationSource: "live"`), candidate → convergence → the two
  distinct pending stages → plan → deadline held then accepted with the
  preserved-plan narration → automatic conflict repair → elapse → 60
  verified minutes → explicit completion retaining them. Every activity
  event satisfied the evidence allowlist; correlation ids on all events;
  `plan_repaired` carried `movedBlockCount: 1` and the planner version;
  outbox actions exposed idempotency keys, expected `If-Match` etags, and
  observed response etags; no message bodies appeared in the projection.
- The served bundle carries the boundary copy, evidence UI, seeded/session
  labels, and proof-index link (verified by string presence in the deployed
  asset).

Remaining judge-visible spot-checks best done by a person in a browser:
free-play cached-vs-live labeling on repeat sends and the
injection-resistance message (both were exercised in the review that
prompted this pass, on the prior revision).
