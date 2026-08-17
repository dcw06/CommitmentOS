from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scripts.run_golden_path import GoldenPathRunner


class _SnapshotContext:
    def __init__(self, store: dict[str, dict[str, dict]]) -> None:
        self._store = copy.deepcopy(store)
        self.queries: list[str] = []

    async def query(self, collection: str, _filters: list) -> list[tuple[str, dict]]:
        self.queries.append(collection)
        return list(self._store.get(collection, {}).items())


class _SnapshotUnitOfWork:
    def __init__(self, store: dict[str, dict[str, dict]]) -> None:
        self._store = store
        self.run_calls = 0
        self.contexts: list[_SnapshotContext] = []

    async def run(self, operation):  # noqa: ANN001, ANN202
        self.run_calls += 1
        context = _SnapshotContext(self._store)
        self.contexts.append(context)
        return await operation(SimpleNamespace(_context=context))


async def test_state_digest_is_one_serializable_cross_collection_snapshot() -> None:
    store = {
        "commitments": {
            "commitment-1": {
                "revision": 3,
                "lifecycle_status": "active",
                "effort": {"confirmed_minutes": 90},
            }
        },
        "work_blocks": {},
        "approvals": {},
        "planner_runs": {"plan-1": {"status": "published"}},
        "evidence": {},
        "source_observations": {
            "observation-1": {"reconciliation_status": "processed"}
        },
        "action_outbox": {},
    }
    runner = GoldenPathRunner.__new__(GoldenPathRunner)
    runner.uow = _SnapshotUnitOfWork(store)

    digest = await runner.state_digest()

    expected_view = {
        "commitments": {
            "commitment-1": {
                "revision": 3,
                "lifecycle_status": "active",
                "effort": 90,
            }
        },
        "work_blocks": {},
        "approvals": {},
        "planner_runs": {"plan-1": {"status": "published"}},
        "evidence": {},
        "source_observations": {
            "observation-1": {"status": "processed"}
        },
        "action_outbox": {},
    }
    expected = hashlib.sha256(
        json.dumps(expected_view, sort_keys=True, default=str).encode()
    ).hexdigest()
    assert digest == expected
    assert runner.uow.run_calls == 1
    assert len(runner.uow.contexts[0].queries) == 7


async def test_replay_barrier_waits_for_inflight_work_and_two_stable_snapshots(
    monkeypatch,
) -> None:  # noqa: ANN001
    runner = GoldenPathRunner.__new__(GoldenPathRunner)
    runner._replay_pipeline_busy = AsyncMock(
        side_effect=[("observation:repair:processing",), (), ()]
    )
    runner.state_digest = AsyncMock(side_effect=["pre-commit", "settled", "settled"])
    runner.rescue_stuck_transport = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr("scripts.run_golden_path.asyncio.sleep", sleep)

    digest = await runner.wait_replay_quiescent(
        timeout=1,
        poll_interval=0,
        stable_samples=2,
    )

    assert digest == "settled"
    assert runner.state_digest.await_count == 3
    assert runner.rescue_stuck_transport.await_count == 0
