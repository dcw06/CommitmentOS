"""Phase 0 Section 6 — stable Calendar identity, If-Match mutation, forced 412.

Runs the complete provider-behavior sequence against the controlled calendar
and self-verifies each step:

    python scripts/calendar_identity_spike.py run
    python scripts/calendar_identity_spike.py cleanup   # preview + delete spike events

Provider behavior only; the outbox executor that will own this path in
production is Phase 1. Nothing here mutates events lacking CommitmentOS
ownership properties.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as build_google_api
from googleapiclient.errors import HttpError

from commitmentos.bootstrap.settings import Settings

WORK_BLOCK_A = "block_spike_identity_001"
WORK_BLOCK_B = "block_spike_identity_002"
COMMITMENT_ID = "commitment_spike_identity_001"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def _secret(ref: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    return sm.access_secret_version(name=ref).payload.data.decode("utf-8")


def _service(settings: Settings):
    client_config = json.loads(_secret(settings.oauth_client_secret_ref))["web"]
    credentials = Credentials(
        token=None,
        refresh_token=_secret(settings.controlled_refresh_token_secret_ref),
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
    )
    return build_google_api("calendar", "v3", credentials=credentials, cache_discovery=False)


def derive_event_id(settings: Settings, work_block_id: str) -> str:
    material = (
        f"commitmentos:{settings.calendar_event_id_algorithm_version}"
        f"{settings.calendar_id}{work_block_id}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")


def _ownership(work_block_id: str, plan_revision: str) -> dict:
    return {
        "managed_by": "commitmentos",
        "commitment_id": COMMITMENT_ID,
        "work_block_id": work_block_id,
        "plan_revision": plan_revision,
    }


def _event_body(work_block_id: str, start: datetime, plan_revision: str, summary: str) -> dict:
    return {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        "extendedProperties": {"private": _ownership(work_block_id, plan_revision)},
    }


def _owned(event: dict, work_block_id: str) -> bool:
    properties = event.get("extendedProperties", {}).get("private", {})
    return (
        properties.get("managed_by") == "commitmentos"
        and properties.get("work_block_id") == work_block_id
    )


def guarded_patch(service, settings: Settings, event_id: str, work_block_id: str, etag: str, body: dict):
    event = service.events().get(calendarId=settings.calendar_id, eventId=event_id).execute()
    if not _owned(event, work_block_id):
        return "refused_not_owned"
    request = service.events().patch(
        calendarId=settings.calendar_id, eventId=event_id, body=body
    )
    request.headers["If-Match"] = etag
    return request.execute()


def guarded_delete(service, settings: Settings, event_id: str, work_block_id: str, etag: str):
    event = service.events().get(calendarId=settings.calendar_id, eventId=event_id).execute()
    if not _owned(event, work_block_id):
        return "refused_not_owned"
    request = service.events().delete(calendarId=settings.calendar_id, eventId=event_id)
    request.headers["If-Match"] = etag
    return request.execute() or "deleted"


def cmd_run(settings: Settings) -> int:
    service = _service(settings)
    start = datetime.now(timezone.utc) + timedelta(days=2)

    event_id_a = derive_event_id(settings, WORK_BLOCK_A)
    event_id_b = derive_event_id(settings, WORK_BLOCK_B)
    print(f"derived id A ({WORK_BLOCK_A}): {event_id_a}")
    print(f"derived id B ({WORK_BLOCK_B}): {event_id_b}")
    check(
        "derived IDs are base32hex-safe and deterministic",
        event_id_a == derive_event_id(settings, WORK_BLOCK_A)
        and set(event_id_a) <= set("0123456789abcdefghijklmnopqrstuv"),
    )

    body_a = _event_body(WORK_BLOCK_A, start, "1", "CommitmentOS spike identity block A")
    created = service.events().insert(
        calendarId=settings.calendar_id, body={**body_a, "id": event_id_a}
    ).execute()
    check("insert with client-supplied stable ID", created["id"] == event_id_a)

    try:
        service.events().insert(
            calendarId=settings.calendar_id, body={**body_a, "id": event_id_a}
        ).execute()
        retry_status = "unexpected-success"
    except HttpError as error:
        retry_status = error.resp.status
    adopted = service.events().get(calendarId=settings.calendar_id, eventId=event_id_a).execute()
    check(
        "insert retry rejected by provider; adoption lookup finds the owned event",
        retry_status == 409 and _owned(adopted, WORK_BLOCK_A),
        f"retry status {retry_status}",
    )
    matches = (
        service.events()
        .list(
            calendarId=settings.calendar_id,
            privateExtendedProperty=f"work_block_id={WORK_BLOCK_A}",
        )
        .execute()
        .get("items", [])
    )
    check("no second event exists for the work block", len(matches) == 1, f"{len(matches)} events")

    # Event B carries deliberately mismatched ownership: adoption for B must refuse.
    service.events().insert(
        calendarId=settings.calendar_id,
        body={
            **_event_body("block_spike_identity_999", start + timedelta(hours=2), "1",
                          "CommitmentOS spike corrupted-ownership event"),
            "id": event_id_b,
        },
    ).execute()
    candidate = service.events().get(calendarId=settings.calendar_id, eventId=event_id_b).execute()
    check("adoption refused when ownership properties mismatch", not _owned(candidate, WORK_BLOCK_B))

    fresh = service.events().get(calendarId=settings.calendar_id, eventId=event_id_a).execute()
    etag_1 = fresh["etag"]
    moved = guarded_patch(
        service, settings, event_id_a, WORK_BLOCK_A, etag_1,
        {
            "start": {"dateTime": (start + timedelta(hours=3)).isoformat()},
            "end": {"dateTime": (start + timedelta(hours=4)).isoformat()},
            "extendedProperties": {"private": _ownership(WORK_BLOCK_A, "2")},
        },
    )
    check(
        "conditional patch with If-Match succeeded; event ID stable across plan revision",
        isinstance(moved, dict) and moved["id"] == event_id_a and moved["etag"] != etag_1,
    )

    # Forced 412: record an etag, let an intervening edit change the event, replay stale intent.
    stale_etag = moved["etag"]
    intervening = service.events().patch(
        calendarId=settings.calendar_id, eventId=event_id_a,
        body={"summary": "CommitmentOS spike identity block A (user-edited)"},
    ).execute()
    try:
        guarded_patch(
            service, settings, event_id_a, WORK_BLOCK_A, stale_etag,
            {"summary": "stale overwrite attempt"},
        )
        check("stale If-Match patch returns 412", False, "patch unexpectedly succeeded")
        response_shape = None
    except HttpError as error:
        response_shape = {
            "status": error.resp.status,
            "reason": getattr(error, "reason", ""),
            "body_excerpt": error.content.decode("utf-8", "replace")[:400],
        }
        check("stale If-Match patch returns 412", error.resp.status == 412)
    after = service.events().get(calendarId=settings.calendar_id, eventId=event_id_a).execute()
    check(
        "no overwrite and no blind retry occurred after 412",
        after["summary"] == intervening["summary"] and after["etag"] == intervening["etag"],
    )
    if response_shape:
        print("412 response shape for the Phase 1/4 outbox state machine:")
        print(json.dumps(response_shape, indent=2))

    unrelated = service.events().insert(
        calendarId=settings.calendar_id,
        body={
            "summary": "Unrelated user event (spike guard test)",
            "start": {"dateTime": (start + timedelta(hours=5)).isoformat()},
            "end": {"dateTime": (start + timedelta(hours=6)).isoformat()},
        },
    ).execute()
    guard_result = guarded_patch(
        service, settings, unrelated["id"], WORK_BLOCK_A, unrelated["etag"],
        {"summary": "should never happen"},
    )
    untouched = service.events().get(calendarId=settings.calendar_id, eventId=unrelated["id"]).execute()
    check(
        "guard refuses to patch a non-owned event; event unchanged",
        guard_result == "refused_not_owned" and untouched["summary"] == unrelated["summary"],
    )

    current = service.events().get(calendarId=settings.calendar_id, eventId=event_id_b).execute()
    delete_result = guarded_delete(
        service, settings, event_id_b, "block_spike_identity_999", current["etag"]
    )
    check("conditional cancellation with If-Match succeeded", delete_result == "deleted")

    # Test-artifact removal (not the product mutation path): unrelated event.
    service.events().delete(calendarId=settings.calendar_id, eventId=unrelated["id"]).execute()

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURES: {_failures}")
        return 1
    print("RESULT: all Section 6 provider-behavior checks passed")
    print(f"remaining spike event for cleanup: {event_id_a}")
    return 0


def cmd_cleanup(settings: Settings) -> int:
    service = _service(settings)
    items = (
        service.events()
        .list(
            calendarId=settings.calendar_id,
            privateExtendedProperty="managed_by=commitmentos",
            maxResults=50,
        )
        .execute()
        .get("items", [])
    )
    print(f"cleanup preview: {len(items)} owned event(s)")
    for event in items:
        properties = event.get("extendedProperties", {}).get("private", {})
        print(f"  {event['id']} | {event.get('summary','')} | {properties.get('work_block_id')}")
    for event in items:
        if event.get("extendedProperties", {}).get("private", {}).get("managed_by") != "commitmentos":
            print(f"  skipped (not owned): {event['id']}")
            continue
        service.events().delete(calendarId=settings.calendar_id, eventId=event["id"]).execute()
        print(f"  deleted: {event['id']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("cleanup")
    args = parser.parse_args()
    settings = Settings.load()
    return cmd_run(settings) if args.command == "run" else cmd_cleanup(settings)


if __name__ == "__main__":
    raise SystemExit(main())
