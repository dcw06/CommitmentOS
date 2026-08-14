# Phase 1 Progress — Contracts and Seeded Vertical Slice

**Started:** 2026-08-12 (Day 3 of the plan schedule)
**Authority:** `Plan_Final/CommitmentOS_Build_Plan_Final.md` §17 Phase 1;
`Plan_Final/CommitmentOS_P0_Code_Architecture.md` §18 steps 2–3;
checklist Part II D1.
**Status:** **GATE CLOSED 2026-08-12** (same day). Deployed seeded slice ran
18/18 checkpoints against revision `commitmentos-00013-tw2` with real
Calendar, including the live pause-proof. See "Gate closure record" below.

## Gate closure record (2026-08-12)

Plan §17 Phase 1 gate — "A seeded end-to-end run reaches real Calendar
through a replay-safe outbox and remains recoverable across Cloud Run
recycling" — **passed live**:

- **Run:** `scripts/run_seeded_slice.py run --pause-proof --timeout 300`,
  run tag `20260812t152758`, revision `commitmentos-00013-tw2` (scale-to-zero;
  the multi-minute run spans instance recycling by construction, and the
  local restart-survival test covers the explicit recycle case).
- **Checkpoints (18/18):** observation committed before dispatch → named
  reconciliation task → effort approval pending/resolved → plan approval
  pending → **automatic actions paused (epoch 2) → plan approved → all three
  outbox intents `held_by_control`, zero Calendar mutations → resumed
  (epoch 3)** → revalidation reissued and executed → three real Calendar
  events, each with the derived stable event ID and CommitmentOS ownership
  properties → three `action_result` observations reconciled to `processed`.
- **Independent verification loop fired unprompted:** the executor's inserts
  triggered the live Calendar watch → webhook (204) → coalesced sync request
  → source-sync fetch (200, restored spike handler) during the gate window.
- **Log redaction verified live (B3 closed):** Cloud Logging scan over the
  gate window — zero bearer tokens, auth headers, cookies, OAuth material,
  or account addresses. Checklist §2 log row checked.
- **Indexes:** all four composite indexes were already READY in Firestore
  (matching `infra/firestore/indexes.json`).
- **Route contracts probed live on the new revision:** health 200; task,
  scheduler, and approval routes 401 before body validation (the 422
  auth-ordering bug is confirmed fixed live); demo read 200 / demo mutation
  403; `/internal/tasks/source-sync` served by the spike handler again.
- **Cleanup:** three conditional `If-Match` cancels applied (live conditional
  mutation exercised once more) and 10 slice documents purged; activity and
  reconciliation-run audit history retained.
- **Checklist:** D1 rows checked with evidence (two rows remain open by
  design for Phase 2/4 machinery, annotated); blockers B1 and B3 closed;
  decision-log entry added.

**Still open after gate closure (not gate-blocking, tracked):**
architecture-diagram validation against deployed trust boundaries (§17
Phase 1 last bullet — owner review of `CommitmentOS_P0_Architecture.svg`),
the three Phase 0 owner sign-offs, B2 screenshots, and the JS-origins glance.

## What is implemented (all under `backend/src/commitmentos/`)

| Layer | Contents |
|---|---|
| Contracts | Canonical length-delimited encoding + deterministic IDs (`domain/shared/types.py`), observation factory and envelope state machine (`contracts/observations.py`), named-task factory with dispatch-generation-aware names (`contracts/tasks.py`), version registry |
| Domain | Commitment lifecycle with terminal-completion invariant, work-block progress transitions (elapsed time never reduces effort), two-machine action outbox (`dispatch_status` × `execution_status` per architecture §12), monotonic-epoch system controls, deterministic activity events |
| Infrastructure | Firestore unit of work with read-through/write-behind staging (reads precede writes inside every transaction), serializers, repositories for all Phase 1 collections; Cloud Tasks named-task dispatcher; Google Calendar writer (`If-Match` on patch/cancel, insert-or-adopt on the stable ID, independent §9.3 ownership refusal) and reader; controlled-credentials provider with rotation-safe cache; Google OIDC verifier |
| Application | `ReconcileObservation` (claim-or-hold envelope, fenced processing lease, dispatch-generation checks), `ResolveApproval` (CAS decision + authoritative fact + continuation observation in one transaction; stale revision ⇒ superseded), `ChangeSystemControl` (epoch increment + control observation; monitoring resume redispatches held observations), `ExecuteCalendarAction` (two-transaction claim → final pre-I/O linearization; 412 ⇒ `stale_precondition` + sync request, no `action_result`, no blind retry), `RunMaintenance` (pending-dispatch scanner: the write-before-enqueue repair path), observation/outbox dispatchers |
| Workflow | `SeededReconciliationWorkflow`: seeded Gmail observation → commitment + effort approval → initial-plan approval → work blocks + outbox intents committed atomically → dispatch; control-resume revalidation supersedes held intent and reissues it with the new epoch |
| API | `/api/v1/approvals/{id}/resolve` and `/api/v1/controls/change` behind server-side session + session-bound CSRF; `/internal/tasks/*` and `/internal/scheduler/*` behind per-group Google OIDC; structured JSON logging with the sensitive-data redactor (closes blocker B3); trace-ID, security-header, and error-mapping middleware; composition root in `bootstrap/container.py`; app boots with every route guarded (verified live via TestClient against the real `create_app`) |

## Test evidence (42 passing)

`backend/tests/` — fakes duck-type the Firestore transaction context, so the
production repository code runs unmodified over the in-memory store; external
adapters are scriptable fakes; no domain logic is monkeypatched.

Phase 1 / D1 rows proven locally:

- **Seeded slice end to end**: observation → commitment → two approvals →
  3 work blocks + 3 outbox intents (one transaction) → executor → 3 Calendar
  events with stable derived IDs + ownership properties → 3 `action_result`
  observations → follow-up reconciliation (`test_phase1_slice.py`).
- **Observation transport**: observations commit before dispatch and travel
  only through the reconciliation queue; task names embed observation ID,
  workflow version, and dispatch generation; payloads carry references only.
- **Replay safety**: replayed seeded observation ⇒ one commitment; redelivered
  executor tasks ⇒ zero duplicate events; double approval resolution ⇒ one
  decision and one continuation observation.
- **Automatic-action pause/resume**: queued action tasks delivered while
  paused hold durably with zero Calendar mutations; resume revalidates,
  supersedes held intent, reissues with the new control epoch, then executes.
- **Monitoring pause/resume**: held observation, no reconciliation task, no
  Gemini-side work; resume bumps the dispatch generation; a stale pre-pause
  task name is acknowledged without work.
- **Restart survival**: approvals resolved by a rebuilt process over the same
  durable state; flow continues (Cloud Run recycle simulation).
- **Crash-gap repair (blocker B1)**: record committed + task creation fails ⇒
  maintenance `dispatch_pending` recreates the same named task
  (`test_crash_gap_repaired_by_maintenance`).
- **412 contract**: `stale_precondition`, one durable Calendar sync request,
  zero overwrite, zero `action_result`; retryable failures record attempt
  state only.
- **Route contracts**: missing session 401, missing/wrong CSRF 403, expired or
  revoked session 401, wrong OIDC identity 403 — each with byte-identical
  durable state afterward (`test_route_contracts.py`).

## Post-deploy findings and fixes (2026-08-12, after first live deploy)

Owner's post-deploy probe surfaced two issues; both are fixed and regression-tested:

1. **Auth ordering on new routes (fixed).** Unauthenticated POSTs to
   `/internal/tasks/*` returned 422 — FastAPI validated the body before the
   in-handler OIDC call, giving unauthenticated callers free schema probing
   (§16.3 violation; the spike routes verified the bearer first). OIDC, the
   controlled session, and CSRF are now FastAPI dependencies, which resolve
   before request-body validation. Regression tests pin the order: invalid
   body + no credentials ⇒ 401/403, never 422
   (`test_route_contracts.py::TestTaskRouteContracts::test_auth_runs_before_body_validation`,
   `TestControlledRouteOrdering`). **Redeploy required.**
2. **Gmail chain dormancy (half accident, fixed).** Mounting a Phase 2 stub at
   `/internal/tasks/source-sync` was deliberate; shadowing the spike's
   working fetch handler at the same path was an accidental route collision
   (my router mounted first). The stub route is removed — the spike handler
   serves that path again until Phase 2's staging generations replace it. A
   boot check asserts no duplicate method+path registrations remain.

Additional review fixes from the comprehensive Phase 1 pass:

- Executor now records `queued → delivered` on the outbox dispatch machine at
  claim time (previously skipped; architecture §12).
- Maintenance sync-request repair skips spike-era request shapes (no
  `sync_generation_id`) so it cannot enqueue Phase 2 payloads at the spike
  handler.
- Serializer round-trip tests added (§16.2 row).
- **`infra/firestore/indexes.json` now defines the four composite indexes the
  deployed queries need** (observation dispatch-eligible scan, outbox
  pending/held scans, activity timeline). Without them, scheduler-driven
  `dispatch_pending` and held-record queries fail on live Firestore. Deploy
  with the Firebase CLI (`firebase deploy --only firestore:indexes`) or four
  `gcloud firestore indexes composite create` commands mirroring the file.

## Phase 1 gate runner

`scripts/run_seeded_slice.py` drives the gate against the deployed service:
it commits a run-tagged seeded observation through the real Firestore unit of
work, dispatches real named Cloud Tasks at the deployed handlers, resolves
both approvals, then verifies live Calendar events (stable derived IDs +
ownership properties) and processed `action_result` observations. Every
bounded step lands on a fresh scale-to-zero instance, which is the
recoverable-across-recycling property exercised live.

- `run --pause-proof` executes §17's pause bullet live: pause before the plan
  approval, prove all intent holds with zero Calendar mutations, resume, and
  verify revalidation reissues and executes under the new epoch (integration
  twin: `test_pause_before_plan_approval_holds_new_intent`).
- `run --cleanup` / `cleanup --run-tag <tag>` cancels the created events via
  the conditional `If-Match` path and purges the slice's Firestore records
  (activity and reconciliation runs retained as audit history).

Gate sequence: deploy revision with the fixes → create indexes → 
`run_seeded_slice.py run --pause-proof` → checkpoints all PASS → check D1
rows + record in the checklist.

## Comprehensive review results (2026-08-12)

- 53 tests passing (unit, integration, contract); ruff clean.
- §19 invariant scan: Calendar API calls only in the writer/reader adapters
  and spike modules; every patch/cancel sends `If-Match`; no
  `BackgroundTasks`/process-local queues; no Pub/Sub in the application
  layer; remaining `sha256` uses are content/token hashing, not ad hoc
  document identity.
- Transaction ordering audited: every command performs reads/queries before
  staged writes, so flushed Firestore transactions satisfy the
  reads-before-writes rule; fenced-lease verification joins every workflow
  write transaction.
- Late-worker fencing: a takeover after lease expiry bumps the fencing
  token; a stale worker's outcome commit fails the token check and the
  retry path converges (exception → task retry → NO_OP).
- mypy not yet run over the new code (ruff + tests only) — optional cleanup.

## Recorded deviations and open items

1. **ADK wrapper deferred to Phase 2.** The Phase 1 workflow runs its typed
   nodes through a deterministic in-process route; the deployed ADK graph
   mechanic was proven in Phase 0 §7. The ADK `Workflow` wrapper joins when
   the Gemini interpretation node lands (Phase 2). Recorded in the workflow
   docstring. **Resolved 2026-08-13:** `AdkReconciliationWorkflow` now wraps
   the route in production (`docs/phase2_progress.md`).
2. **Spike login retained.** The proven Phase 0 login flow
   (`spike/section9_auth.py`) remains the session issuer; sessions now carry a
   CSRF secret consumed by the new mutation routes. The full `AuthRouter`
   port with the complete negative matrix is Phase 5 scope (D4), as planned.
3. **Sync repositories stubbed.** `sync_cursors`/`sync_generations`/
   `sync_generation_items` raise until the Phase 2/4 bounded-generation
   gates; `sync_requests` is live (used by 412 handling and maintenance).
4. **Deployed-gate confirmation pending.** First deploy done by the owner;
   a redeploy with the auth-ordering/route fixes plus the composite indexes
   is required, then one passing `run_seeded_slice.py run --pause-proof`
   closes the deployed proof.
5. **Architecture diagram validation** (§17 Phase 1 last bullet) not yet
   done; do together with the deployed run.
6. **Resolved in Phase 3B (2026-08-13):** the Phase 1 seeded planner was
   deliberately naive (60-minute 09:00 blocks); the deterministic portfolio
   planner now replaces it in the initial-plan continuation and the
   `first_plan_requires_approval` contract is unchanged by that swap.
