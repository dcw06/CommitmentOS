# Phase 5B Evidence Pack

Measured results for the golden-run campaign and the live D4 security probes
(`docs/phase5_plan.md` Chunk 5B; checklist Part II D4; build plan §16.5).

## Sanitization rules (same as every evidence directory)

- Never include credentials, tokens, cookies, channel secrets, OAuth
  material, or email bodies.
- The controlled account's real address, real Gmail thread/message IDs, and
  real Calendar event IDs stay out of this directory and every committed
  doc. Identities are referred to by alias (`controlled-01`, `owner`).
  Calendar/observation IDs in the JSON reports are app-derived hashes, not
  provider IDs, and carry no personal data — but truncate them in prose.
- Review any screenshot for secrets in URLs, headers, and browser tabs
  before saving.

## What lands here

| File pattern | Produced by | Contents |
|---|---|---|
| `golden_run_NN_<tag>.json` | `scripts/run_golden_path.py run` | one run's checkpoints + §16.5 metrics (latency, verified minutes, replay digest) |
| `security_probes_<tag>.json` | `scripts/run_phase5b_security.py all` | D4 live probe results per contract, zero-side-effect assertions |
| `phase5_gate_summary.md` | owner, after the campaign | the ten-run table + gate sign-off |

The gate closes when ten **consecutive** `golden_run_*` files pass with §16.5
metrics inside budget and the `security_probes_*` report is all-green. A
failed run stops the campaign; fix, regression-test, redeploy, and restart
the count.

## Recorded deviations (mirrored from the driver docstrings)

- **Thread mode renders the full thread each run.** A static committed thread
  renders in full, so the M1→M2→M3 staged ownership/deadline evolution
  appears as one interpretation plus convergent replays rather than three
  timed observations. The staged evolution itself was proven live at the
  Phase 2 gate (request → acceptance → deadline-change, one commitment, two
  revisions). The campaign proves the *rest* of the loop live per run.
- **Block elapse is a driver stand-in.** The campaign compresses the
  scenario's multi-day clock; the driver performs the same durable
  `planned → awaiting_check_in` transition and `work_check_in_required`
  audit record the 4C safety scan applies at scheduled time. The scan itself
  was proven live at the Phase 4C gate and is exercised by the local
  rehearsal (`tests/golden_path/`) under a controllable clock.
- **The conflict is inserted through the Calendar API.** Owner-approved
  convention from the Phase 4 gate; the demo video uses the Calendar UI.
- **The web session is minted directly in Firestore.** Stand-in for browser
  login (same hashed-token shape the production AuthRouter writes); the
  AuthRouter's own flow is proven by the live session probes in
  `security_probes_*` and the D4 contract suite.
- **Webhook rate-limit exceedance is an owner-run step.** Left out of the
  automated probe set to avoid tripping the durable limiter outside a gate
  window; unit + restart-durability coverage is in
  `backend/tests/contract/test_webhook_rate_limit.py`.
- **The driver settles and proves the watch channel before the conflict
  leg.** The scenario clock separates planning from the conflict by a day;
  the campaign compresses that. Google throttles channel notifications
  after a burst of changes (observed live 2026-08-15: echo deliveries at
  10-second spacing, then the conflict insert's notification swallowed
  entirely, while a change after a quiet window pushed within one second).
  The driver therefore waits for the channel to go quiet after the create
  burst and inserts a transparent far-future probe event — outside the
  planning horizon and capacity math, removed by the between-runs reset —
  confirming delivery before the timed conflict insert. The measured
  conflict-to-repair latency still covers the full watch → webhook → sync →
  classification → repair → executor path.
- **The driver may deliver a stuck task payload directly to the
  authenticated handler.** Back-to-back campaign runs reuse deterministic
  domain IDs (same thread → same commitment → same approval → same
  continuation observation), so a generation-0 task name can fall inside
  Cloud Tasks' 24-hour name retention from the previous run and the
  enqueue converges silently on the consumed name (observed live
  2026-08-15: a continuation observation stuck `queued`/attempts=0 across
  five `dispatch_pending` ticks). Unreachable in production — observations
  are immutable and never purged-and-recreated; only the campaign reset
  does that. Task names are a transport deduplication aid (plan §7.3); the
  observation CAS and the executor's guarded claim remain the product
  idempotency boundaries, and the direct delivery uses the same OIDC
  handler path as the recorded replay contract.
