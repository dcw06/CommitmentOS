# Phase 2 Progress — Gmail Evidence and Identity

**Started:** 2026-08-13 (Day 4; Phase 2 is scheduled Days 6–8)
**Authority:** `Plan_Final/CommitmentOS_Build_Plan_Final.md` §17 Phase 2, §9.1/9.1.1,
§9.6/9.7, §13.2, §16.1; `Plan_Final/CommitmentOS_P0_Code_Architecture.md` §7, §8.1,
§11.5; checklist Part II D2.
**Status:** **GATE CLOSED 2026-08-13 (live).** All D2 protocol rows and the
§17 identity/evidence behaviors pass locally (84 tests green), and the
deployed gate run passed against revision `commitmentos-00018-qxx`. See
"Gate closure record" below.

## Gate closure record (2026-08-13)

Plan §17 Phase 2 gate — "Real and replayed thread activity produces the
correct commitment records with zero unintended duplicates" — **passed live**:

- **Deployment:** four new composite indexes READY; revisions
  `commitmentos-00014-c62` (v2 env pins) → `00016-fcd` (delimiter
  neutralization + acceptance convergence) → `00018-qxx` (Gmail adapter
  fixes); all route trust contracts re-probed live (health 200; task,
  scheduler, pubsub, and dashboard routes reject unauthenticated callers
  before body work). Three Cloud Scheduler jobs created and enabled;
  `dispatch_pending` proven live with a scheduler-delivered 200.
- **Real thread:** request ("Proposal Revision") + acceptance reply through
  watch → Pub/Sub → bounded generation (7 staged / 7 applied, two vanished
  drafts skipped, candidate cursor promoted 17550→18550) → Gemini
  `extraction_v2` → **one commitment** (`my_commitment`,
  `awaiting_confirmation`, deadline Friday 16:00 PT from "before our Friday
  4 p.m. review", confidence 0.95) + evidence excerpts + a pending
  `effort_confirmation` approval (no invented effort). Unrelated real inbox
  mail (receipts, security alerts) reconciled to `processed` with zero
  commitments; two real payment-request emails routed to
  `identity_confirmation` approvals — the confirmation boundary live.
- **Deadline revision:** the "Thursday at 4 p.m." reply ran a second, fully
  autonomous cycle (no manual dispatch): `update_existing` on the same
  commitment, revision 2, deadline Thursday 16:00 PT, before/after change
  recorded in the audit timeline. Cursor promoted to revision 2 (18627).
- **Replay:** all three thread observations redelivered to
  `/internal/tasks/reconcile-observation` with an impersonated tasks-SA
  OIDC token → `200 no_op` ×3; durable state (commitment revisions,
  evidence count, approvals, observation count) byte-identical before and
  after. Zero duplicates, zero mutations, zero model calls.
- **§16.1 extraction eval** (32 labeled cases, live Gemini): run 1 measured
  100% schema-valid, 90.6% ownership, 71.9% deadline, 100% injection
  containment — and exposed one real gap (a delimiter-escape injection body
  produced an accepted proposal) plus scoring artifacts (resolver-covered
  dismissal cases scored against the model, confirmation routing scored as
  failure, two under-specified labels). The gap became the
  `neutralize_untrusted_delimiters` hardening (§13.2) with a regression
  test; the labels/scoring were corrected with rationale recorded in the
  fixture pack. Run 2: **100% on every metric**, mean $0.000771/message,
  mean latency 5.0 s. Both raw result files preserved in
  `docs/phase2_evidence/`.
- **Live-only findings fixed during the gate** (each now regression-tested):
  Gmail's `historyTypes` filter enum is singular `messageAdded` while the
  response field is plural (discovery-document validation the fakes cannot
  see), and `messages.get` 404s on history records for discarded drafts —
  the worker now skips vanished messages instead of entering a retry storm.
  During the storm, pausing the source-sync queue froze deliveries cleanly
  and the named-task machinery resumed the stuck generation from its
  durable checkpoint after the fix — the recovery design working as
  specified.
- **Log scans clean** over the gate window: no bearer tokens, no controlled
  account address, no message-body content in Cloud Logging.

## Plan §17 Phase 2 scope → implementation

| Requirement | Implementation |
|---|---|
| Serialized per-user Gmail staging generations, bounded page/apply commits, final cursor publication | `SynchronizeSource` (`application/commands/synchronize_source.py`): per-user fenced source lease, one provider page per named task, deterministic `sync_generation_items`, page/apply checkpoints with commutative XOR manifests (chunk-boundary independent), single final publication transaction promoting the candidate cursor exactly once; sync repositories + serializers implemented (`sync_cursors`/`sync_generations`/`sync_generation_items` no longer stubs) |
| Message fetch, normalization, minimal evidence storage | `GoogleGmailReader` (history.list with `messagesAdded`, bounded message fetch, text-body extraction, content-stable payload hash); staged items carry identifiers/labels/subject only — bodies are never staged (§13.3); INBOX/SENT relevance filter, DRAFT/SPAM/TRASH/CHAT excluded |
| Daily watch renewal, catch-up, bounded cursor recovery, loop prevention | `RunMaintenance.renew_watches` (re-arms `users.watch`, never touches the published cursor), `recover_cursors` (6-hour quiet-window catch-up), Gmail 404-on-cursor → `full_resync_required` marking; `scripts/create_scheduler_jobs.sh` creates the three Cloud Scheduler jobs |
| Gemini structured extraction, deterministic validation, ownership classification, identity operations | `extraction_v2` wire schema (multi-proposal + identity ops, strict pydantic, sanitized copy for `response_schema`), prompt `commitment_interpretation_v2.md`, `GeminiInterpreter` adapter (delimited untrusted source, candidate context, sanitized metadata), `ModelOutputValidator` (evidence quotes must be exact substrings; confidence never grants authority), `CommitmentIdentityResolver` (deterministic §9.6 rules: verified targets only, fingerprint convergence for restatements, dismissed-span suppression, ownership-compatibility checks) |
| Fixtures incl. the demo ownership/deadline-revision thread and prompt injection | Golden thread drives `test_phase2_interpretation.py` (create → restatement convergence → M3 deadline revision); dismissal-resurface, multi-commitment, ambiguity, and injection cases covered; `extraction_eval_fixtures_v1.json` adds 32 labeled cases across all twelve §16.1 categories |
| Candidate dashboard and source evidence view | `ListCommitments` / `GetCommitment` queries + `CommitmentsRouter`: `GET /api/v1/commitments` (status/risk filters) and `GET /api/v1/commitments/{id}` (evidence excerpts + references, work blocks, related audit trail) behind the server-side session |
| Phase 1 deferred join: ADK `Workflow` wrapper | `AdkReconciliationWorkflow` (`workflows/reconciliation/graph.py`): production reconciliation now executes through an ADK `Workflow` graph (`load_observation → execute_route → finalize_reconciliation_run`) run by `InMemoryRunner` per named-task delivery; the inner route object is shared with tests, so both paths execute identical logic (proven by `TestAdkGraphExecution`) |
| Phase 1 deferred join: Cloud Scheduler jobs | `scripts/create_scheduler_jobs.sh` (dispatch_pending every 5 min, renew_watches daily, recover_cursors every 6 h) |

## Transport and trust changes

- `ReceiveGmailSignal` + `PubSubRouter` replace the spike Gmail Pub/Sub
  ingress: OIDC dependency before body work, durable coalesced sync request
  (spike-compatible `gmail:{user}` document, `max(historyId)`), then one named
  bootstrap task whose name varies per signal (Cloud Tasks 24 h name retention
  cannot swallow a later notification).
- `/internal/tasks/source-sync` is now served by the real `SynchronizeSource`
  handler. Spike-shaped **Calendar** task bodies (no `sync_request_id`) bridge
  to the Phase 0 fetch handler so the live Calendar watch→webhook→fetch loop
  keeps running until Phase 4 replaces it; the bridge is explicitly marked.
- The spike section-4 router is no longer mounted (both its routes are
  superseded). A boot check in `main.py` now asserts no duplicate method+path
  registrations (hardening the Phase 1 shadowing incident into a guarantee).
- Observations materialized during apply start in a new `staged`
  reconciliation status, invisible to every dispatch scan until their
  generation publishes; release flips them to `pending` in bounded batches
  (architecture §11.5 step 7). Replays across generations converge on the
  content-stable observation identity.

## Test evidence (81 passing; 28 new)

`test_phase2_sync.py` — checklist D2, all rows:

- Two-page deterministic fixture staged, applied, and published exactly once;
  aggregate staged/applied manifests match; draft filtered.
- Published cursor unchanged after page 1; candidate cursor stored on the
  generation only at the final page; spike-era cursor document adopted in
  place at revision 0.
- Page retry reuses deterministic item IDs and restages nothing.
- Stale fencing token can neither checkpoint nor publish (fence checked
  before status in the publication transaction).
- Worker death after page 1 resumes from the durable checkpoint after lease
  takeover with `adopt_fence`; no duplicate items.
- A coalesced signal cannot start a second generation while one is active;
  redelivered bootstrap tasks after publication acknowledge without creating
  empty generations (`signal_already_serviced`).
- Replayed Pub/Sub deliveries converge on one named task; auth failure
  persists `reauth_required`; invalid cursor marks `full_resync_required`;
  the write-before-enqueue crash gap is repaired by `dispatch_pending`;
  watch renewal never touches the published cursor; quiet-window catch-up
  creates and dispatches a request exactly when stale.

`test_phase2_interpretation.py` — §17 gate behaviors over the real workflow
path (scripted interpreter, production validation/resolution/persistence):

- Golden acceptance → commitment (`awaiting_confirmation`) + evidence with
  derived span key + effort approval + §9.6 audit record (candidate set,
  proposed op, final op, reason, sanitized model metadata).
- Replayed observation ⇒ one commitment, zero further model calls.
- Restatement proposed as `create` converges to `update_existing` via
  fingerprint matching — zero duplicates.
- M3 deadline revision updates the existing commitment (revision 2,
  Thursday 16:00) with a recorded before/after change.
- A dismissed source span re-proposed unchanged resolves to `ignore`
  (`dismissed_span_resurfaced`); nothing is created.
- Fabricated evidence (injection shape) is rejected with zero commitments,
  zero approvals, zero outbox intents, and a visible
  `interpretation_rejected` activity; schema-invalid model output likewise.
- Ambiguous ownership routes to an `identity_confirmation` approval.
- Two commitments in one message receive distinct span keys and records.
- The ADK `Workflow` wrapper produces identical durable results.

`test_route_contracts.py` additions: dashboard reads require the session
(401), the evidence view serves excerpts (never bodies), unknown commitment
404, unknown filter 400.

## Deployment requirements (owner, before the live gate run)

1. **Create four new composite indexes** (added to
   `infra/firestore/indexes.json`): commitments (user_id, updated_at DESC),
   commitments (user_id, lifecycle_status, updated_at DESC),
   sync_generations (user_id, source, status),
   sync_generation_items (sync_generation_id, status, sync_generation_item_id).
2. **Redeploy** — new routes, the spike Gmail routes are gone, and `.env`
   version pins changed (`prompt_version=commitment_interpretation_v2`,
   `extraction_schema_version=extraction_v2`); the cloud env vars must match.
3. **Create the Cloud Scheduler jobs**: `bash scripts/create_scheduler_jobs.sh`.
4. **Run the §16.1 extraction eval** (~32 real Gemini calls, ≈ $0.03–0.10):
   `python scripts/run_extraction_eval.py` — writes measured results to
   `docs/phase2_evidence/`; target ≥ 90% ownership and deadline accuracy,
   raw result preserved either way.
5. **Live gate run**: send a real thread (request → acceptance → deadline
   change) to the controlled account; verify watch → Pub/Sub → bounded
   generation → published cursor → reconciliation → commitment + revision;
   replay by re-delivering the named tasks and confirm zero duplicates.

**Gate (plan §17):** "Real and replayed thread activity produces the correct
commitment records with zero unintended duplicates" — proven locally
end-to-end; closes after the deployed run above.

## Recorded deviations and open items

1. **ADK node granularity.** The reconciliation graph currently wraps the
   deterministic route as one `execute_route` stage between typed load and
   finalize nodes; the planning stages split into their own graph nodes when
   the Phase 3 portfolio planner lands (docstring records this).
2. **Identity-confirmation continuation is minimal.** Approving an
   `identity_confirmation` approval records the decision and continuation
   observation; automatic application of the confirmed operation joins the
   Phase 3 planning continuations. Dismissal via the dashboard is likewise
   Phase 3 UI scope (the domain rule and dismissal suppression are live).
3. **Calendar source-sync bridge.** Spike-shaped Calendar bodies still run
   the Phase 0 bounded fetch through the real route (removed by Phase 4's
   Calendar generations).
4. **Deadline normalization is model-side.** §9.7's rule (message-time
   reference, thread timezone, working-day end for date-only deadlines) is
   enforced by prompt contract + confidence floor + confirmation routing;
   a deterministic re-derivation pass is Phase 5 hardening if eval results
   demand it.
5. **Frontend dashboard rendering** remains Vite scaffolding; Phase 2
   delivers the read API (candidates + evidence view). Visual polish is
   Phase 6 by plan.
