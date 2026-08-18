# Interactive sandbox hardening progress

Date: 2026-08-18  
Status: source-complete; local verification green; owner deploy pending

This record covers the judge-path defects reproduced against the deployed
`/sandbox` revision. It does not modify or extend any frozen evidence under
`docs/phase*_evidence/`.

## Correctness and truthfulness

- A fixed card now has a deterministic semantic contract in addition to the
  production evidence/authority validator. Schema-valid live output with the
  wrong outcome, ownership, identity operation, target, or deadline day falls
  back to the recorded interpretation and is labeled `recorded-fallback`.
- Cache keys include the full source metadata and candidate context. Failed or
  semantically rejected live calls are cached too, preventing repeated model
  spend on a known-bad fixed result.
- The result cache remains process-shared, but every session owns its interpreter
  wrapper and provenance. A fresh world therefore begins at `not-run`; another
  judge can no longer relabel it `live` or `live-cached`.
- Message outcomes compare actual semantic before/after state. An identity pause
  or duplicate can no longer be described as a successful revision, and the
  thread retains the truthful outcome detail that was actually produced.
- The frontend renders every commitment and raises a visible divergence warning
  if the authored one-commitment invariant is ever broken.

## Story order, timing, and session safety

- A new session begins with an explicit lane choice. Guided story and free play
  are mutually exclusive until reset, so authored Gmail timestamps, card-click
  order, and judge-authored messages can no longer produce two contradictory
  thread chronologies or contaminate one another's workflow state.
- Free play requires a visible subject before the first message. The backend
  assigns a distinct thread id and monotonically increasing sent times, and the
  UI/API carry that subject with every rendered message.
- The authored order is enforced server-side: request, acceptance, effort
  confirmation, first-plan approval, deadline change, conflict, time advance,
  and check-in. The guided tour follows the same sequence.
- The deadline message now occurs before the first planned block. Conflict
  injection refuses past blocks, so the sandbox cannot retroactively book over
  already elapsed time.
- Every read or mutation is serialized by a per-session async lock. Concurrent
  requests for one single-use card produce exactly one mutation and one 409.
- `POST /sandbox/api/session/reset` atomically releases the old world before
  opening the replacement. Manual resets no longer leak capacity for 45 minutes.
- State reads no longer refresh the idle deadline, every world also has a
  two-hour absolute lifetime, and opening worlds is limited to 12 per minute per
  process in addition to the 40-world concurrency ceiling.
- The documented Cloud Run deploy command enables session affinity, which is
  required for intentionally process-local worlds. Instance recycle and idle
  expiry still recover by opening a clean session.

## Deployment and model capability boundary

- `scripts/deploy_commitmentos.py` is now the owner-run release gate. It
  deploys with session affinity and `maxScale=2`, routes 100% to the latest
  ready revision, and then reads the effective service description back. It
  fails on affinity, scale, traffic, or environment drift and also proves the
  served custom-message API route and free-play UI bundle are present. The
  deploy path sends one isolated custom message and requires a live sandbox
  model result, proving the runtime can access the distinct secret/key.
- Production boot requires
  `COMMITMENTOS_SANDBOX_GEMINI_API_KEY_SECRET_REF` and rejects it if it equals
  the controlled-data Gemini secret reference. The secret must contain a key
  issued by a sandbox-only Gemini quota project.
- The composition root constructs separate interpreter, lazy client factory,
  API key, and cached client instances for controlled-data work and public
  sandbox work. The sandbox factory retains only its Secret Manager reference;
  it no longer closes over the `ApplicationContainer` object graph.

## Presentation

- A true 390 px Playwright viewport has no horizontal overflow
  (`innerWidth == scrollWidth == 390`). The tour becomes a compact grid, the
  reset control spans the card, content can shrink/wrap, and the calendar owns
  any necessary internal horizontal scrolling.
- A fresh page displays `interpretation: not run yet` rather than inheriting a
  process-global model label.

## Judge-authored messages

- A resettable mode chooser opens either the guided deck or an isolated
  free-play thread. In free play, a judge can
  select Jordan Ellis or You, submit any non-empty message up to 1,000
  characters, and send several messages consecutively from the same persona.
  Every send receives a unique Gmail message/observation id and traverses the
  same transient thread renderer, interpreter, validator, identity resolver,
  and durable command workflow as a guided message.
- Free-play is bounded to 8 messages per session and a rolling 12 requests per
  minute per process. All model methods share a second rolling ceiling of 12
  invocations per minute, two concurrent calls, and 12 calls per session;
  explanations cannot bypass the extraction allowance. Guided inputs alone
  may use the 256-entry process cache (and 512-entry coordination-lock table).
  Judge-authored source and structured output remain only in their world and
  are discarded on reset/expiry. The owner
  deployment command caps Cloud Run at two instances, bounding aggregate
  service traffic to at most two process-local rolling ceilings; the distinct
  sandbox quota project is the service-wide spend backstop across processes.
- Arbitrary text never selects a guided card by copying its body and never uses
  a recorded interpretation. If Gemini is unavailable or rejects the request,
  the visible source becomes `custom-unavailable`, durable commitment state is
  unchanged, and the UI explicitly says no canned result was substituted.
- The sender is a server-side enum, not a caller-supplied address: `you` maps to
  the sandbox controlled user/outbound Gmail direction and `jordan` maps to the
  fixed simulated correspondent/inbound direction. No real address or mailbox
  can be named.
- Model calls have a 20-second sandbox transport timeout and matching
  application deadline. SDK retries are disabled. The adapter retries once
  without thinking only for the narrow HTTP-400 "thinking config unsupported"
  compatibility response; auth, quota, 429, timeout, and 5xx failures make one
  provider call.

## Dynamic free-play output

- Effort proposals retain the workflow's `proposed_minutes` value through the
  API. Missing effort stays visibly blank and cannot be approved until the judge
  enters 15–2,400 minutes; the UI no longer invents a three-hour default.
- Calendar block labels, completion notes, commitment panels, evidence, and
  approval values derive from the free-play commitment. Once a free-play
  commitment has an approved plan, judges can inject a calendar conflict,
  advance past a block, and record a bounded verified-minutes check-in through
  the same real command stack. Shorter blocks log their actual duration rather
  than claiming the guided story's fixed 60 minutes.
- Approval controls now expose the stored reason and proposed outcome, preserve
  calendar-choice and confirmed-ownership fields, and offer both approve and
  reject paths with an optional rejection reason. Ambiguous ownership can be
  resolved as the judge's commitment, a request to the judge, or a commitment
  made to the judge.
- Idle and absolute expiry now schedule deletion when a world is created or
  touched. Lazy eviction remains a defense in depth, but private custom text no
  longer waits for a subsequent request before leaving process memory.
- The privacy banner now says exactly what crosses the model boundary: no Google
  account or controlled-user data is used, free-play text is sent through the
  sandbox-only Gemini key, retained in process until reset/expiry, and never
  written to Firestore or the shared guided cache.

## Browser logic follow-up

- Explicit retraction is now a first-class `cancel_existing` identity
  operation. The prompt names it, the strict wire contract accepts it, and a
  deterministic high-confidence retraction guard prevents a cancellation
  quote from becoming a positive `create`. A unique in-thread obligation is
  canceled without creating a replacement; an unauthorized participant's
  attempt is held for confirmation.
- A counterparty's proposed change to a controlled-user deadline no longer
  updates the authoritative commitment. The existing deadline remains binding
  while an identity approval carries the proposed date and quote. Acceptance
  applies the update to the same record; rejection leaves it unchanged.
- Terminal completion clears the old projection. The sandbox derives retained
  verified minutes from work-block history, hides terminal risk/remaining-work
  values, and reports reserved, completed, and canceled Calendar totals
  separately.
- The current deadline's evidence is rendered first and labeled as such;
  earlier and completion evidence remain visible. An elapsed block's projection
  refresh now says it is awaiting a verified-minute check-in instead of claiming
  verified progress already changed.
- Cancellation outcomes include the lifecycle transition in their narration;
  accepted counterparty deadlines use the dedicated
  `deadline_change_confirmation` request and audit event; the originating
  thread note updates after acceptance or rejection; blank and duplicate
  evidence excerpts are removed before rendering.
- The guided conclusion now links to seeded demonstration data, not “live
  data.”

## Local verification

```text
.venv/bin/pytest
306 passed

.venv/bin/ruff check .
All checks passed!

cd frontend && npm run build
production build completed successfully (30 modules)

Playwright viewport audit
innerWidth=390, scrollWidth=390, bodyWidth=390
sender payloads=[you, you], rendered thread messages=2
```

## Required owner action

The browser-logic follow-up remains local until the owner runs
`.venv/bin/python scripts/deploy_commitmentos.py --deploy`. The release gate
includes affinity, the two-instance cap, latest traffic, API, current workflow
controls, and truthful process-retention privacy copy. Then rerun the sandbox
security probe and authored interactive story before describing this follow-up
as live-verified. Do not overwrite prior evidence files.
