# Section 7 evidence — Gemini structured output and deployed ADK graph run

Captured 2026-08-12. Both proofs executed against the deployed Cloud Run
service (revision `commitmentos-00008`), invoked with impersonated
`commitmentos-scheduler@` OIDC identity tokens.

## Gemini structured output (deployed call)

| Field | Value |
|---|---|
| Model (reported by API) | `gemini-3.5-flash` |
| Prompt / schema versions | `commitment_interpretation_v1` / `extraction_v1` |
| Thinking level | `low`, applied |
| Input | Golden fixture messages M1+M2 (request + acceptance), T₀ = 2026-08-10 |
| Latency | 1,643 ms deployed (5,713 ms first local call) |
| Tokens | 605 prompt / ~210 output |
| Estimated cost | Well under $0.001 per call at flash-class list prices |
| Disposition | `accepted`, zero deterministic violations |
| Metadata persistence | `model_calls/spike-…` — versions, latency, tokens, disposition only; no source body, no prompt text |

**Golden expectations all matched**: `ownership_type = my_commitment`,
`beneficiary = Professor Chen`, `deadline_value = 2026-08-14T16:00:00-07:00`
(the model resolved "before our Friday 4 p.m. review" against the Monday
message timestamp in `America/Los_Angeles`), evidence quote an exact
substring of M2.

### Validation layering (the finding of this section)

The Gemini `response_schema` proto **rejects `additionalProperties`** — which
pydantic emits for `extra="forbid"` models — so the schema handed to the API
is a sanitized copy (refs inlined, `additionalProperties` stripped) used as
guidance only. The strict pydantic model remains the authoritative validator
over the returned JSON. Rejection tests (all local, all passed): unknown
field, invalid ownership enum, naive datetime, confidence out of range,
effort out of range, and a fabricated evidence quote caught by the
substring-of-source check.

Untrusted-data delimiting: source messages travel inside
`<untrusted_source_messages>` markers with an explicit instructions-are-data
rule; the interpretation output is declared an action-free proposal (prompt
rule 6), and no tool or mutation path exists on the interpretation route.

## Deployed ADK graph run

| Field | Value |
|---|---|
| ADK version | `google-adk 2.6.3` |
| Workflow version | `reconciliation_v1` |
| Graph | `START → load_observation → validate_observation → finalize_run` |
| Durable input | `sync_requests/gmail:…` (the Section 4 observation document) |
| Events emitted | 3 (one per node), deterministic route, no branching |
| Calendar mutations | 0 (no Calendar client exists in the graph path) |
| Durable outcome | `reconciliation_runs/run-spike-620dcd21fbec` |
| Outcome | `observation_acknowledged_no_action`; started/terminated timestamps 84 ms apart |

The outcome document's `route` array records `load_observation` as the first
executed node (the graph's `__START__` sentinel precedes it by construction).

## ADK 2.6.3 API notes for Phase 1

- `Workflow(name=…, edges=[(START, node_a, node_b, …)])` with `@node`-decorated
  functions; runs inside `InMemoryRunner.run_async` with a session.
- With `parameter_binding="node_input"`, parameters bind by **name to keys of
  the previous node's returned dict**; the first node needs a default for its
  parameter (no seed input arrives from START).
- A `Workflow` executed from an async FastAPI route must be awaited, never
  `asyncio.run()`.
