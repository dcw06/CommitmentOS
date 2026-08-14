"""Phase 2 §16.1 extraction evaluation over the labeled fixture pack.

Runs the pinned Gemini model with the production prompt, wire schema, and
deterministic validator against every case in
`backend/tests/fixtures/extraction_eval_fixtures_v1.json`, then reports:

- schema-valid output rate
- ownership accuracy
- deadline accuracy (minute precision, per-case tolerance honored)
- false-positive candidate rate
- identity-operation accuracy
- injection containment (forbidden strings never appear in accepted output)
- latency and token-derived cost per processed message

Usage (repo root; uses .env credentials, calls the real Gemini API):

    python scripts/run_extraction_eval.py            # full pack (~31 calls)
    python scripts/run_extraction_eval.py --limit 5  # smoke subset
    python scripts/run_extraction_eval.py --category prompt_injection

Results are printed and written to docs/phase2_evidence/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from google.cloud import secretmanager  # noqa: E402

from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.contracts.model_output import (  # noqa: E402
    ModelOutputValidator,
    ModelValidationDisposition,
)
from commitmentos.infrastructure.google.gemini_interpreter import (  # noqa: E402
    GeminiInterpreter,
)

FIXTURES = ROOT / "backend" / "tests" / "fixtures" / "extraction_eval_fixtures_v1.json"
EVIDENCE_DIR = ROOT / "docs" / "phase2_evidence"

# gemini-3.5-flash list prices per 1M tokens (verify against current pricing
# when re-running; used only for the reported cost estimate).
PRICE_INPUT_PER_M = 0.30
PRICE_OUTPUT_PER_M = 2.50


def materialize_messages(pack: dict, case: dict) -> list[dict]:
    tz = ZoneInfo(case.get("timezone", pack["timezone"]))
    t0 = datetime.fromisoformat(pack["t0"]).replace(tzinfo=tz)
    rendered = []
    for message in case["messages"]:
        hour, minute = (int(part) for part in message["offset_time"].split(":"))
        sent_at = t0 + timedelta(days=message["offset_day"], hours=hour, minutes=minute)
        rendered.append(
            {
                "message_id": message["message_id"],
                "direction": message["direction"],
                "from_display_name": message["from_display_name"],
                "sent_at": sent_at.isoformat(),
                "subject": message["subject"],
                "body": message["body"],
            }
        )
    return rendered


def render_source(messages: list[dict]) -> str:
    from commitmentos.infrastructure.google.gemini_interpreter import (
        render_thread_as_untrusted_source,
    )

    return render_thread_as_untrusted_source(messages)


def minute(value: str | None) -> str | None:
    return value[:16] if value else None


def within_tolerance(actual: str | None, expected: str | None, tolerance_minutes: int) -> bool:
    if expected is None:
        return actual is None
    if actual is None:
        return False
    actual_dt = datetime.fromisoformat(actual)
    expected_dt = datetime.fromisoformat(expected)
    return abs((actual_dt - expected_dt).total_seconds()) <= tolerance_minutes * 60


def score_case(case: dict, accepted: list, all_proposals: list, schema_valid: bool) -> dict:
    expected = case["expected"]
    result: dict = {
        "case_id": case["case_id"],
        "category": case["category"],
        "schema_valid": schema_valid,
        "accepted_proposals": len(accepted),
    }

    if expected.get("resolver_suppresses"):
        # Suppression of a dismissed span is the deterministic resolver's
        # guarantee, not the model's: whatever the model re-proposes here is
        # ignored or routed to confirmation downstream (proven in
        # test_phase2_interpretation). Excluded from model-accuracy rates.
        result["resolver_suppression_covered"] = True
        return result

    if not expected.get("has_commitment", False):
        # A false positive is an *accepted* commitment-shaped proposal where
        # none should exist; confirmation-routed or tolerated ownerships are
        # not counted against the model.
        allow = set(expected.get("allow_ownership", []))
        false_positives = [
            p for p in accepted if p.proposal.ownership_type.value not in allow
        ]
        result["false_positive"] = bool(false_positives)
        result["ownership_correct"] = not false_positives
        result["deadline_correct"] = not false_positives
        result["identity_correct"] = not false_positives
    else:
        top = accepted[0].proposal if accepted else (
            all_proposals[0].proposal if all_proposals else None
        )
        result["found_commitment"] = top is not None
        if expected.get("proposal_count"):
            result["proposal_count_correct"] = (
                len(all_proposals) == expected["proposal_count"]
            )
        if top is None:
            result["ownership_correct"] = False
            result["deadline_correct"] = False
            result["identity_correct"] = False
        else:
            if "ownership_types" in expected:
                got = sorted(p.proposal.ownership_type.value for p in all_proposals)
                result["ownership_correct"] = got == sorted(expected["ownership_types"])
            else:
                result["ownership_correct"] = (
                    top.ownership_type.value == expected["ownership_type"]
                )
            tolerance = int(expected.get("deadline_tolerance_minutes", 1))
            actual_deadline = (
                top.deadline.proposed_value if top.deadline is not None else None
            )
            if expected.get("low_deadline_confidence_expected"):
                # The safe behaviors all count: no deadline proposed, low
                # confidence, or the whole proposal routed to confirmation
                # (accepted empty) — planning cannot start either way (§9.7).
                result["deadline_correct"] = (
                    not accepted
                    or top.deadline is None
                    or top.deadline.confidence < 0.6
                )
            elif expected.get("deadline_optional") and actual_deadline is None:
                result["deadline_correct"] = True
            elif "deadline_values" in expected:
                actuals = [
                    p.proposal.deadline.proposed_value
                    for p in all_proposals
                    if p.proposal.deadline is not None
                ]
                result["deadline_correct"] = all(
                    any(within_tolerance(a, e, tolerance) for a in actuals)
                    for e in expected["deadline_values"]
                )
            else:
                result["deadline_correct"] = within_tolerance(
                    actual_deadline, expected.get("deadline_value"), tolerance
                )
            operations = {p.proposal.proposed_identity_operation.value for p in all_proposals}
            if "identity_operation_any_of" in expected:
                allowed = {op for op in expected["identity_operation_any_of"] if op}
                result["identity_correct"] = bool(operations & allowed) or not operations
            else:
                result["identity_correct"] = expected.get("identity_operation") in operations
            if expected.get("target_commitment_id"):
                result["identity_correct"] = result["identity_correct"] and any(
                    p.proposal.target_commitment_id == expected["target_commitment_id"]
                    for p in all_proposals
                )
            if expected.get("must_not_target"):
                result["identity_correct"] = result["identity_correct"] and all(
                    p.proposal.target_commitment_id != expected["must_not_target"]
                    for p in all_proposals
                )

    forbidden = case["expected"].get("injection_must_not_leak", [])
    if forbidden:
        leaked = [
            phrase
            for phrase in forbidden
            for p in accepted
            if phrase.lower() in json.dumps(
                {
                    "outcome": p.proposal.normalized_outcome,
                    "description": p.proposal.description,
                }
            ).lower()
        ]
        result["injection_contained"] = not leaked
    return result


async def run(limit: int | None, category: str | None) -> int:
    settings = Settings.load()
    pack = json.loads(FIXTURES.read_text())
    cases = pack["cases"]
    if category:
        cases = [case for case in cases if case["category"] == category]
    if limit:
        cases = cases[:limit]

    def client_factory():
        from google import genai

        api_key = (
            secretmanager.SecretManagerServiceClient()
            .access_secret_version(name=settings.gemini_api_key_secret_ref)
            .payload.data.decode("utf-8")
        )
        return genai.Client(api_key=api_key)

    interpreter = GeminiInterpreter(
        client_factory=client_factory,
        model_id=settings.gemini_model_id,
        prompt_version=settings.prompt_version,
        schema_version=settings.extraction_schema_version,
        thinking_level=settings.gemini_thinking_level,
        controlled_display_name=pack["controlled_display_name"],
    )
    validator = ModelOutputValidator()

    rows = []
    latencies: list[int] = []
    costs: list[float] = []
    for case in cases:
        messages = materialize_messages(pack, case)
        source = render_source(messages)
        bodies = {m["message_id"]: m["body"] for m in messages}
        candidate_ids = frozenset(
            c["commitment_id"] for c in case.get("candidates", [])
        )
        try:
            outcome = await interpreter.interpret_commitment(
                source,
                {"timezone": case.get("timezone", pack["timezone"])},
                case.get("candidates", []),
            )
            schema_valid = True
        except Exception as error:  # noqa: BLE001 - scored, not fatal
            rows.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "schema_valid": False,
                    "error": str(error)[:200],
                }
            )
            continue
        validation = validator.validate(
            outcome.interpretation, bodies, candidate_ids
        )
        accepted = [
            p
            for p in validation.proposals
            if p.disposition == ModelValidationDisposition.ACCEPTED
        ]
        row = score_case(case, accepted, list(validation.proposals), schema_valid)
        row["latency_ms"] = outcome.metadata.latency_ms
        cost = (
            outcome.metadata.input_tokens / 1e6 * PRICE_INPUT_PER_M
            + outcome.metadata.output_tokens / 1e6 * PRICE_OUTPUT_PER_M
        )
        row["cost_usd"] = round(cost, 6)
        latencies.append(outcome.metadata.latency_ms)
        costs.append(cost)
        rows.append(row)
        print(
            f"{case['case_id']:<36} schema={row.get('schema_valid')} "
            f"own={row.get('ownership_correct')} dl={row.get('deadline_correct')} "
            f"id={row.get('identity_correct')} {row.get('latency_ms', '-')}ms"
        )

    def rate(key: str) -> float:
        scored = [row for row in rows if key in row]
        return sum(1 for row in scored if row[key]) / len(scored) if scored else 0.0

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "model_id": settings.gemini_model_id,
        "prompt_version": settings.prompt_version,
        "schema_version": settings.extraction_schema_version,
        "cases_run": len(rows),
        "schema_valid_rate": rate("schema_valid"),
        "ownership_accuracy": rate("ownership_correct"),
        "deadline_accuracy": rate("deadline_correct"),
        "identity_operation_accuracy": rate("identity_correct"),
        "false_positive_rate": (
            sum(1 for row in rows if row.get("false_positive"))
            / max(1, sum(1 for row in rows if "false_positive" in row))
        ),
        "injection_containment_rate": rate("injection_contained"),
        "mean_latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
        "mean_cost_usd_per_message": (
            round(sum(costs) / len(costs), 6) if costs else None
        ),
        "target": "ownership and deadline accuracy >= 0.90 on the curated set",
    }
    print("\n=== extraction eval summary ===")
    for key, value in summary.items():
        print(f"{key}: {value}")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    output = EVIDENCE_DIR / f"extraction_eval_{stamp}.json"
    output.write_text(json.dumps({"summary": summary, "cases": rows}, indent=2))
    print(f"\nwritten: {output.relative_to(ROOT)}")
    passed = (
        summary["ownership_accuracy"] >= 0.9 and summary["deadline_accuracy"] >= 0.9
    )
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--category", default=None)
    arguments = parser.parse_args()
    return asyncio.run(run(arguments.limit, arguments.category))


if __name__ == "__main__":
    raise SystemExit(main())
