# Section 9 evidence — minimal trust contracts

Captured 2026-08-12 against the deployed service.

## OIDC negative matrix — genuinely signed tokens

All four cases used real Google-signed identity tokens (impersonated service
accounts), not synthetic strings:

| Case | Result |
|---|---|
| Pub/Sub route: valid signature, wrong audience | 403 |
| Pub/Sub route: right audience, wrong service identity | 403 |
| Tasks route: valid signature, wrong audience | 403 |
| Tasks route: right audience, wrong service identity | 403 |

Rejections occur before any Firestore write or task dispatch. Positive
acceptance was proven earlier by live traffic (Sections 4–8).

## Controlled-user session

- `/auth/login` issues a one-time transaction (state hashed as document ID,
  nonce hashed, PKCE S256) with 10-minute expiry, then redirects to Google
  requesting only `openid email` — mailbox scopes never appear in login.
- Live positive test: the controlled account completed login and `/app/me`
  returned `{"authenticated": true, "user_id": "user_fixture_controlled_001",
  "session": "server-side"}`.
- **Application allowlist, live negative test**: the admin account completed a
  full Google sign-in in a fresh incognito session and received
  `{"detail": "account not allowed"}` — no session document was created
  (Firestore still holds exactly one session, belonging to the controlled
  identity). With the app In production, Google's test-user gate no longer
  filters accounts, so this rejection is the sole guard — now proven.
- Forged/absent state at the callback → 403/400 with no side effects;
  transactions are consumed single-use inside a Firestore transaction.
- Cookie: opaque random token (server stores only its SHA-256), `HttpOnly`,
  `Secure`, `SameSite=Lax`, path `/`, no `Domain` attribute (host-scoped),
  and contains no OAuth material.
- Same origin, CORS disabled: no `Access-Control-Allow-Origin` header on any
  response.

## Seeded demo

- `GET /demo/today` serves repository-owned seeded JSON derived from
  `golden_scenario_rev_1`; the handler reads a static file and has no access
  path to credentials or live data.
- `POST /demo/*` (representative mutation attempt) → 403 with zero Firestore
  writes, task dispatches, or Google API calls. The full mutation matrix is
  Phase 5 (Part II).

## Consequence for the Section 3 decision

The application allowlist was the last provisional condition on the OAuth
publishing-mode selection. With it proven, **External / In production /
unverified personal use is the final P0 mode.**
