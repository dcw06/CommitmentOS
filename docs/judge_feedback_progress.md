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

---

# Round 2 (2026-08-18): repair stall, fallback semantics, date clarity

Status: source-complete; local verification green; owner deploy pending.
A second external review of `commitmentos-00055-ks8` prompted these
diagnoses and fixes. No planner, policy, executor, or replay behavior
changed.

## Diagnosed with real measurements first

- **The guided conflict repair blocked on a live Gemini call.** The
  reviewer bounded the stall at 4.7–19.7 s; timed API runs against the
  serving revision measured the conflict card at 4.82 s versus ~0.9 s for
  every other action. Cause (read from source, then measured): the workflow
  awaits `explain_decision` inside the repair path, and the sandbox
  interpreter tried the live model with a 20 s transport timeout.
  **Fix:** sandbox `explain_decision` is now deterministic — it returns the
  workflow's plan-diff fallback immediately and never calls the model
  (`DETERMINISTIC_EXPLANATION_MODEL_ID`). Production keeps live
  explanations. A pulsing busy message covers residual latency honestly; no
  phase streaming was built.
- **The deadline card's `recorded-fallback` label was pinned by cache.**
  Live diagnosis (three fresh-cache story runs against real Gemini): the
  model intermittently returns a second proposal — a restatement of the
  existing commitment beside the authored revision — and the exactly-one
  contract rejected the whole output (`proposal_count`), caching the
  failure for the process lifetime. **Fixes:** (1) proposal narrowing —
  the conforming proposal is selected when extras appear; a narrowed accept
  still passes every per-proposal semantic check plus the downstream
  validator, and falls back only when nothing conforms; (2) non-live cache
  entries now expire after `FALLBACK_CACHE_TTL_SECONDS` (10 min) while live
  entries persist, so a failure is retried, not authoritative forever;
  (3) rejection codes ride in the log message text so structured logging
  keeps them. Validation itself was not weakened.
- **"Current deadline evidence" was a mislabel, not a misbinding.** The
  interpretation contract defines the first evidence span as the quote
  anchoring the commitment itself, and the workflow binds
  `deadline.evidence_id` to that span. The UI now labels it "Primary
  evidence"; the deadline's true source span is `source_expression`,
  already quoted beside the date. No production binding changed.
- **Simulated-clock latency removed from sandbox evidence.**
  `repair_latency_ms` / `decision_latency_ms` compute to ~0 under the
  FakeClock and read as production measurements; they are no longer
  projected. The measured latencies live in the frozen evidence pack.

## Presentation

- Seeded demo data carries explicit dates on the canonical scenario week
  (Mon Sep 14 – Mon Sep 21): every block visibly precedes its commitment's
  deadline, ending the "due Monday, blocks Tuesday/Friday" apparent
  violation. Authored scenario semantics unchanged.
- Completion is consequence-labeled ("Mark complete — cancels N future
  blocks") with a confirm step; completed dashboard commitments show a
  prominent explicit-completion line so verified-less-than-estimated reads
  as honesty.
- Effort copy is conditional: with no proposed minutes it says "enter your
  estimate" instead of claiming the agent proposed one.
- Event-specific evidence disclosure labels (Planner / Calendar action /
  Policy / Check-in …) replace thirty identical summaries; each surface has
  an `<h1>`; `Priority 0` renders as "Normal"; the demo banner opens with
  the fixture-derived story sentence.

## Local verification

```text
.venv/bin/pytest                 325 passed (+4: narrowing, fallback-only,
                                 deterministic explanation, cache TTL)
.venv/bin/ruff check .           clean
cd frontend && npm run build     clean
cd frontend && npm test          3 passed (effort-copy branches added)
Chromium audit (sandbox + demo): 22/22 — narration, event-specific
  disclosures, no simulated latency, primary-evidence label, consequence-
  labeled completion + confirm, dated demo deadlines/blocks, banner story,
  h1s, Normal priority, 390px, zero console errors
```

## Required owner action

Deploy via the release gate, then rerun the security probes and re-time the
conflict card on the new revision (expect ~1 s, matching the other
actions). The deadline card should now label `live` on most fresh sessions;
a `recorded-fallback` sighting self-heals within the TTL.

---

# Round 3 (2026-08-20): informed approval, honest narration, guided closure

Status: source-complete; local verification green; owner deploy pending.
A third review scored the submission 4.2/5 (architecture 4.6, demo
readiness 3.8) and green-lit this final code cut. Declined by agreement:
scenario unification (the two stories are deliberate — bridge sentence
added to the README and the guided conclusion instead) and a sandbox
pause/hold/resume card (shown on the real dashboard in the video instead).

- **Ghost-block plan preview.** The `initial_plan_approval` payload always
  carried `proposed_blocks`; the sandbox now exposes them
  (`proposedBlocks`) and renders dashed "proposed · not reserved yet"
  calendar entries plus a slot list on the approval card ("Approving
  reserves exactly these blocks — nothing is written yet"). Pinned by test:
  before approval there are zero work blocks, zero outbox actions, and zero
  calendar events; the approved blocks equal the previewed ones.
- **Deadline-change narration fixed.** Identity resolution succeeds on that
  card — the old copy ("did not clear the deterministic identity or policy
  boundary") undersold it. A held deadline change now says: "The proposal
  matched the existing commitment, but the deadline change was held for
  your approval." Identity confirmations keep the boundary copy.
- **Guided completion is now the tour's explicit step 9.** The old final
  step fired on `remaining === 0`, which the 60-of-180 story never reaches,
  so "That is the whole loop" appeared while "Mark complete" still sat
  outside the sequence. The tour now routes to explicit closure whenever
  the deck is exhausted and a commitment is open; "whole loop" (step 10)
  renders only after terminal completion.
- **Genuine model metadata in evidence.** `interpretation_created` audit
  payloads already carry sanitized `model_id`, `prompt_version`, and
  client-measured wall-clock `latency_ms`; `interpretation_rejected`
  carries `error_codes`. These are now projected — with `modelLatencyMs`
  shown only when a call actually happened (> 0), so a recorded result is
  identified by `recorded-interpretation` and never displays a latency.
  Nothing is synthesized.

Local verification: 328 backend tests (+3), ruff clean, frontend builds,
vitest 3 green, Chromium audit 18/18 (ghosts before/absent after approval,
no pre-approval mutation, narration copy, completion-step tour gating,
model metadata honesty, dated demo, 390 px, zero console errors).

Owner action: deploy via the release gate, rerun the probe suite, and
verify on the live revision that the guided story still performs exactly
one minimal conflict repair (the probes' authored story plus a timed
conflict card cover this).
