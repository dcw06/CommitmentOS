# Sandbox deploy evidence

Live security probes recorded after the interactive judge sandbox
(`/sandbox`) was deployed. Kept separate from `docs/phase5_evidence/` so the
phase 5 pack keeps describing only the phase 5 golden campaign.

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
