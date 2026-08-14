# Phase 5A Progress — Completion Truth and the Hardened Surface

**Started/completed locally:** 2026-08-14 (Day 5; Phase 5 is scheduled Days 15–17)
**Authority:** `docs/phase5_plan.md` Chunk 5A; build plan §4.5, §11.2, §13.1/13.3,
§13.5, §16.3–16.4; checklist Part II D4 (all rows) plus the open D1
worker-kill/takeover row.
**Status:** **LOCAL EXIT CLOSED 2026-08-14.** All seven 5A work items are
implemented and locally proven: **220 tests green** (up from the 151-test
Phase 4 baseline), Ruff clean, targeted mypy clean across all 13 changed
source files, and the golden dry-run reaches `completed` through the real
workflow. Live D4 probes and the golden campaign are 5B scope.

## 1. Manual completion (§4.5 terminal invariant)

- `CompleteCommitment` (`application/commands/complete_commitment.py`) fills
  the stub: one transaction writes the immutable completion evidence record
  (client idempotency key, note, verified/confirmed minutes at completion),
  the terminal lifecycle transition with `completed_at` via the domain
  `complete()` invariant, a `COMPLETION_RECORDED` activity, and the
  `COMPLETION_CONFIRMED` continuation observation. Dispatch after commit;
  the crash gap stays repairable by `dispatch_pending`.
- Idempotency: replay of the same act converges `NO_OP`
  (`completion_already_recorded`); a second completion act on a completed
  commitment mutates nothing (`commitment_already_completed`); reusing a key
  for different facts is rejected; revision conflicts are surfaced without
  mutation.
- `POST /api/v1/commitments/{id}/complete` (`api/routers/completion.py`) is
  guarded by the controlled session + session-bound CSRF resolving before
  body validation, wired through the container and `main.py`.
- **Continuation** (`_handle_completion_confirmed` in `phase1_workflow.py`):
  pending check-in requests close under the terminal state
  (`awaiting_check_in`/`active` blocks → `missed`, verified minutes
  untouched — golden audit step 17); leftover future `planned` blocks are
  canceled and release their Calendar time through guarded outbox CANCEL
  intents carrying the published snapshot etag as `If-Match` (no snapshot ⇒
  skip and let the 4C drift detector surface it — never a blind delete);
  then one portfolio replan removes the completed commitment from the
  demand set. Completing the live overdue `daf9a729…` will therefore also
  clear the standing portfolio infeasibility (5B precondition).
- Terminal safety: replayed continuations converge byte-identically; later
  safety passes and replans keep the commitment closed; a replayed
  completion observation after an explicit reopen re-closes nothing.

Evidence: `backend/tests/integration/test_phase5a_completion.py` (11 tests,
including the golden dry-run: seed → effort 180 → plan approval → three real
executor-created events → verified 60-minute check-in → elapse → explicit
completion → terminal with 60 honest verified minutes against the
180-minute estimate).

## 2. AuthRouter port — the spike login is gone from the live path

- `AuthRouter` (`api/routers/auth.py`) fills the stub as the production
  session issuer over the ports: `oauth_transactions` repository (state
  hash = document ID, CAS `consume_pending`), PKCE S256, 10-minute
  transaction TTL, opaque 12-hour sessions stored as SHA-256 with a
  session-bound CSRF secret. Routes: `GET /auth/login` (allowlisted
  `return_to` targets only), `GET /auth/callback` (single-use state → code
  exchange → scope validation → id-token verification with nonce and the
  controlled-account allowlist), `POST /auth/logout` (session + CSRF;
  revokes the current session), `GET /api/v1/me` (authenticated read
  returning the CSRF token).
- `GoogleOAuthClient` (`infrastructure/google/oauth_client.py`) fills the
  adapter stub: authorization-URL construction, code exchange, revocation,
  granted-scope validation (login requests only `openid email`).
- `main.py` no longer mounts any spike module: the section 9 login/demo
  router and the section 7 Gemini/ADK proof router are both gone from the
  live path. Spike code remains under `spike/` for the Phase 0 evidence
  record and the local watch-management scripts only.

**D4 session negative matrix** (`backend/tests/contract/test_auth_contracts.py`,
16 tests): non-allowlisted redirect targets rejected before any write;
missing/mismatched/expired/replayed state each rejected with zero side
effects; mismatched nonce ⇒ no session; callback replay cannot create a
second session; failed exchange and non-allowlisted account ⇒ no session;
cookie flags (`Secure`, `HttpOnly`, `SameSite=Lax`, opaque, no OAuth
material); logout revokes (and itself requires CSRF); expiry, revocation,
and unknown-session rejection enforced.

## 3. Full CSRF suite (D4)

`TestFullCsrfSuite` in `test_route_contracts.py`: one parametrized contract
over **every** controlled mutation route — approvals resolve, controls
change, work-block check-in, plan undo, and the new completion route —
proving missing session (401), missing CSRF (403), and invalid CSRF (403)
each leave the durable store byte-identical with zero task dispatches, and
that auth rejections run before body validation (no schema leakage).
`/auth/logout` is covered by the same contract in the auth suite.

## 4. Full demo mutation matrix (D4)

- `DemoRouter` + `StaticDemoReadModel` replace the spike demo route: the
  read model's only state is the committed `demo_data/` path — no
  Firestore, credential, or Google API access path exists (structural
  separation asserted in test). `commitments.json` and `activity.json`
  (0-byte placeholders since Phase 0) are now seeded from
  `golden_scenario_rev_1`.
- `TestFullDemoMutationMatrix`: enumerates every mounted production
  mutation method/path **from the live route table** (self-maintaining as
  routes are added), attempts each under `/demo`, and proves 403 with zero
  Firestore writes, task dispatches, or Calendar calls — including with a
  fully authenticated controlled session. Demo reads serve only seeded
  data, expose no CSRF/session material and no live mutation controls.

## 5. Webhook rate-limit negatives (D4)

`backend/tests/contract/test_webhook_rate_limit.py`: 20 valid signals pass,
the 21st+ return 429 with zero sync requests and zero dispatches; the
Firestore-backed window survives a process restart (the property that
distinguishes it from the Phase 0 per-instance limiter); the window
recovers after expiry; invalid-token probes are rejected before the limiter
and cannot exhaust the valid-signal budget.

## 6. Audited controlled-account cleanup (D4)

- `CleanupControlledAccount`
  (`application/commands/cleanup_controlled_account.py`): preview →
  confirmation phrase → execute, aborting on phrase mismatch or any durable
  drift since preview. Calendar deletion targets only recorded app-owned
  work-block events, sends the published snapshot etag as `If-Match`
  (unsynchronized targets are skipped, never blindly deleted), and relies
  on the writer's independent §9.3 ownership guard. The purge removes the
  controlled user's domain documents (commitments, work blocks, evidence —
  including commitment-linked seeded evidence — approvals, observations,
  outbox, planner runs, dismissals) while retaining activity and
  reconciliation-run audit history, sync cursors/generations, channels,
  sessions, controls, and snapshots. One
  `CONTROLLED_CLEANUP_COMPLETED` activity records the cleanup in the audit
  timeline.
- Script entry: `scripts/reset_controlled_account.py preview` / `run
  --confirm "<phrase>"` over the raw-client
  `FirestoreCleanupDocumentStore`; 5B uses it as the between-runs reset. It
  disposes of the two stale Phase 2 identity approvals and the pre-fix
  `19b1acfb…` action approval generically (all user approvals purge).

Evidence: `backend/tests/integration/test_phase5a_cleanup.py` (6 tests:
owned-only targeting, unrelated events byte-identical, audit recording,
wrong-phrase and drift aborts, stale-approval disposal, no-snapshot skip).

## 7. Fault-injection matrix (§16.4, the open D1 row)

`tests/fault_injection/` (the formerly empty scaffold, now a package wired
into `testpaths`; the harness reuses the backend in-memory twin under a
distinct module name):

1. **Worker-kill + fenced-lease takeover** (open D1 row closed locally):
   redelivery inside the lease window cannot take over; after expiry one
   retry claims with a bumped fencing token; the late original worker's
   forged outcome commit with the stale fence cannot land (store
   byte-identical); the takeover completes with exactly one commitment;
   post-takeover replay converges `NO_OP`.
2. **Executor death after the Calendar response** (§9.4
   create-before-record): external insert landed, record didn't; recovery
   through the stable event ID adopts (never re-inserts) — one event, one
   terminal success, full replay byte-identical.
3. **Executor death before the Calendar response**: `action_in_flight` with
   no external event; recovery executes the insert exactly once.
4. **Transient backend failure**: retryable outcome recorded, redelivery
   completes every action with exactly one insert per stable event ID.
5. **Projection corruption blocks stale execution**: a corrupted remaining-
   minutes projection makes every queued action `action_stale` with zero
   Calendar I/O, zero events, and no block leaving `planned`.

Credited, not duplicated: mid-generation worker death resuming from the
durable checkpoint (Phase 2 `test_worker_death_resumes_from_durable_checkpoint`
and `test_stale_fencing_token_cannot_checkpoint_or_publish`; exercised live
by the Phase 4A fenced barrier/publication proofs).

## Route inventory re-audit (§13.5, post-spike-removal)

Every mounted route and its trust contract (boot check
`_assert_no_duplicate_routes` still enforces uniqueness):

| Route | Guard |
|---|---|
| `GET /health/live` | public, no business state |
| `GET /auth/login` | public entry; allowlisted `return_to` only; no durable write on rejection |
| `GET /auth/callback` | single-use CAS state transaction + PKCE + nonce + controlled-account allowlist |
| `POST /auth/logout` | controlled session + CSRF |
| `GET /api/v1/me` | controlled session |
| `POST /api/v1/approvals/{id}/resolve` | controlled session + CSRF (before body validation) |
| `POST /api/v1/controls/change` | controlled session + CSRF |
| `POST /api/v1/work-blocks/{id}/check-in` | controlled session + CSRF |
| `POST /api/v1/plans/{id}/undo` | controlled session + CSRF |
| `POST /api/v1/commitments/{id}/complete` | controlled session + CSRF (new) |
| `GET /api/v1/commitments`, `GET /api/v1/commitments/{id}` | controlled session |
| `GET /api/v1/dashboard/{today,activity,system-status}` | controlled session |
| `POST /internal/tasks/{reconcile-observation,execute-calendar-action,source-sync}` | tasks-SA OIDC (audience + identity) |
| `POST /internal/scheduler/maintenance/{kind}` | scheduler-SA OIDC |
| `POST /internal/pubsub/gmail` | pubsub-SA OIDC |
| `POST {calendar_webhook_path}` | channel token (constant-time hash) + channel/resource mapping + durable per-channel rate limit |
| `GET /demo/{today,commitments,activity}` | public, static seeded data only |
| `POST/PUT/PATCH/DELETE /demo/{anything}` | always 403 before any logic |

No spike module is mounted; no unguarded route exists.

## Local verification (5A exit)

```text
220 passed (backend/tests + tests/fault_injection; baseline was 151)
ruff: All checks passed (backend/src, backend/tests, tests, scripts)
targeted mypy: Success, no issues found in 13 source files
golden dry-run: completed through the real workflow with honest minutes
```

## Recorded deviations and notes

1. **Boot-time OAuth secret read.** `container.auth_router()` reads the
   OAuth client secret once at boot (the spike read it per request). A
   Secret Manager failure at deploy now fails the revision rather than the
   request — deliberate; note for the 5B deploy.
2. **Login landing target.** Successful login redirects to `/app` by
   default (served in Phase 6); `/api/v1/me` is an allowlisted explicit
   target so live probes can verify the flow end to end without the UI.
3. **Completion cancels future planned blocks.** Slightly beyond the plan
   letter (which only requires closing check-in requests): a completed
   commitment's leftover `planned` blocks release their Calendar time via
   guarded `If-Match` cancels. The golden path itself has no planned blocks
   at completion; the behavior is regression-tested.
4. **Cancel intents carry an empty projection hash.** A completed
   commitment intentionally carries no current-provenance projection;
   revision, control-epoch, and etag guards still protect these cancels
   (the executor's documented legacy-provenance mode).
5. **Demo seeded data filled.** `demo_data/commitments.json` and
   `activity.json` were 0-byte placeholders since Phase 0; now seeded from
   `golden_scenario_rev_1` (the 18-step audit contract condensed).
6. **Deploy note (owner):** the next deploy removes the spike login/demo
   routes and changes the session issuer. Existing spike-issued sessions
   remain valid (same cookie name, same `web_sessions` shape). Cloud Run
   traffic is still name-pinned — an explicit `update-traffic` is required.

## Still open for 5B (by design)

Live D4 probes against the deployed revision (session negative matrix,
OAuth replay, CSRF probes, wrong OIDC audience/identity, `/demo` matrix,
webhook rate-limit exceedance), the `GoldenPathRunner` fill-in, campaign
preconditions (reconnect + renew watches, complete or clean `daf9a729…`,
restore `--to-latest` traffic), and the ten consecutive golden runs.
