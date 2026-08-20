# Sandbox deploy evidence

Live security probes recorded after the interactive judge sandbox
(`/sandbox`) was deployed. Kept separate from `docs/phase5_evidence/` so the
phase 5 pack keeps describing only the phase 5 golden campaign.

## `security_probes_20260820t051512.json` — 73/73 green

Recorded 2026-08-20T05:15Z against
`https://commitmentos-2hscowvydq-uw.a.run.app`, serving revision
`commitmentos-00059-t4z` at 100% traffic — the final pre-freeze revision,
adding the plan-repair receipt, explicit-closure copy, and terminal-risk
presentation fix. Same 73-probe matrix, all six groups green.

Live spot-check on the same revision: the conflict card returned in 0.62 s
with the receipt derived from the actual `plan_repaired` payload ("Meeting
observed → conflict detected → 1 block moved, 2 preserved → feasibility
restored"). Production logs from the same window show both interpretation
safety mechanisms operating: proposal narrowing accepted a live two-proposal
response as `live`, and a transient provider failure produced an honestly
labeled `recorded-fallback` that the ten-minute cache TTL retires.

## `security_probes_20260820t040040.json` — 73/73 green

Recorded 2026-08-20T04:00Z against
`https://commitmentos-2hscowvydq-uw.a.run.app`, serving revision
`commitmentos-00058-dtx` at 100% traffic — the revision carrying the
round-2 and round-3 judge-feedback passes (deterministic sandbox
explanations, proposal narrowing with fallback-cache TTL, ghost-block plan
preview, matched-hold narration, guided completion step, genuine model
metadata in evidence).

All six groups green, same 73-probe matrix as the previous revision. The
timed authored story on this revision also passed every acceptance gate:

- **Conflict card 0.62 s** (was 4.82 s before the explanation decoupling) —
  the repair response no longer waits on a live model call.
- **The deadline-change card labeled `live`**: real Gemini output accepted
  by the card contract via proposal narrowing (live call 2.79 s).
- Matched-hold narration, preserved-plan narration, and the ghost-block
  preview (3 proposed blocks, zero mutations before approval) all served
  live; exactly one block moved in the repair with the rest preserved.
- Evidence honesty held live: no simulated latency fields; genuine
  `gemini-3.5-flash` model id with wall-clock latency (2,258 ms) projected
  on the interpretation event; completion retained exactly the 60 verified
  minutes.

## `security_probes_20260818t153546.json` — 73/73 green

Recorded 2026-08-18T15:35Z against
`https://commitmentos-2hscowvydq-uw.a.run.app`, serving revision
`commitmentos-00055-ks8` at 100% traffic — the revision deployed through
`scripts/deploy_commitmentos.py` carrying the sandbox hardening and
judge-feedback passes.

All six groups green: `session` (12), `csrf` (15), `oidc` (6), `demo` (27),
`sandbox` (12), `ratelimit` (1). The `sandbox` group grew from 9 to 12 with
the free-play surface: custom messages require a session the caller created,
caller-defined message identities are rejected (the sender is a server-side
enum), and oversized custom messages are rejected before any interpretation.
A second consecutive run minutes later was also 73/73 (not preserved — same
revision, same result; this file is the canonical record).

The same session also verified the deployed authored story end to end via
the API: live Gemini interpretation on the first card
(`interpretationSource: "live"`), candidate → convergence → distinct
`awaiting_effort_confirmation` / `awaiting_plan_approval` stages → plan →
counterparty deadline held then accepted with the preserved-plan narration
("Feasibility was recalculated; … the plan was preserved unchanged") →
automatic conflict repair → elapse → 60 verified minutes → explicit
completion retaining them. Every activity event satisfied the audit-evidence
allowlist (correlation ids on all 37 events; `plan_repaired` carrying
`movedBlockCount: 1` and the planner version; outbox actions exposing
idempotency keys, expected `If-Match` etags, and observed response etags;
no message bodies anywhere in the projection).

## `security_probes_20260818t030803.json` — 70/70 green

Recorded 2026-08-18T03:08Z against
`https://commitmentos-2hscowvydq-uw.a.run.app`, serving revision
`commitmentos-00048-4ft` at 100% traffic.

All six groups green: `session` (12), `csrf` (15), `oidc` (6), `demo` (27),
`sandbox` (9), `ratelimit` (1).

The `sandbox` group is new with this deployment. It proves the interactive
surface is unauthenticated without being unguarded: a caller cannot read
state without a session it created, a forged session id is rejected, input
outside the fixed card deck is rejected, a card the state does not allow is
rejected, one session cannot see another's world, a fresh world holds no
commitments, sandbox responses carry no controlled-account identifiers, and
the read-only `/demo` surface still rejects every mutation beside it.

## Deviations and notes

- **`ratelimit` remains a documented owner-run step.** It self-reports green
  rather than driving the webhook over its limit, to avoid tripping the
  durable limiter outside a gate window. The substance is proven in
  `backend/tests/contract/test_webhook_rate_limit.py` (including restart
  durability). This is unchanged from previous gates — read the green mark
  as "skipped with a note", not "exercised".
- **Probes were run with `--service-url`.** The local `.env` carries
  development defaults (`http://localhost:8080`), so the deployed URL was
  passed explicitly rather than editing `.env`. This is the intended usage:
  `probe_oidc` derives its audiences from the service under test, not from
  local settings, so one flag retargets every group correctly.
- **`GRPC_DNS_RESOLVER=native` was set for this run.** The operator's shell
  blocked the gRPC c-ares resolver's direct UDP:53 lookups, which surfaced as
  `503 errors resolving firestore.googleapis.com` before any probe ran.
  Forcing gRPC onto the system resolver is an environment workaround with no
  bearing on the service or the results.

## Separately verified

The deployed sandbox was smoke-tested end to end before the probe run:
opening a session and playing the first card returned
`interpretationSource: "live"` — real Gemini interpretation on the deployed
revision — extracting the commitment as `request_to_me` with an exact
evidence quote and the deadline normalized to 17:30 Pacific.
