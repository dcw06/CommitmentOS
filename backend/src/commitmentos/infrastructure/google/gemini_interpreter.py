from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from commitmentos.application.ports.model_interpreter import (
    InterpretationResult,
    ModelInvocationMetadata,
    ModelOutputParseError,
)
from commitmentos.contracts.model_output import (
    gemini_response_schema,
    parse_interpretation_wire,
    wire_to_contract,
)

PROMPTS_DIRECTORY = Path(__file__).resolve().parents[2] / "prompts"


class ExplanationWireV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(min_length=1, max_length=600)


class GeminiInterpreter:
    """Structured commitment extraction through the pinned Gemini model.

    The prompt delimits all source text as untrusted data, the request grants
    no tools, and the response passes through the strict `extraction_v2`
    pydantic contract before anything downstream sees it (plan §13.2). The
    adapter returns typed proposals plus sanitized invocation metadata; it
    never persists source text.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        model_id: str,
        prompt_version: str,
        schema_version: str,
        thinking_level: str,
        controlled_display_name: str,
    ) -> None:
        self._client_factory = client_factory
        self._client: Any = None
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._schema_version = schema_version
        self._thinking_level = thinking_level
        self._controlled_display_name = controlled_display_name
        self._prompt_template = (
            PROMPTS_DIRECTORY / f"{prompt_version}.md"
        ).read_text(encoding="utf-8")

    async def interpret_commitment(
        self,
        source_text: str,
        source_metadata: Mapping[str, str],
        candidate_commitments: Sequence[Mapping[str, str]],
    ) -> InterpretationResult:
        prompt = self._render_prompt(source_text, source_metadata, candidate_commitments)
        response, latency_ms, thinking_applied = await asyncio.to_thread(
            self._generate, prompt, gemini_response_schema()
        )
        wire, error_codes = parse_interpretation_wire(response.text or "")
        if wire is None:
            raise ModelOutputParseError(error_codes)
        interpretation = wire_to_contract(wire)
        return InterpretationResult(
            interpretation=interpretation,
            metadata=self._invocation_metadata(response, latency_ms, thinking_applied),
        )

    async def explain_decision(
        self,
        decision: Mapping[str, object],
        evidence: Sequence[Mapping[str, str]],
    ) -> tuple[str, ModelInvocationMetadata]:
        template = (PROMPTS_DIRECTORY / "explanation_v1.md").read_text(
            encoding="utf-8"
        )
        prompt = "\n".join(
            (
                template,
                "<trusted_decision>",
                json.dumps(decision, sort_keys=True, default=str),
                "</trusted_decision>",
                "<trusted_evidence>",
                json.dumps(list(evidence), sort_keys=True, default=str),
                "</trusted_evidence>",
            )
        )
        response, latency_ms, thinking_applied = await asyncio.to_thread(
            self._generate,
            prompt,
            {
                "type": "object",
                "properties": {
                    "explanation": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 600,
                    }
                },
                "required": ["explanation"],
            },
        )
        try:
            wire = ExplanationWireV1.model_validate_json(response.text or "")
        except ValidationError as error:
            raise ModelOutputParseError(("explanation_schema_rejected",)) from error
        return (
            wire.explanation,
            self._invocation_metadata(
                response,
                latency_ms,
                thinking_applied,
                prompt_version="explanation_v1",
                schema_version="explanation_v1",
            ),
        )

    # ------------------------------------------------------------------

    def _generate(
        self,
        prompt: str,
        response_schema: Mapping[str, Any],
    ) -> tuple[Any, int, bool]:
        from google.genai import types as genai_types

        if self._client is None:
            self._client = self._client_factory()
        config_kwargs: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_schema": response_schema,
            "thinking_config": genai_types.ThinkingConfig(
                thinking_level=self._thinking_level
            ),
        }
        started = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=self._model_id,
                contents=prompt,
                config=genai_types.GenerateContentConfig(**config_kwargs),
            )
            thinking_applied = True
        except Exception:
            # Phase 0 §7 fallback: retry once without the thinking config in
            # case the deployed API version rejects it.
            config_kwargs.pop("thinking_config")
            response = self._client.models.generate_content(
                model=self._model_id,
                contents=prompt,
                config=genai_types.GenerateContentConfig(**config_kwargs),
            )
            thinking_applied = False
        latency_ms = int((time.monotonic() - started) * 1000)
        return response, latency_ms, thinking_applied

    def _render_prompt(
        self,
        source_text: str,
        source_metadata: Mapping[str, str],
        candidate_commitments: Sequence[Mapping[str, str]],
    ) -> str:
        lines = [
            self._prompt_template,
            f"\nThe controlled user is: {self._controlled_display_name}.",
            f"Thread timezone: {source_metadata.get('timezone', 'UTC')}.",
            "Configured working-day end for date-only deadlines: 17:30.",
            "\n<candidate_commitments>",
        ]
        if candidate_commitments:
            for candidate in candidate_commitments:
                lines.append(
                    f'<candidate commitment_id="{candidate.get("commitment_id", "")}" '
                    f'ownership_type="{candidate.get("ownership_type", "")}" '
                    f'lifecycle_status="{candidate.get("lifecycle_status", "")}" '
                    f'deadline="{candidate.get("deadline", "")}">'
                    f'{candidate.get("normalized_outcome", "")}</candidate>'
                )
        else:
            lines.append("(none)")
        lines.append("</candidate_commitments>")
        lines.append(self._delimit_untrusted_source(source_text))
        return "\n".join(lines)

    @staticmethod
    def _delimit_untrusted_source(source_text: str) -> str:
        return f"\n<untrusted_source_messages>\n{source_text}\n</untrusted_source_messages>"

    def _invocation_metadata(
        self,
        response: Any,
        latency_ms: int,
        thinking_applied: bool,
        prompt_version: str | None = None,
        schema_version: str | None = None,
    ) -> ModelInvocationMetadata:
        usage = getattr(response, "usage_metadata", None)
        return ModelInvocationMetadata(
            model_id=getattr(response, "model_version", None) or self._model_id,
            prompt_version=prompt_version or self._prompt_version,
            schema_version=schema_version or self._schema_version,
            thinking_level=self._thinking_level if thinking_applied else "unset",
            latency_ms=latency_ms,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        )


def render_thread_as_untrusted_source(messages: Sequence[Mapping[str, str]]) -> str:
    """Render fetched thread messages in the delimited data format the prompt
    contract expects. Bodies stay in invocation memory only and cannot smuggle
    delimiter tags."""
    from commitmentos.contracts.model_output import neutralize_untrusted_delimiters

    rendered = []
    for message in messages:
        subject = neutralize_untrusted_delimiters(message.get("subject", ""))
        body = neutralize_untrusted_delimiters(message.get("body", ""))
        rendered.append(
            f'<message id="{message["message_id"]}" direction="{message.get("direction", "")}" '
            f'from="{neutralize_untrusted_delimiters(message.get("from_display_name", ""))}" '
            f'sent_at="{message.get("sent_at", "")}" '
            f'subject="{subject}">\n'
            f"{body}\n</message>"
        )
    return "\n".join(rendered)
