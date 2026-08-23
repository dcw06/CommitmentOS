# CommitmentOS — Devpost story (brief)

## What it does

CommitmentOS turns promises made over email into protected, continuously
managed work plans. Gemini 3.5 Flash reads Gmail threads and extracts who
promised what, to whom, by when — every claim anchored to an exact quote from
the message. Deterministic code does everything else: converges replies onto
one commitment instead of duplicating it, reserves Google Calendar blocks
behind two explicit approvals, repairs the plan in seconds when a meeting
lands on reserved time, and counts only the minutes you explicitly verify.
Completion is your decision. It is never inferred from time passing.

## How we built it

Taskmaster track. The whole thing is structured as a continuous control
loop, not a one-shot planner.

| Layer | Component |
| --- | --- |
| Signals | Gmail push (Pub/Sub) + authenticated Calendar webhook → named, idempotent Cloud Tasks; workers persist durable observations in Firestore |
| Interpretation | Gemini 3.5 Flash inside an ADK workflow (`commitment_interpretation_v2`) — evidence-quoted, deterministically validated before use |
| Identity | Deterministic resolver: requests ≠ commitments; an acceptance reply updates the same record |
| Planning | Deterministic portfolio planner (`stable-slot-score-v1`) — stable block identity makes repairs minimal |
| Policy & execution | Autonomy thresholds → transactional outbox → executor revalidating revisions and etags (`If-Match`) at the last instant; a 412 stales the intent |
| Durable truth | Firestore only; every decision lands on a correlated audit timeline |
| Safety net | Cloud Scheduler: watch renewal, cursor catch-up, once-a-minute reconciliation — push latency is an optimization, never a dependency |
| Judge surfaces | `/sandbox` — the real stack over an in-memory twin, sandbox-only Gemini key · `/demo` — seeded, read-only |

The sandbox is the demo surface. No login: a guided story, or free play
where a judge types any message and watches Gemini interpret it live —
model id, wall-clock latency, and the quoted evidence — then drives the
same planner, policy, and executor over an isolated world. Every audit
entry expands into execution evidence: revisions, policy reasons,
idempotency keys, expected and observed etags.

Solo, in 11 days. The rule that made it possible: Gemini only ever
interprets language — everything that can touch a calendar is
deterministic, guarded, and replayable. The agent keeps promises by
refusing to guess.

## The receipts

10 live end-to-end runs + 10 hardened seeded runs · 1,110 acceptance
checkpoints · 0 duplicate commitments or Calendar events · conflict-to-repair
7.2–10.2 s (mean 9.1 s) · 73/73 live security probes · 32/32 extraction eval
at 100%, including injection resistance, at ~$0.0008/message · 328 automated
tests · byte-identical replay of every observation and action. Each number is
indexed to the Cloud Run revision that produced it in `docs/proof_index.md`.

## Findings and learnings

- **Google permanently reserves Calendar event IDs.** Recreating a
  cancelled event's ID silently adopts its corpse; the executor now detects
  the tombstone and revives via `events.update` with an `If-Match` guard.
- **Calendar push throttles after change bursts.** The once-a-minute
  reconciliation floor turns lost notifications from an outage into a
  60-second delay — measured, not assumed.
- **Gemini sometimes returns a redundant restatement beside the right
  proposal.** Narrowing selects the proposal that passes every semantic
  check instead of discarding genuinely live output — validation was never
  weakened.
- **"Exactly once" is a product outcome, not a setting.** Named tasks +
  revision fences + stable event IDs + etag preconditions — proven by
  deliberately re-delivering everything and diffing durable state to zero.

## What's next

Dependencies between commitments, effort estimates that learn from verified
history, and more sources (Outlook, Canvas) feeding the same loop. The core
contract — evidence-backed detection, minimal repair, human-only completion —
doesn't change.
