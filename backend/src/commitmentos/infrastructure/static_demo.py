from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


class StaticDemoReadModel:
    """Repository-owned seeded demo data (plan §7.4 route class 4).

    Reads committed JSON derived from `golden_scenario_rev_1`. No Firestore,
    credential, or Google API access path exists in this adapter, so the
    judge-facing demo surface cannot reach live data even if misused.
    """

    def __init__(self, data_directory: Path) -> None:
        self._data_directory = data_directory

    async def get_today(self) -> Mapping[str, Any]:
        return self._load("today.json")

    async def list_commitments(self) -> Sequence[Mapping[str, Any]]:
        return self._load("commitments.json")

    async def list_activity(self) -> Sequence[Mapping[str, Any]]:
        return self._load("activity.json")

    def _load(self, filename: str) -> Any:
        return json.loads((self._data_directory / filename).read_text(encoding="utf-8"))
