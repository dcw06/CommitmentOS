# Phase 5 Split — Completion and Hardened Surface (5A), Golden Campaign and Gate (5B)

**Authority:** plan §17 Phase 5 (Days 15–17), §4.5 (completion invariant),
§11.2 (progress evidence), §13.1/13.3 (autonomy policy, cleanup, data
minimization), §13.5 (endpoint authentication), §16.1–16.5 (evaluation and
acceptance metrics); checklist Part I §1 (frozen `golden_scenario_rev_1`,
18-step audit contract) and Part II D4 (all security-hardening rows), plus
the D1 lease row left open by design (worker-kill/takeover fault injection).
Phase 4 closed live 2026-08-14 (Day 5, eight days ahead of schedule).

Phase 5 splits on the project's build-versus-prove seam. 5A builds every
surface the golden scenario and the D4 matrices still lack and proves it
locally; 5B is pure operations and evidence — the scripted golden-run
campaign against the deployed service, the live security probes, and the
gate. 5B writes no new product code except fixes for what its runs expose.
The official Phase 5 gate — "All Section 16 acceptance metrics pass with
measured results preserved" — closes at the end of 5B.

Already closed by earlier phases (credited, not repeated): work-block
check-ins and the guarded check-in route (3A), monitoring and
automatic-action pause with control epochs (Phase 1, live pause-proof),
log redaction (Phase 1, B3), prompt-injection fixtures and delimiter
neutralization (Phase 2, §16.1 eval at 100%), 412 stale-precondition
machinery (Phase 1/4B local; provider 412 proven live in Phase 0 §6),
stale-held-action resumption (Phase 1 live), projection-provenance refresh
(4C), serialized concurrent Gmail delivery (Phase 2). Sent-message
completion inference stays out of P0 regardless of schedule (plan §6.2).

**2026-08-14 amendment:** the dedicated 4A/4B live exits closed after this
plan was written (`docs/phase4_evidence/phase4ab_gate_run.md`, 65/65):
real user-move adoption and the deployed-executor forced 412 with
synchronized resume are now credited, so the former 5B carried-over item
is gone. That run also hardened the guarded approval route (Calendar
`choice` forwarding) and added the explicit `restore_approved_slot`
action, both with regression coverage — the suite baseline entering
Phase 5 is 151 tests.

## Chunk 5A — Completion truth and the hardened surface

**LOCAL EXIT CLOSED 2026-08-14** — all seven items below are implemented
and locally proven: 220 tests green (baseline 151), Ruff clean, targeted
mypy clean, and the golden dry-run reaches `completed` through the real
workflow. Full record: `docs/phase5a_progress.md`; decision-log entry in
the checklist. D4 checkboxes close at the 5B gate with live evidence.

Everything still missing before a golden run can reach its terminal step,
plus every D4 security surface, proven locally.

1. **Manual completion** — fill the `CompleteCommitment` stub and add the
   guarded route (controlled session + CSRF resolving before body
   validation, client idempotency key, expected commitment revision).
   One transaction writes the completion evidence record, `completed_at`,
   and the terminal lifecycle transition; the continuation observation
   closes pending check-in requests (golden audit step 17). Enforce the
   §4.5 invariant end to end: verified minutes are never fabricated to
   match the estimate, and later reconciliation, replayed observations,
   and the periodic safety pass keep the commitment closed. A completed
   commitment leaves the portfolio demand set — completing the live
   overdue `daf9a729…` will also clear the standing portfolio
   infeasibility surfaced by the Phase 4 gate finding.
2. **AuthRouter port (D4 session matrix)** — fill the `AuthRouter` stub,
   replacing the retained Phase 0 spike login as the session issuer. Keep
   the proven state/nonce/PKCE single-use transaction machinery; add the
   full negative matrix as contract tests: allowlisted redirect targets
   only; missing, mismatched, expired, and replayed state; mismatched
   nonce; callback replay cannot create a second session; logout revokes;
   expiry and revocation enforced. Remove the last spike modules from the
   live path and re-audit the route inventory (§13.5).
3. **Full CSRF suite (D4)** — one parametrized contract covering every
   controlled mutation route, including the new completion route: missing
   and invalid CSRF ⇒ 403 with byte-identical durable state.
4. **Full demo mutation matrix (D4)** — demo client separated from the
   live API client; enumerate every production mutation method/path under
   `/demo` and prove each is rejected with zero Firestore writes, task
   dispatches, OAuth access, or Google API calls; seeded mode exposes no
   live mutation controls.
5. **Webhook rate-limit negatives (D4)** — the durable per-channel limit
   shipped in 4A; add the exceedance test (over-limit valid signals ⇒
   429 with zero side effects) and record the evidence.
6. **Audited controlled-account cleanup (D4)** — the documented developer
   command: preview targets before mutation, delete only events carrying
   valid CommitmentOS ownership properties, leave unrelated events
   unchanged, record the cleanup in the audit timeline. Built as a library
   command with a script entry so 5B can use it as the between-runs reset;
   it also disposes of the two stale Phase 2 identity approvals and the
   pre-fix `19b1acfb…` action approval.
7. **Remaining fault-injection rows (§16.4, local)** — worker-kill and
   fenced-lease takeover on reconciliation (the open D1 row: a late
   original worker cannot commit); executor death while `action_in_flight`
   both before and after the Calendar response; create-before-record crash
   converging on one event through the stable ID (§9.4); projection
   corruption blocking stale action execution; mid-generation worker kill
   resuming from the durable checkpoint (credit Phase 2's coverage, extend
   where thin). These land in the empty `tests/fault_injection` scaffold.

**5A exit:** full suite green with the new contract and fault tests, Ruff
and targeted mypy clean, D4 rows checkable except their live probes, and a
local golden dry-run reaching `completed` through the real workflow.

## Chunk 5B — Golden-run campaign, live evidence, and the Phase 5 gate

Pure operations against the deployed service. Product code changes only as
fixes for findings, each with a regression test.

1. **Golden-run driver** — fill the `GoldenPathRunner` stub
   (`scripts/run_golden_path.py`): seed the date-shifted
   `golden_scenario_rev_1` state per the checklist §1 relative-time
   convention (seed day `T`, portfolio second commitment, busy events);
   script every user decision through the guarded routes (effort 180,
   plan approval, check-ins, completion) with the scenario clock; insert
   the conflict via the recorded Calendar-API convention from the Phase 4
   gate; assert the frozen 18-step audit contract and §16.5 metrics per
   run; reset between runs with the 5A cleanup command. Exact placements
   are recorded at the first successful run and frozen for the remaining
   nine (checklist §1 rule).
2. **Live security evidence (D4 live)** — against the deployed revision:
   full session negative matrix through the new AuthRouter, OAuth
   replay, CSRF probes on every mutation route, wrong OIDC audience and
   identity on all three internal groups, the complete `/demo` mutation
   matrix, and webhook rate-limit exceedance — each verified for zero
   durable or external side effects.
3. **Campaign preconditions** — reconnect the controlled account and renew
   both watches per the runbook immediately before the campaign (watches
   expire ~08-19); complete or clean up `daf9a729…` so the demo portfolio
   starts feasible; verify queues empty and snapshot published. (The
   former carried-over 4B live items — real user-move adoption and the
   deployed-executor forced 412 — closed 2026-08-14 in the dedicated
   4A/4B exit run; see the amendment above.)
4. **Ten consecutive golden runs** — the §6.2 hard gate, warmed for the
   repair leg. Measured results preserved in `docs/phase5_evidence/`:
   duplicates (must be zero), hard-constraint violations (zero), single
   allocation, audit completeness per mutation, conflict-to-repair latency
   (<60 s operational, <15 s warmed), replay safety, recovery rows.
   A failed run stops the campaign: fix, regression-test, redeploy, and
   restart the count — ten must be consecutive.

**5B exit = the Phase 5 gate:** all §16.5 acceptance metrics pass with
measured results preserved in `docs/phase5_evidence/`; checklist D4 rows
checked with live evidence; gate recorded in the decision log. Only then
does Phase 6 (competition delivery) begin.

## Sequencing and estimate

5A ≈ one to two working sessions — completion and the cleanup command are
small; the AuthRouter port is mechanical over the proven spike flow; the
fault-injection matrix is the deep half. 5B ≈ one to two sessions — the
driver is the build, the campaign is wall-clock. Both fit inside the
plan's Days 15–17 window with the current eight-day schedule lead.

Deployment note carried from the Phase 4 gate: Cloud Run traffic is
currently name-pinned, so a plain `gcloud run deploy` does not shift
traffic. Restore `--to-latest` routing (or update traffic explicitly per
deploy) before 5B's campaign; deploys and traffic changes are owner-run
commands.

Demo-data note: the moved block `99423c96…` (Fri 17:00–18:00Z), the
conflict meeting `ksf9h4qm…`, the Phase 3 busy fixtures, and overdue
`daf9a729…` remain on the controlled account. The 5A cleanup command plus
golden-run seeding replace this ad-hoc state with reproducible campaign
state; keep a screenshot of the current calendar first if it is wanted for
the demo narrative.
