from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping, NewType, Sequence

UserId = NewType("UserId", str)
CommitmentId = NewType("CommitmentId", str)
WorkBlockId = NewType("WorkBlockId", str)
ObservationId = NewType("ObservationId", str)
ApprovalId = NewType("ApprovalId", str)
OutboxId = NewType("OutboxId", str)
PlannerRunId = NewType("PlannerRunId", str)
ReconciliationRunId = NewType("ReconciliationRunId", str)
TraceId = NewType("TraceId", str)


class CanonicalEncoder:
    """Length-delimited canonical encoding for deterministic document IDs.

    Every part is encoded as `<type-tag><byte-length>:<bytes>` so no
    combination of parts can collide through ad hoc string concatenation.
    """

    @staticmethod
    def encode(parts: Sequence[str | int | datetime | None]) -> bytes:
        pieces: list[bytes] = [b"cenc:v1"]
        for part in parts:
            if part is None:
                pieces.append(b"n0:")
            elif isinstance(part, bool):
                raise TypeError("boolean parts are not canonically encodable")
            elif isinstance(part, int):
                raw = str(part).encode("ascii")
                pieces.append(b"i" + str(len(raw)).encode("ascii") + b":" + raw)
            elif isinstance(part, datetime):
                if part.tzinfo is None:
                    raise ValueError("naive datetimes are not canonically encodable")
                raw = part.astimezone(timezone.utc).isoformat(timespec="microseconds").encode("ascii")
                pieces.append(b"t" + str(len(raw)).encode("ascii") + b":" + raw)
            elif isinstance(part, str):
                raw = part.encode("utf-8")
                pieces.append(b"s" + str(len(raw)).encode("ascii") + b":" + raw)
            else:
                raise TypeError(f"unsupported canonical part type: {type(part)!r}")
        return b"|".join(pieces)

    @staticmethod
    def hash(parts: Sequence[str | int | datetime | None]) -> str:
        return hashlib.sha256(CanonicalEncoder.encode(parts)).hexdigest()


class RevisionSet:
    def __init__(self, revisions: Mapping[str, int]) -> None:
        self._revisions: dict[str, int] = dict(revisions)

    def get(self, key: str) -> int | None:
        return self._revisions.get(key)

    def matches(self, current: Mapping[str, int]) -> bool:
        return all(current.get(key) == expected for key, expected in self._revisions.items())

    def as_mapping(self) -> Mapping[str, int]:
        return dict(self._revisions)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RevisionSet):
            return NotImplemented
        return self._revisions == other._revisions

    def __repr__(self) -> str:
        return f"RevisionSet({self._revisions!r})"


JsonObject = Mapping[str, Any]
