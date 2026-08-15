# Phase 5 Gate Summary — Golden Campaign and Live Security Evidence

**Campaign date:** 2026-08-15 (Day 6; Phase 5 scheduled Days 15–17)
**Deployed revision under test:** `commitmentos-00042-fcj` (traffic `--to-latest`)
**Driver:** `scripts/run_golden_path.py` in `--thread` gate mode — every run
re-observes the real golden thread in the controlled mailbox with live
Gemini interpretation. Recorded deviations: this directory's README.
**Gate (plan §17 Phase 5 / §6.2):** ten consecutive golden-path runs with
all §16.5 acceptance metrics inside budget, measured results preserved.

## Ten consecutive passing runs

| Run | Tag (UTC) | Conflict→repair (s) | Verified minutes at completion | Checkpoints | Replay digest (state byte-identical) |
|-----|-----------|--------------------:|-------------------------------:|------------:|-----------------|
| 01 | 20260815t063708 | 9.314 | 120 | 61/61 | bb44fc0dfd0cf88f |
| 02 | 20260815t064428 | 8.804 | 120 | 61/61 | a77289c3522d9e3e |
| 03 | 20260815t065510 | 10.082 | 120 | 61/61 | b2799a63faf159bb |
| 04 | 20260815t070251 | 9.931 | 120 | 61/61 | 7f951f0dd25cbac6 |
| 05 | 20260815t070919 | 10.198 | 120 | 61/61 | 2af12192251b0316 |
| 06 | 20260815t071601 | 8.901 | 120 | 61/61 | f1883b7f2cc2eaa9 |
| 07 | 20260815t072249 | 8.882 | 120 | 61/61 | c131101db08db8fe |
| 08 | 20260815t072928 | 7.221 | 120 | 61/61 | 9cad320b40d4d229 |
| 09 | 20260815t073553 | 10.127 | 120 | 61/61 | ebdca87e15c56874 |
| 10 | 20260815t074239 | 7.797 | 120 | 61/61 | 859c6fa5580281cd |

Latency min/mean/max: **7.221 / 9.126 / 10.198 s** — every run inside the
15-second warmed budget and far inside the 60-second operational budget,
on a scale-to-zero deployment (no warmed instance was configured).

Every run's 61 checkpoints include the §16.5 acceptance surface: exactly
one commitment from the three-message thread (zero duplicates under
replay), `my_commitment` ownership, confident future deadline, effort
confirmed at 180, plan approved with every block inside the hard-constraint
envelope, zero double-allocated intervals across the portfolio, stable
derived Calendar event IDs held across runs, all creates through
outbox → authenticated executor, verified check-ins with idempotent
redelivery (`check_in_already_recorded`), conflict inserted → exactly one
minimal repair moving one block with its event ID preserved and every
unaffected block byte-identical, second commitment untouched, conflict
meeting untouched, explicit completion with honest verified minutes
(120 of the 180 estimate — closure without fabricated work), pending
check-ins closed by the terminal state, the frozen 18-step audit order,
and full replay of every observation and action leaving durable state
byte-identical. Run 01's placements are the frozen canonical layout; runs
02–10 reproduced the identical placements.

## Campaign interruption note (owner adjudication)

Runs 01–02 and runs 03–10 executed as two driver processes: the
between-runs reset before run 03 aborted once on its own
`state_changed_since_preview` drift-guard (the previous run's replayed
actions were still settling) and the campaign stopped by design; the
driver was hardened (reset retries after a settle wait; between-run pause
raised to 45 s) and resumed with `campaign --start 3 --count 8`. **No run
failed** — every produced run passed, timestamps are continuous
(06:37–07:49 UTC), and the reset that aborted made no mutation. Recorded
here so the owner can accept the ten runs as consecutive or order a fresh
uninterrupted campaign.

## Live D4 security evidence

`security_probes_20260814t160344.json` — all green against the deployed
service: session negative matrix through the production AuthRouter
(allowlisted redirect targets, state replay, logout/revocation), CSRF
negatives on every controlled mutation route, wrong OIDC audience and
identity on all three internal route groups, the complete `/demo`
mutation matrix (POST/PUT/PATCH/DELETE across every production mutation
path), and seeded demo reads. **Webhook rate-limit exceedance remains the
documented owner-run step** (unit + restart-durability coverage in
`backend/tests/contract/test_webhook_rate_limit.py`).

## Findings the campaign surfaced (all fixed same day)

1. **Product defect — cancelled-corpse adoption (fixed + deployed):**
   Google permanently reserves a Calendar event ID; the executor's
   insert-or-adopt path recorded `succeeded` after adopting a cancelled
   corpse left by the between-runs reset. `insert_or_adopt_owned` now
   revives an owned cancelled event via `events.update` (desired body,
   `status: confirmed`, `If-Match` on the corpse etag; concurrent edits
   surface as the typed 412 path). Narrowly production-reachable (user
   deletes an owned event while a create retry is in flight). Regression:
   `backend/tests/unit/test_calendar_writer_corpse_revival.py` (3 tests);
   fake calendar semantics aligned with Google (cancel leaves a
   retrievable tombstone). Deployed in `commitmentos-00042-fcj`.
2. **Watch-channel burst throttling:** Google delivers channel
   notifications at 10-second spacing after change bursts and can swallow
   a notification entirely; a change after a quiet window pushes in ~1 s.
   Driver: settle wait + transparent liveness probe before the timed leg
   (README deviation).
3. **Cloud Tasks 24-hour name retention vs deterministic IDs:**
   back-to-back runs recreate identical observation/action IDs whose
   generation-0 task names the prior run consumed; enqueue converges
   silently and no server path re-dispatches a stuck `queued` observation.
   Unreachable in production (observations are immutable and never
   purged-and-recreated). Driver: direct OIDC delivery through the
   replay-contract path (README deviation).
4. **Scenario-state pollution:** leftover Phase 3 busy fixtures (notably
   an all-day Sunday hold) forced the only repair placement beyond the
   24-hour policy threshold — the policy escalated correctly
   (`single_shift_exceeds_24_hours`), which is product correctness but a
   campaign environment defect. Fixtures removed; the campaign calendar
   is exactly the seeded scenario state.
5. **Driver races (fixed in driver):** single-shot event verification
   raced rescued creates (now polls); the reset raced the prior run's
   replay tail (now retries after settle).

Context finding from the 08-14 preparation window, recorded for
completeness: an OAuth reconnect briefly stored the wrong account's
refresh token (secret v6, disabled), which poisoned the thread baseline
and registered a stray Gmail watch on the admin mailbox; the correct
token (v7) has been enabled since 16:01 UTC 08-14, the baseline was
re-recorded from the controlled mailbox, the Pub/Sub retry backlog was
seeked, and the stray watch expires by ~08-21.

## Gate status

- Ten consecutive passing golden runs: **complete** (table above).
- §16.5 measured metrics: **inside budget**, preserved per run in this
  directory.
- D4 live probes: **all green** except the owner-run rate-limit
  exceedance step.
- Owner sign-off: ☐ pending (accept the interruption note, run the
  rate-limit exceedance step, then record the decision-log entry).
