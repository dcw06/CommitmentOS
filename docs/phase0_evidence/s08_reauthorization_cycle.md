# Section 8 evidence — reauthorization and invalid-refresh behavior

Captured 2026-08-12. Full live cycle against the deployed service, with the
production-mode credential.

## The cycle

| Step | Result |
|---|---|
| 1. Valid credential | Gmail and Calendar source-sync both `fetched` (4 history records / 4 changed events) |
| 2. Controlled revocation | Revoke endpoint HTTP 200 |
| 3. Gmail read after revoke | `{"status": "reauth_required", "detail": "credential refresh failed; dependent work stopped"}` |
| 4. Calendar read after revoke | Same `reauth_required` stop; state persisted on both sync-request documents |
| 5. Reauthorization | Fresh consent flow; refresh token stored as secret version 5 |
| 6. Code-replay test | Second exchange of the same authorization code → **HTTP 400, `invalid_grant`** |
| 7. Recovery | Both sources `fetched` again — the running instance picked up the new secret version without redeploy (credential cache cleared on failure) |
| 8. Watch renewal | Gmail watch re-registered (history 17550, exp 2026-08-19T07:10Z); Calendar channel rotated to `57939fb0…`, old channel stopped |
| 9. Hygiene | Dead secret version 4 destroyed; version 5 is the only enabled token |

## Findings

- **Revocation kills the in-memory access token too**, not just the refresh
  token: the deployed instance had a ~1-hour access token cached from step 1,
  and step 3 failed immediately — the API rejected it, google-auth attempted
  a refresh, and `RefreshError` surfaced. There is no grace window after
  revocation.
- **No silent fallback**: the `reauth_required` responses carry no source
  data; the durable sync-request documents flip to `reauth_required` with
  `auth_error: invalid_grant` and `auth_failed_at`, and the previously fetched
  fields are never re-served as fresh.
- **No mutation with invalid credentials**: the spike workers are read-only by
  construction; the only Calendar-mutation code path (Section 6 guard)
  requires a working credential to even read the event it verifies ownership
  on.
- **Failure classification** is stable across all observations this spike:
  `RefreshError('invalid_grant: Token has been expired or revoked.')` — the
  string Section 13's production handling should match on.

## Reconnection runbook (recorded)

- Selected mode (In production) has no 7-day token expiry, so no scheduled
  reconnection cadence is required during the build; any revocation or auth
  failure surfaces as `reauth_required` and is repaired with
  `scripts/oauth_spike.py authorize --mode production` followed by
  `gmail_watch_spike.py register` and `calendar_watch_spike.py renew`.
- Mandatory fresh reconnect + watch renewal immediately before golden-run
  testing and again before demo recording (both watches expire every 7 days
  regardless of mode — daily renewal automation arrives in Phase 2).
