# Section 6 evidence — stable Calendar identity, If-Match, forced 412

Captured 2026-08-12 via `scripts/calendar_identity_spike.py run` (all checks
self-verifying; full sequence passed first run). Provider-behavior proofs
executed from local spike code with the production credential; the outbox
executor that owns this path in production is Phase 1's seeded-slice gate.

## Stable identity

- Derivation `base32hex(sha256("commitmentos:v1" + calendar_id + work_block_id))`
  produced deterministic, base32hex-safe 52-char IDs, e.g.
  `47sk5n3n…sekg` for `block_spike_identity_001`.
- Insert with the client-supplied ID succeeded; the provider returned the same ID.
- Insert **retry** was rejected by Calendar with HTTP **409** ("identifier
  already exists"); the adoption lookup fetched the existing event and verified
  its private ownership properties — exactly one event existed for the work
  block afterward. This closes the create-before-record crash window.
- Adoption was **refused** when an event carried mismatched ownership
  properties (wrong `work_block_id`), proving adoption depends on property
  verification, not ID possession alone.

## Conditional mutation

- Patch with `If-Match: <observed etag>` succeeded; the event ID remained
  identical across the plan-revision change (revision lives in extended
  properties and the patch body, never in identity).
- Conditional cancellation (delete with `If-Match`) succeeded.
- The ownership guard refused to patch a non-owned event; the unrelated event
  was byte-identical afterward.

## Forced 412

Sequence: record etag → intervening edit changes the event → replay the stale
conditional patch.

- Result: HTTP **412**, no overwrite, no blind retry with a refetched etag;
  the event retained the intervening editor's state (summary and etag
  unchanged by the stale attempt).
- Exact response shape for the outbox `stale_precondition` state machine:

```json
{
  "error": {
    "code": 412,
    "message": "Precondition check failed.",
    "errors": [
      {"message": "Precondition check failed.", "domain": "global", "reason": "failedPrecondition"}
    ],
    "status": "FAILED_PRECONDITION"
  }
}
```

## Cleanup

Preview listed exactly one owned event (by `managed_by=commitmentos` extended
property); ownership was re-verified before deletion; no unrelated event was
touched. The unrelated guard-test event was removed separately as a test
artifact, outside the owned-mutation path.
