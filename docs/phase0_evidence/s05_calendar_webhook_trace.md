# Section 5 evidence — Calendar watch delivery and webhook boundary

Captured 2026-08-12. Controlled account referred to as `controlled-01`.

## Chain proven

Spike-owned Calendar event insert → `events.watch` notification → public
webhook (channel-token validated) → coalesced durable sync request → named
source-sync Cloud Task → bounded incremental `events.list` fetch with an
unpromoted candidate sync token.

## Registration

| Field | Value |
|---|---|
| Original channel | `bf3e855f-676c-4458-a911-55abddd0cba4`, resource `8UMl0oXhKo82Dpb7vDPvSIbSO68` |
| Expiration | 2026-08-19T04:10:57Z (7 days) |
| Baseline sync token | Recorded as published cursor before the channel opened |
| Secret handling | Only the SHA-256 hash of the channel token persisted; raw value lives in Secret Manager alone |
| Registration handshake | `sync` state notification validated and recorded at 04:13:22Z; no task enqueued for a handshake |

## Invalid-request matrix (live service, zero side effects verified)

| Case | Result |
|---|---|
| 1. Missing channel token | 401 |
| 2. Incorrect channel token | 403 |
| 3. Valid token, unknown channel ID | 403 |
| 4. Valid token, mismatched resource ID | 403 |
| 5. Valid token, invalid resource state | 400 |
| 6. Non-empty body | 400 |
| 7. Wrong HTTP method | 405 |

After all seven probes the calendar `sync_requests` document was absent —
no durable write, no task, no Google API call resulted from any invalid
request. Cases 3–5 carried the genuine channel token, proving rejection
does not rely on token secrecy alone.

Note: an initial run of cases 2–5 returned 500 because the runtime service
account had no access to the channel-token secret (least-privilege gap);
the failure occurred before any durable write. Fixed by granting
`secretmanager.secretAccessor` on that one secret.

## Valid delivery trace

| Step | Observation |
|---|---|
| Poke event inserted | `rh7uc783cm2gvd2p9g0i35pe8o`, spike-owned via private extended property |
| Webhook notification | Validated, coalesced into `sync_requests/calendar:…`, named task `calsync-…-198615` |
| Worker fetch | 1 changed event seen in one bounded page (max 25) |
| Candidate sync token | Present, held on the request document |
| Published sync token | Unchanged throughout |

## Duplicate-signal convergence

Two identical synthetic signals (same message number 999001, valid token,
current channel) were replayed at the live webhook: both returned 204,
`signal_count` rose to 3 on the single request document, and named-task
dedup absorbed the second dispatch — 2 worker executions total across 3
signals, converging on one consistent fetched state. The re-fetch is
idempotent by construction because the published cursor never moved.

## Renewal

| Field | Value |
|---|---|
| New channel | `7edd3e89-6829-4af5-9009-716d34343d3c`, expires 2026-08-19T04:17:49Z |
| Replacement metadata | `previous_channel_id` / `previous_resource_id` recorded for overlap acceptance |
| Old channel | Stopped via `channels.stop` immediately after the new channel opened |
| Credential | Production-issued refresh token (secret v4) — completes the Section 3 watch-renewal row for Calendar |

## Deferred from this section

- Durable per-channel rate limiting (current limiter is per-instance,
  in-memory, 20 signals/60 s) — Phase 5.
- Full staging-generation protocol and 410 full-resync recovery — Phase 4
  (the worker records `full_resync_required` on a 410 but does not yet
  rebuild).
