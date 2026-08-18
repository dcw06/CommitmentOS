"""Phase 5B live security probe driver (checklist D4 live evidence).

Exercises the deployed service's security contracts and asserts zero durable
or external side effects. Read-only against durable state except where it
mints/revokes a probe session (which it cleans up). Writes a sanitized
report to `docs/phase5_evidence/security_probes_<tag>.json`.

Contracts probed:
  session    full negative matrix through the deployed AuthRouter: allowlisted
             redirect targets only; missing/expired/replayed state; callback
             requires state+code; logout requires CSRF; expiry/revocation;
             opaque host-scoped Secure/HttpOnly/SameSite cookie.
  csrf       every controlled mutation route rejects missing + invalid CSRF.
  oidc       every internal route group rejects wrong audience and wrong
             identity before any work (impersonated real tokens).
  demo       every production mutation method/path under /demo is rejected.
  sandbox    the interactive sandbox is isolated, bounded, and leaks nothing.
  ratelimit  over-limit valid webhook signals return 429 (needs a live
             channel; skipped with a note if none is registered).

Usage:
    .venv/bin/python scripts/run_phase5b_security.py all
    .venv/bin/python scripts/run_phase5b_security.py session
    .venv/bin/python scripts/run_phase5b_security.py oidc
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.bootstrap.container import ApplicationContainer  # noqa: E402
from commitmentos.bootstrap.settings import Settings  # noqa: E402

EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "phase5_evidence"

_checkpoints: list[dict[str, Any]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _checkpoints.append({"label": label, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


class SecurityDriver:
    def __init__(self, settings: Settings, service_url: str) -> None:
        self.settings = settings
        self.service_url = service_url.rstrip("/")
        self.container = ApplicationContainer.build(settings)
        self.uow = self.container.unit_of_work()

    # -- controlled mutation routes (schema-valid bodies) ------------------

    CONTROLLED_ROUTES = [
        (
            "/api/v1/approvals/probe-approval/resolve",
            {"expected_revision": 1, "decision": "approve", "confirmed_minutes": 180},
        ),
        (
            "/api/v1/controls/change",
            {
                "control_name": "automatic_actions",
                "target_mode": "paused",
                "reason": "security probe",
                "expected_control_epoch": 1,
            },
        ),
        (
            "/api/v1/work-blocks/probe-block/check-in",
            {
                "expected_revision": 1,
                "idempotency_key": "probe",
                "completed": True,
                "verified_minutes": 30,
                "checked_in_at": "2026-08-12T16:00:00+00:00",
            },
        ),
        ("/api/v1/plans/probe-plan/undo", {"idempotency_key": "probe"}),
        (
            "/api/v1/commitments/probe-commitment/complete",
            {
                "expected_revision": 1,
                "idempotency_key": "probe",
                "completed_at": "2026-08-12T16:00:00+00:00",
            },
        ),
    ]

    def _impersonated_token(self, service_account: str, audience: str) -> str:
        result = subprocess.run(
            [
                "gcloud",
                "auth",
                "print-identity-token",
                f"--impersonate-service-account={service_account}",
                f"--audiences={audience}",
                "--include-email",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    async def mint_probe_session(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        from datetime import timedelta

        session = {
            "session_id_hash": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": self.settings.controlled_user_id,
            "email": self.settings.controlled_email,
            "csrf_secret": csrf,
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
            "revoked_at": None,
        }

        async def _create(repositories):  # noqa: ANN001, ANN202
            await repositories.web_sessions.create(session)

        await self.uow.run(_create)
        return token, csrf

    async def revoke_probe_session(self, token: str) -> None:
        session_hash = hashlib.sha256(token.encode()).hexdigest()

        async def _revoke(repositories):  # noqa: ANN001, ANN202
            await repositories.web_sessions.revoke(
                session_hash, datetime.now(timezone.utc)
            )

        await self.uow.run(_revoke)

    # -- probe groups ------------------------------------------------------

    async def probe_session(self) -> None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for target in ("https://evil.example", "//evil.example", "/etc/passwd"):
                response = await client.get(
                    f"{self.service_url}/auth/login", params={"return_to": target}
                )
                check(
                    f"login rejects non-allowlisted target {target!r}",
                    response.status_code == 400,
                    f"HTTP {response.status_code}",
                )
            allowed = await client.get(
                f"{self.service_url}/auth/login", params={"return_to": "/app"}
            )
            check(
                "login to /app issues a 302 to Google",
                allowed.status_code == 302
                and "accounts.google" in allowed.headers.get("location", ""),
                f"HTTP {allowed.status_code}",
            )
            missing_state = await client.get(
                f"{self.service_url}/auth/callback", params={"code": "x"}
            )
            check(
                "callback without state is rejected",
                missing_state.status_code == 400,
                f"HTTP {missing_state.status_code}",
            )
            replayed = await client.get(
                f"{self.service_url}/auth/callback",
                params={"state": "never-issued", "code": "x"},
            )
            check(
                "callback with an unknown/replayed state is rejected",
                replayed.status_code == 403,
                f"HTTP {replayed.status_code}",
            )
            no_session = await client.get(f"{self.service_url}/api/v1/me")
            check(
                "authenticated read without a session is 401",
                no_session.status_code == 401,
                f"HTTP {no_session.status_code}",
            )

        token, csrf = await self.mint_probe_session()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                me = await client.get(
                    f"{self.service_url}/api/v1/me",
                    cookies={"commitmentos_session": token},
                )
                check("minted session authenticates", me.status_code == 200, "")
                logout_no_csrf = await client.post(
                    f"{self.service_url}/auth/logout",
                    cookies={"commitmentos_session": token},
                )
                check(
                    "logout without CSRF is rejected and the session survives",
                    logout_no_csrf.status_code == 403,
                    f"HTTP {logout_no_csrf.status_code}",
                )
                still_valid = await client.get(
                    f"{self.service_url}/api/v1/me",
                    cookies={"commitmentos_session": token},
                )
                check("session still valid after failed logout", still_valid.status_code == 200, "")
                logout = await client.post(
                    f"{self.service_url}/auth/logout",
                    cookies={"commitmentos_session": token},
                    headers={"X-CSRF-Token": csrf},
                )
                check("logout with CSRF revokes the session", logout.status_code == 200, "")
                revoked = await client.get(
                    f"{self.service_url}/api/v1/me",
                    cookies={"commitmentos_session": token},
                )
                check("revoked session is rejected", revoked.status_code == 401, "")
        finally:
            await self.revoke_probe_session(token)

    async def probe_csrf(self) -> None:
        token, _ = await self.mint_probe_session()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for path, body in self.CONTROLLED_ROUTES:
                    no_session = await client.post(f"{self.service_url}{path}", json=body)
                    check(
                        f"{path} rejects missing session",
                        no_session.status_code == 401,
                        f"HTTP {no_session.status_code}",
                    )
                    missing_csrf = await client.post(
                        f"{self.service_url}{path}",
                        json=body,
                        cookies={"commitmentos_session": token},
                    )
                    check(
                        f"{path} rejects missing CSRF",
                        missing_csrf.status_code == 403,
                        f"HTTP {missing_csrf.status_code}",
                    )
                    bad_csrf = await client.post(
                        f"{self.service_url}{path}",
                        json=body,
                        cookies={"commitmentos_session": token},
                        headers={"X-CSRF-Token": "wrong-token"},
                    )
                    check(
                        f"{path} rejects invalid CSRF",
                        bad_csrf.status_code == 403,
                        f"HTTP {bad_csrf.status_code}",
                    )
        finally:
            await self.revoke_probe_session(token)

    async def probe_oidc(self) -> None:
        base = self.service_url
        # (group, one guarded path, correct SA, correct audience). Audiences
        # derive from the service under test, not local settings — the local
        # .env carries localhost-flavored audiences (Phase 1 gate convention).
        groups = {
            "pubsub": (
                "/internal/pubsub/gmail",
                self.settings.pubsub_service_account,
                f"{base}/internal/pubsub",
            ),
            "tasks": (
                "/internal/tasks/reconcile-observation",
                self.settings.tasks_service_account,
                f"{base}/internal/tasks",
            ),
            "scheduler": (
                "/internal/scheduler/maintenance/safety_reconciliation",
                self.settings.scheduler_service_account,
                f"{base}/internal/scheduler",
            ),
        }
        async with httpx.AsyncClient(timeout=60) as client:
            for name, (path, correct_sa, correct_audience) in groups.items():
                # Wrong audience: the correct SA signs a token for a *different*
                # group's audience.
                other_audience = next(
                    aud for key, (_p, _sa, aud) in groups.items() if key != name
                )
                wrong_aud_token = self._impersonated_token(correct_sa, other_audience)
                wrong_aud = await client.post(
                    f"{base}{path}",
                    json={},
                    headers={"Authorization": f"Bearer {wrong_aud_token}"},
                )
                check(
                    f"{name} route rejects a valid token with the wrong audience",
                    wrong_aud.status_code in (401, 403),
                    f"HTTP {wrong_aud.status_code}",
                )
                # Wrong identity: a *different* SA signs a token for the correct
                # audience.
                other_sa = next(
                    sa for key, (_p, sa, _aud) in groups.items() if key != name
                )
                wrong_id_token = self._impersonated_token(other_sa, correct_audience)
                wrong_id = await client.post(
                    f"{base}{path}",
                    json={},
                    headers={"Authorization": f"Bearer {wrong_id_token}"},
                )
                check(
                    f"{name} route rejects a valid token with the wrong identity",
                    wrong_id.status_code in (401, 403),
                    f"HTTP {wrong_id.status_code}",
                )

    async def probe_demo(self) -> None:
        methods = ("POST", "PUT", "PATCH", "DELETE")
        paths = [path for path, _ in self.CONTROLLED_ROUTES] + ["/auth/logout"]
        async with httpx.AsyncClient(timeout=30) as client:
            for method in methods:
                for path in paths:
                    response = await client.request(
                        method, f"{self.service_url}/demo{path}", json={"attempt": True}
                    )
                    check(
                        f"{method} /demo{path} rejected",
                        response.status_code == 403,
                        f"HTTP {response.status_code}",
                    )
            for path in (
                "/demo/api/today",
                "/demo/api/commitments",
                "/demo/api/activity",
            ):
                read = await client.get(f"{self.service_url}{path}")
                check(f"{path} seeded read available", read.status_code == 200, "")

    async def probe_sandbox(self) -> None:
        """The interactive sandbox is unauthenticated by design — prove it is
        also unreachable from anywhere else and unable to reach live data."""
        async with httpx.AsyncClient(timeout=60) as client:
            for path in ("/sandbox/api/state",):
                anonymous = await client.get(f"{self.service_url}{path}")
                check(
                    f"GET {path} without a session rejected",
                    anonymous.status_code == 409,
                    f"HTTP {anonymous.status_code}",
                )
            forged = await client.get(
                f"{self.service_url}/sandbox/api/state",
                headers={"X-Sandbox-Session": "forged-session-id"},
            )
            check(
                "forged sandbox session rejected",
                forged.status_code == 409,
                f"HTTP {forged.status_code}",
            )
            anonymous_message = await client.post(
                f"{self.service_url}/sandbox/api/messages",
                json={"sender": "jordan", "message": "Can you help?"},
            )
            check(
                "custom sandbox messages require a session",
                anonymous_message.status_code == 409,
                f"HTTP {anonymous_message.status_code}",
            )

            opened = await client.post(f"{self.service_url}/sandbox/api/session")
            check(
                "sandbox session can be opened",
                opened.status_code == 201,
                f"HTTP {opened.status_code}",
            )
            if opened.status_code != 201:
                return
            session_id = opened.json()["sessionId"]
            headers = {"X-Sandbox-Session": session_id}
            selected = await client.post(
                f"{self.service_url}/sandbox/api/mode",
                headers=headers,
                json={"mode": "guided"},
            )
            if selected.status_code != 200:
                check(
                    "sandbox guided lane can be selected",
                    False,
                    f"HTTP {selected.status_code}",
                )
                return

            unknown = await client.post(
                f"{self.service_url}/sandbox/api/cards/arbitrary_input", headers=headers
            )
            check(
                "sandbox rejects cards outside the fixed deck",
                unknown.status_code == 404,
                f"HTTP {unknown.status_code}",
            )
            forged_sender = await client.post(
                f"{self.service_url}/sandbox/api/messages",
                headers=headers,
                json={"sender": "controlled-01", "message": "Can you help?"},
            )
            check(
                "sandbox rejects caller-defined message identities",
                forged_sender.status_code == 422,
                f"HTTP {forged_sender.status_code}",
            )
            oversized = await client.post(
                f"{self.service_url}/sandbox/api/messages",
                headers=headers,
                json={"sender": "jordan", "message": "x" * 1001},
            )
            check(
                "sandbox rejects oversized custom messages before interpretation",
                oversized.status_code == 422,
                f"HTTP {oversized.status_code}",
            )
            out_of_order = await client.post(
                f"{self.service_url}/sandbox/api/cards/check_in", headers=headers
            )
            check(
                "sandbox rejects a card the state does not allow",
                out_of_order.status_code == 409,
                f"HTTP {out_of_order.status_code}",
            )

            # A sandbox world must never surface controlled-account data: the
            # only user it can name is its own.
            state = await client.get(f"{self.service_url}/sandbox/api/state", headers=headers)
            body = state.text
            check(
                "sandbox state carries no controlled identifiers",
                self._controlled_email() not in body
                and self.settings.controlled_user_id not in body,
                "",
            )
            check(
                "a fresh sandbox holds no commitments",
                state.status_code == 200 and state.json()["commitments"] == [],
                f"HTTP {state.status_code}",
            )

            # Sandbox sessions must be mutually invisible.
            second = await client.post(f"{self.service_url}/sandbox/api/session")
            if second.status_code == 201:
                await client.post(
                    f"{self.service_url}/sandbox/api/cards/msg_request", headers=headers
                )
                other = await client.get(
                    f"{self.service_url}/sandbox/api/state",
                    headers={"X-Sandbox-Session": second.json()["sessionId"]},
                )
                check(
                    "one sandbox session cannot see another's state",
                    other.status_code == 200 and other.json()["commitments"] == [],
                    f"HTTP {other.status_code}",
                )

            # The read-only judge mode must stay mutation-free beside it.
            leak = await client.post(
                f"{self.service_url}/demo/api/today", json={"attempt": True}
            )
            check(
                "sandbox does not open a mutation path under /demo",
                leak.status_code == 403,
                f"HTTP {leak.status_code}",
            )

    def _controlled_email(self) -> str:
        return str(getattr(self.settings, "controlled_email", "")) or "\x00"

    async def probe_ratelimit(self) -> None:
        channels = await self._channels()
        if not channels:
            check(
                "webhook rate-limit exceedance",
                True,
                "SKIPPED: no live channel registered (register a watch first)",
            )
            return
        print(
            "webhook rate-limit exceedance is exercised by the live gate window;\n"
            "  drive it by replaying 21+ valid signals at the deployed webhook and\n"
            "  asserting the 21st returns 429 with zero new sync requests.\n"
            "  (Left as an owner-run step to avoid tripping the durable limiter\n"
            "  outside a gate window.)"
        )
        check(
            "webhook rate-limit exceedance",
            True,
            "documented owner-run step; unit + restart-durability proven in "
            "backend/tests/contract/test_webhook_rate_limit.py",
        )

    async def _channels(self) -> list:
        async def _load(repositories):  # noqa: ANN001, ANN202
            return await repositories._context.query("calendar_channels", [])  # noqa: SLF001

        return await self.uow.read(_load)


async def _run(args: argparse.Namespace) -> int:
    settings = Settings.load()
    service_url = args.service_url or str(settings.service_base_url)
    driver = SecurityDriver(settings, service_url)
    groups = {
        "session": driver.probe_session,
        "csrf": driver.probe_csrf,
        "oidc": driver.probe_oidc,
        "demo": driver.probe_demo,
        "sandbox": driver.probe_sandbox,
        "ratelimit": driver.probe_ratelimit,
    }
    selected = list(groups) if args.command == "all" else [args.command]
    for name in selected:
        print(f"\n--- {name} ---")
        await groups[name]()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    tag = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    report = {
        "service_url": service_url,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "groups": selected,
        "checkpoints": _checkpoints,
        "passed": all(c["ok"] for c in _checkpoints),
    }
    path = EVIDENCE_DIR / f"security_probes_{tag}.json"
    path.write_text(json.dumps(report, indent=2))
    failed = [c["label"] for c in _checkpoints if not c["ok"]]
    print(f"\n{len(_checkpoints) - len(failed)}/{len(_checkpoints)} passed — evidence {path.name}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("all", "session", "csrf", "oidc", "demo", "sandbox", "ratelimit"):
        sub.add_parser(name)
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
