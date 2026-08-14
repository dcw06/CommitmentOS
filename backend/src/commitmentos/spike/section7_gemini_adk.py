"""Phase 0 Section 7 spike — Gemini structured output and a deployed ADK graph run.

Two OIDC-protected proof routes:

- POST /internal/spike/interpret — calls the pinned Gemini model with the
  frozen prompt and strict schema over the golden fixture thread, validates
  the output deterministically, and persists sanitized call metadata.
- POST /internal/spike/graph-run — runs a bounded three-node ADK Workflow
  (load_observation first) from a durable Firestore observation and writes a
  typed reconciliation-run outcome document.

The extraction schema lives here as `extraction_v1`; Phase 1 migrates it into
`contracts/model_output.py` alongside the full command stack.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from google import genai
from google.genai import types as genai_types
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from commitmentos.bootstrap.settings import Settings
from commitmentos.spike.section4_gmail import _access_secret, _firestore_client, _verify_delivery

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "gmail_fixture_golden_proposal_revision_001.json"
)
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "commitment_interpretation_v1.md"
FIXTURE_T0 = "2026-08-10"  # Monday; deterministic materialization for the spike


class EvidenceSpanV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    quote: str = Field(min_length=5, max_length=300)


class CommitmentInterpretationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_commitment: bool
    ownership_type: Literal[
        "my_commitment", "request_to_me", "commitment_to_me", "none", "ambiguous"
    ]
    title: str = Field(min_length=3, max_length=200)
    beneficiary_display_name: str | None = None
    deadline_expression: str | None = None
    deadline_value: AwareDatetime | None = None
    deadline_confidence: float = Field(ge=0, le=1)
    ownership_confidence: float = Field(ge=0, le=1)
    proposed_effort_minutes: int | None = Field(default=None, ge=15, le=2400)
    evidence: list[EvidenceSpanV1] = Field(max_length=6)


def load_fixture_messages(until_index: int) -> list[dict]:
    fixture = json.loads(FIXTURE_PATH.read_text())
    tz = ZoneInfo(fixture["timezone"])
    t0 = datetime.fromisoformat(FIXTURE_T0).replace(tzinfo=tz)
    materialized = []
    for message in fixture["messages"][:until_index]:
        hour, minute = message["offset_time"].split(":")
        sent_at = t0 + timedelta(days=message["offset_day"], hours=int(hour), minutes=int(minute))
        persona = fixture["personas"][message["from_persona"]]
        materialized.append(
            {
                "message_id": message["message_id"],
                "direction": message["direction"],
                "from_display_name": persona["display_name"],
                "sent_at": sent_at.isoformat(),
                "subject": message["subject"],
                "body": message["body"],
            }
        )
    return materialized


def render_prompt(messages: list[dict], controlled_display_name: str) -> str:
    lines = [
        PROMPT_PATH.read_text(),
        f"\nThe controlled user is: {controlled_display_name}.",
        "Thread timezone: America/Los_Angeles.",
        "\n<untrusted_source_messages>",
    ]
    for message in messages:
        lines.append(
            f'<message id="{message["message_id"]}" direction="{message["direction"]}" '
            f'from="{message["from_display_name"]}" sent_at="{message["sent_at"]}" '
            f'subject="{message["subject"]}">\n{message["body"]}\n</message>'
        )
    lines.append("</untrusted_source_messages>")
    return "\n".join(lines)


_WHITESPACE = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().lower()


def deterministic_violations(
    interpretation: CommitmentInterpretationV1, messages: list[dict]
) -> list[str]:
    violations: list[str] = []
    bodies = {m["message_id"]: _collapse(m["body"]) for m in messages}
    if interpretation.has_commitment and not interpretation.evidence:
        violations.append("commitment without evidence")
    for span in interpretation.evidence:
        if span.message_id not in bodies:
            violations.append(f"evidence references unknown message {span.message_id}")
        elif _collapse(span.quote) not in bodies[span.message_id]:
            violations.append(f"evidence quote not found in {span.message_id}")
    if interpretation.deadline_value is not None and interpretation.deadline_expression is None:
        violations.append("deadline value without source expression")
    return violations


@lru_cache(maxsize=1)
def _genai_client(api_key_ref: str) -> genai.Client:
    return genai.Client(api_key=_access_secret(api_key_ref))


def _api_response_schema() -> dict:
    # The Gemini response_schema proto rejects additionalProperties and $ref;
    # the API copy is guidance only — the strict pydantic model remains the
    # authoritative validator for everything the model returns.
    schema = CommitmentInterpretationV1.model_json_schema()
    definitions = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(definitions[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "additionalProperties"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def run_interpretation(settings: Settings) -> dict:
    messages = load_fixture_messages(until_index=2)  # M1 request + M2 acceptance
    prompt = render_prompt(messages, "Controlled User")
    client = _genai_client(settings.gemini_api_key_secret_ref)

    config_kwargs = {
        "response_mime_type": "application/json",
        "response_schema": _api_response_schema(),
        "thinking_config": genai_types.ThinkingConfig(
            thinking_level=settings.gemini_thinking_level
        ),
    }
    started = time.monotonic()
    try:
        response = client.models.generate_content(
            model=settings.gemini_model_id,
            contents=prompt,
            config=genai_types.GenerateContentConfig(**config_kwargs),
        )
        thinking_level_applied = True
    except Exception:
        config_kwargs.pop("thinking_config")
        response = client.models.generate_content(
            model=settings.gemini_model_id,
            contents=prompt,
            config=genai_types.GenerateContentConfig(**config_kwargs),
        )
        thinking_level_applied = False
    latency_ms = int((time.monotonic() - started) * 1000)

    disposition = "accepted"
    violations: list[str] = []
    interpretation: CommitmentInterpretationV1 | None = None
    try:
        interpretation = CommitmentInterpretationV1.model_validate_json(response.text)
    except ValidationError as error:
        disposition = "rejected_schema"
        violations = [str(e["type"]) + ":" + ".".join(str(p) for p in e["loc"]) for e in error.errors()]
    if interpretation is not None:
        violations = deterministic_violations(interpretation, messages)
        if violations:
            disposition = "rejected_deterministic"

    usage = response.usage_metadata
    expected_deadline = "2026-08-14T16:00"
    actual_deadline = (
        interpretation.deadline_value.isoformat()[:16] if interpretation and interpretation.deadline_value else None
    )
    evidence_record = {
        "model_version_reported": getattr(response, "model_version", None),
        "prompt_version": settings.prompt_version,
        "schema_version": settings.extraction_schema_version,
        "thinking_level": settings.gemini_thinking_level,
        "thinking_level_applied": thinking_level_applied,
        "latency_ms": latency_ms,
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
        "disposition": disposition,
        "violation_count": len(violations),
        "golden_expectations": {
            "ownership_is_my_commitment": bool(
                interpretation and interpretation.ownership_type == "my_commitment"
            ),
            "deadline_matches_friday_16": actual_deadline == expected_deadline,
            "has_commitment": bool(interpretation and interpretation.has_commitment),
        },
        "called_at": datetime.now(timezone.utc).isoformat(),
    }
    _firestore_client(settings.google_cloud_project).collection("model_calls").document(
        f"spike-{uuid.uuid4().hex[:12]}"
    ).set(evidence_record)

    return {
        **evidence_record,
        "violations": violations,
        "interpretation": json.loads(interpretation.model_dump_json()) if interpretation else None,
    }


async def run_graph(settings: Settings) -> dict:
    from google.adk.runners import InMemoryRunner
    from google.adk.workflow import START, Workflow, node

    project = settings.google_cloud_project
    run_id = f"run-spike-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc)

    @node(name="load_observation", parameter_binding="node_input")
    def load_observation(node_input=None):
        snapshot = (
            _firestore_client(project)
            .collection("sync_requests")
            .document(f"gmail:{settings.controlled_user_id}")
            .get()
        )
        if not snapshot.exists:
            raise ValueError("no durable observation available")
        data = snapshot.to_dict()
        return {
            "observation": {
                "source": data.get("source"),
                "user_id": data.get("user_id"),
                "latest_history_id": data.get("latest_history_id"),
                "status": data.get("status"),
            }
        }

    @node(name="validate_observation", parameter_binding="node_input")
    def validate_observation(observation):
        valid = observation["source"] == "gmail" and observation["user_id"] == settings.controlled_user_id
        return {"observation": observation, "valid": valid}

    @node(name="finalize_run", parameter_binding="node_input")
    def finalize_run(observation, valid):
        outcome = {
            "run_id": run_id,
            "workflow_version": settings.workflow_version,
            "observation_source": observation["source"],
            "observation_user": observation["user_id"],
            "route": ["load_observation", "validate_observation", "finalize_run"],
            "outcome": "observation_acknowledged_no_action" if valid else "observation_rejected",
            "calendar_mutations": 0,
            "started_at": started_at,
            "terminated_at": datetime.now(timezone.utc),
        }
        _firestore_client(project).collection("reconciliation_runs").document(run_id).set(outcome)
        return {"outcome": outcome["outcome"], "run_id": run_id}

    workflow = Workflow(
        name="spike_reconciliation",
        edges=[(START, load_observation, validate_observation, finalize_run)],
    )
    first_node = workflow.graph.nodes[0].name if workflow.graph else "?"

    async def execute() -> tuple[int, dict | None]:
        runner = InMemoryRunner(agent=workflow, app_name="commitmentos-spike")
        session = await runner.session_service.create_session(
            app_name="commitmentos-spike", user_id=settings.controlled_user_id
        )
        event_count = 0
        final_output: dict | None = None
        async for event in runner.run_async(
            user_id=settings.controlled_user_id,
            session_id=session.id,
            new_message=genai_types.Content(role="user", parts=[genai_types.Part(text="run")]),
        ):
            event_count += 1
            output = getattr(event, "output", None)
            if isinstance(output, dict) and "run_id" in output:
                final_output = output
        return event_count, final_output

    event_count, final_output = await execute()
    return {
        "adk_version": __import__("google.adk.version", fromlist=["__version__"]).__version__,
        "workflow_version": settings.workflow_version,
        "first_registered_node": first_node,
        "event_count": event_count,
        "final_output": final_output,
        "durable_outcome_document": f"reconciliation_runs/{run_id}",
        "terminated": True,
    }


def build_section7_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post("/internal/spike/interpret")
    async def interpret(request: Request) -> dict:
        _verify_delivery(
            request, settings.scheduler_oidc_audience, settings.scheduler_service_account
        )
        return run_interpretation(settings)

    @router.post("/internal/spike/graph-run")
    async def graph_run(request: Request) -> dict:
        _verify_delivery(
            request, settings.scheduler_oidc_audience, settings.scheduler_service_account
        )
        return await run_graph(settings)

    return router
