# Section 4 evidence — Gmail watch delivery trace

Captured 2026-08-12. All identifiers sanitized per the evidence-pack rules; the
controlled mailbox is referred to as `controlled-01`.

## Chain proven

Gmail change (test email into `controlled-01`) → `users.watch` notification →
Pub/Sub push with OIDC → deployed ingress validation → coalesced durable sync
request → named source-sync Cloud Task → bounded `history.list` fetch with
unpromoted candidate cursor.

## Watch registration

| Field | Value |
|---|---|
| Initial history ID | 17427 |
| Watch expiration | 2026-08-19T03:46:40Z (7 days) |
| Credential | Production-issued refresh token (secret v4) — registration succeeded with the refreshed credential |
| Published cursor doc | `sync_cursors/gmail:user_fixture_controlled_001` |

## Negative evidence (real tokens, wrong expectation)

2026-08-12 03:50Z: a stale deployed env (placeholder service-account identity
from an overwritten local `.env`) caused the ingress to reject genuine
Google-signed Pub/Sub pushes with HTTP 403 — repeatedly, with zero Firestore
writes and zero task dispatches. This demonstrates the wrong-identity
rejection path of route contract B with production tokens rather than
synthetic ones. Unauthenticated (401) and garbage-token (403) probes were also
verified against the deployed service with zero side effects.

## Positive delivery trace (after env fix, Pub/Sub automatic retry)

| Time (UTC) | Hop | Result |
|---|---|---|
| 03:52:19–20 | Final stragglers against stale expectation | 403, no side effects |
| 03:52:22 | 3 × Pub/Sub push accepted (retried backlog) | 204 after durable commit |
| 03:52:22–24 | 3 × named source-sync task executions | 200 |

## Durable state after the trace

| Field | Value | Meaning |
|---|---|---|
| `delivery_count` | 3 | Three Pub/Sub deliveries coalesced into one sync request document |
| `latest_history_id` | 17543 | Max history ID across coalesced deliveries |
| `status` | fetched | Worker completed the bounded fetch |
| `history_records_seen` | 4 | One bounded page (max 25), no further page |
| `next_page_token_present` | false | — |
| `candidate_history_id` | 17550 | Held on the request; **not** promoted |
| `published_history_id_unchanged` | 17427 | Authoritative cursor untouched, as designed |

Task payloads carried only `{schema, source, user_id, latest_history_id}` —
no message bodies, no OAuth material. Task names
(`gmailsync-<user>-<historyId>`) provided transport-level dedup for retried
deliveries of the same coalesced state.

## Deferred from this section

- Crash-gap repair (sync request committed without its task) — design supports
  manual re-dispatch; the periodic dispatcher is Phase 1.
- Full staging-generation protocol (multi-page checkpoints, fenced publication)
  — Phase 2 gate.
