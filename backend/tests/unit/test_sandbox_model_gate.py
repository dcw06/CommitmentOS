from __future__ import annotations

import pytest

from commitmentos.contracts.model_output import CommitmentInterpretationV1
from commitmentos.sandbox.interpreter import (
    BoundedSandboxModelInterpreter,
    SandboxModelBudgetError,
    SandboxModelCallGate,
)
from commitmentos.sandbox.twin import FakeModelInterpreter


async def test_process_gate_counts_extraction_and_explanation_together() -> None:
    live = FakeModelInterpreter()
    live.script(
        CommitmentInterpretationV1(schema_version="extraction_v2", proposals=())
    )
    gate = SandboxModelCallGate(
        max_calls_per_window=1,
        window_seconds=60,
        max_concurrent_calls=1,
    )
    first_session = BoundedSandboxModelInterpreter(live, gate, max_calls=2)
    second_session = BoundedSandboxModelInterpreter(live, gate, max_calls=2)

    await first_session.interpret_commitment("hello", {}, ())

    with pytest.raises(SandboxModelBudgetError, match="rate limit"):
        await second_session.explain_decision({}, ())


async def test_session_gate_counts_every_model_method() -> None:
    live = FakeModelInterpreter()
    live.script(
        CommitmentInterpretationV1(schema_version="extraction_v2", proposals=())
    )
    bounded = BoundedSandboxModelInterpreter(
        live,
        SandboxModelCallGate(max_calls_per_window=10),
        max_calls=1,
    )

    await bounded.interpret_commitment("hello", {}, ())

    with pytest.raises(SandboxModelBudgetError, match="session model budget"):
        await bounded.explain_decision({}, ())
