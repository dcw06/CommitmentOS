# Interactive judge sandbox — design record

Added 2026-08-18, after the post-hardening gate closed. This is a **new
surface**, not a change to any gated path: the reconciliation engine,
executor, planner, policy, `/app`, `/demo`, and every internal route are
untouched. The ten-run campaign evidence in `docs/phase5_evidence/` therefore
still describes exactly what it always described.

## Why

Judge mode at `/demo` renders a finished story. Reading about an agent is
weaker evidence than driving one, and the alternative a judge might ask for —
logging into a real Google account we publish credentials for — is worse on
every axis: it would need multi-user OAuth we deliberately do not have, it
would break the moment Google challenges an unfamiliar login, and every judge
would be mutating the same controlled account that our measured evidence was
earned on.

## What it is

A simulated email thread the judge plays both sides of, plus world events (a
meeting lands on a reserved block, time passes, minutes get logged). The
production stack reacts: interpreter, `ModelOutputValidator`,
`CommitmentIdentityResolver`, `PortfolioPlanner`, repair policy,
`ExecuteCalendarAction`, and the audit timeline are all the real classes,
composed in `sandbox/world.py` exactly as `bootstrap/container.py` composes
them for the controlled user.

What is simulated is the world around the stack — the same in-memory twin the
test suite has used since Phase 1, promoted from `backend/tests/fakes.py` to
`commitmentos/sandbox/twin.py` (the test module now re-exports it, so both
consumers stay on one implementation):

| Port | Sandbox adapter | Fidelity note |
|---|---|---|
| Firestore | `InMemoryContext` dict store | Production repository + serializer code runs unmodified against it |
| Calendar | `FakeCalendar` + reader/writer | Mirrors etag `If-Match` 412s and Google's cancelled-event corpse/revival semantics |
| Gmail | `FakeGmailReader` | Thread fetch, ordering, and label semantics |
| Cloud Tasks | `FakeTaskDispatcher` | Named-task dedup; the engine delivers inline |
| Clock | `FakeClock` | Advanceable, so "let time pass" runs the real safety reconciliation |

## Isolation

The sandbox is the only unauthenticated mutating surface in the service, so
its safety is structural rather than procedural:

1. **No live surface exists in the composition.** `SandboxWorld` takes the
   twin adapters plus a model interpreter. There is no credentials provider,
   no Firestore client, and no controlled user id anywhere in it, so no
   request — well-formed or not — can name a live resource.
2. **No ambient authority.** The session id travels in an explicit
   `X-Sandbox-Session` header, never a cookie. A cross-site request cannot
   ride an existing session, so there is nothing for CSRF to forge.
3. **Fixed inputs.** Only card ids from the deck are accepted. Free text
   never reaches the model, so the surface is not a prompt-injection
   playground or an unbounded spend endpoint.
4. **Bounded cost.** Concurrent worlds, idle lifetime, and actions per
   session are capped, and model calls are cached per card.
5. **`/demo` unchanged.** The read-only judge mode keeps its blanket mutation
   rejection; the two surfaces share no state or route prefix.

Pinned by `backend/tests/contract/test_sandbox_contracts.py` and the
`sandbox` probe group in `scripts/run_phase5b_security.py`.

## The one live edge: interpretation

Interpretation is genuinely Gemini. Because the deck is fixed, the first call
for a given message is a real model call whose result is cached for the
process lifetime; later sessions reuse it. If no live interpreter is
configured, or the call fails or is rejected by the strict wire schema, the
card's recorded interpretation is used instead and the response labels which
path produced it (`live`, `live-cached`, or `recorded`), so a judge is never
shown model output that did not happen. The deterministic validator is
unchanged on both paths — evidence quotes must be exact substrings of the
source message, which is also what keeps the recorded fixtures honest.

**Recorded-proposal deviation.** Commitment ids are content-derived and
unknowable when a fixture is authored, so a recorded `update_existing`
proposal carries no `target_commitment_id`. The live model has no such
problem: the workflow passes it the candidate commitments with their ids.
`_bind_candidate_target` fills the recorded proposal's target from the sole
candidate, reproducing what the model does with the same context. The
validator's `identity_target_missing` rejection is left intact for every case
that cannot be resolved unambiguously.

## Sequencing deviation

`engine.resolve_approval` deliberately does **not** synchronize calendar
truth between a plan being proposed and the user approving it. Doing so
advances `calendar_state_revision`, which correctly stales the pending
planner run — the planner's own guard firing exactly as designed, but as a
confusing detour in a demonstration (the judge's approval would fail, the
system would re-propose, and they would have to approve twice). Executor
triggered syncs still run inside `drain()` after the plan is committed. This
is a property of the sandbox driver only; nothing in the guard changed.

## Known limits

- Sandbox state is process-local, so a Cloud Run instance recycle drops
  in-flight worlds. Judges see a fresh session rather than an error; this is
  acceptable because sandbox state is a demonstration artifact and never
  durable truth.
- The scenario is a single thread with one commitment. The portfolio
  contention story (several commitments competing for capacity) is visible
  in `/demo` but not drivable in the sandbox.
- Cards are single-use within a session; "start over" is the way to replay.
