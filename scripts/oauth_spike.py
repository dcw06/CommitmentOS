"""Phase 0 Section 3 — OAuth publishing-mode decision spike.

Runs the real authorization-code flow against the configured OAuth client with
the frozen scope_set_v1, then exercises refresh and revocation so both
publishing modes can be compared with identical steps.

Usage (from the repo root, consent screen prerequisites completed first):

    python scripts/oauth_spike.py authorize --mode testing
    python scripts/oauth_spike.py refresh
    python scripts/oauth_spike.py revoke
    python scripts/oauth_spike.py status

The refresh token is stored only in Secret Manager. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

import httpx
from google.api_core import exceptions as gapi_exceptions
from google.auth.transport.requests import Request
from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from commitmentos.bootstrap.settings import Settings

REDIRECT_URI = "http://localhost:8080/auth/callback"
CALLBACK_TIMEOUT_SECONDS = 300
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"


def _secret_name_from_ref(ref: str) -> tuple[str, str]:
    parts = ref.split("/")
    return parts[1], parts[3]


def _access_secret(client: secretmanager.SecretManagerServiceClient, ref: str) -> str:
    response = client.access_secret_version(name=ref)
    return response.payload.data.decode("utf-8")


def _store_secret(client: secretmanager.SecretManagerServiceClient, ref: str, value: str) -> str:
    project, name = _secret_name_from_ref(ref)
    parent = f"projects/{project}/secrets/{name}"
    try:
        version = client.add_secret_version(
            parent=parent, payload={"data": value.encode("utf-8")}
        )
    except gapi_exceptions.NotFound:
        client.create_secret(
            parent=f"projects/{project}",
            secret_id=name,
            secret={"replication": {"automatic": {}}},
        )
        version = client.add_secret_version(
            parent=parent, payload={"data": value.encode("utf-8")}
        )
    return version.name.rsplit("/", 1)[-1]


def _decode_jwt_claims_unverified(id_token: str) -> dict:
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class _CallbackCapture(BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != urlparse(REDIRECT_URI).path:
            self.send_response(404)
            self.end_headers()
            return
        _CallbackCapture.captured = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"CommitmentOS OAuth spike: authorization received. You can close this tab.")

    def log_message(self, *args: object) -> None:
        return


def _wait_for_callback() -> dict:
    port = urlparse(REDIRECT_URI).port
    server = HTTPServer(("localhost", port), _CallbackCapture)
    server.timeout = CALLBACK_TIMEOUT_SECONDS
    _CallbackCapture.captured = {}
    while not _CallbackCapture.captured:
        server.handle_request()
    server.server_close()
    return _CallbackCapture.captured


def cmd_authorize(settings: Settings, mode: str) -> int:
    sm = secretmanager.SecretManagerServiceClient()
    client_config = json.loads(_access_secret(sm, settings.oauth_client_secret_ref))
    scopes = list(settings.required_google_scopes())

    flow = Flow.from_client_config(client_config, scopes=scopes, redirect_uri=REDIRECT_URI)
    auth_url, expected_state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="false"
    )

    print(f"[mode: {mode}] Opening the consent flow in your browser.")
    print("Sign in as the CONTROLLED account, and note every warning screen you see.")
    print(f"If the browser does not open: {auth_url}")
    webbrowser.open(auth_url)

    params = _wait_for_callback()
    if "error" in params:
        print(f"RESULT: authorization denied by provider or user: {params['error']}")
        return 1
    if params.get("state") != expected_state:
        print("RESULT: state mismatch — aborting without token exchange.")
        return 1

    flow.fetch_token(code=params["code"])
    creds = flow.credentials

    if getattr(cmd_authorize, "replay_test", False):
        web = client_config["web"]
        replay = httpx.post(
            web["token_uri"],
            data={
                "grant_type": "authorization_code",
                "code": params["code"],
                "client_id": web["client_id"],
                "client_secret": web["client_secret"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": flow.code_verifier,
            },
            timeout=30,
        )
        replay_error = replay.json().get("error", "<none>") if replay.status_code != 200 else "<none>"
        print(
            f"code_replay_test: second exchange of the same authorization code -> "
            f"HTTP {replay.status_code}, error={replay_error}"
        )

    granted = sorted(creds.scopes or [])
    requested = sorted(scopes)
    claims = _decode_jwt_claims_unverified(creds.id_token) if creds.id_token else {}
    account_email = claims.get("email", "<no id_token email>")
    expiry = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
    now = datetime.now(timezone.utc)

    print()
    print("=== sanitized authorization evidence ===")
    print(f"observed_at_utc: {now.isoformat(timespec='seconds')}")
    print(f"publishing_mode_label: {mode}")
    print(f"account: {'controlled account' if account_email == settings.controlled_email else 'UNEXPECTED: ' + account_email}")
    print(f"requested_scopes: {len(requested)}")
    print(f"granted_scopes: {len(granted)}")
    if granted != requested:
        print(f"SCOPE MISMATCH — granted: {granted}")
    print(f"refresh_token_issued: {creds.refresh_token is not None}")
    if expiry:
        print(f"access_token_lifetime_seconds: {int((expiry - now).total_seconds())}")

    if creds.refresh_token:
        version = _store_secret(sm, settings.controlled_refresh_token_secret_ref, creds.refresh_token)
        print(f"refresh_token_stored: Secret Manager version {version}")
    else:
        print("refresh_token_stored: NO TOKEN — record this as the mode's observed behavior")

    print()
    print("Record in docs/phase0_evidence/: the consent warnings you saw, per scope class.")
    return 0


def _load_stored_credentials(
    settings: Settings,
    sm: secretmanager.SecretManagerServiceClient,
    version: str | None = None,
) -> Credentials:
    client_config = json.loads(_access_secret(sm, settings.oauth_client_secret_ref))["web"]
    token_ref = settings.controlled_refresh_token_secret_ref
    if version is not None:
        token_ref = token_ref.rsplit("/", 1)[0] + f"/{version}"
    refresh_token = _access_secret(sm, token_ref)
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
        scopes=list(settings.required_google_scopes()),
    )


def cmd_refresh(settings: Settings, version: str | None = None) -> int:
    sm = secretmanager.SecretManagerServiceClient()
    creds = _load_stored_credentials(settings, sm, version)
    if version is not None:
        print(f"refreshing stored token version: {version}")
    try:
        creds.refresh(Request())
    except Exception as error:  # noqa: BLE001 — the failure class is the evidence
        print(f"refresh_result: FAILED — {type(error).__name__}: {error}")
        return 1
    expiry = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
    now = datetime.now(timezone.utc)
    print("refresh_result: OK")
    if expiry:
        print(f"new_access_token_lifetime_seconds: {int((expiry - now).total_seconds())}")
    return 0


def cmd_revoke(settings: Settings) -> int:
    sm = secretmanager.SecretManagerServiceClient()
    refresh_token = _access_secret(sm, settings.controlled_refresh_token_secret_ref)
    response = httpx.post(REVOKE_ENDPOINT, data={"token": refresh_token}, timeout=30)
    print(f"revoke_http_status: {response.status_code}")
    print("Expected next step: `refresh` must now fail with invalid_grant.")
    return 0 if response.status_code == 200 else 1


def cmd_status(settings: Settings) -> int:
    sm = secretmanager.SecretManagerServiceClient()
    project, name = _secret_name_from_ref(settings.controlled_refresh_token_secret_ref)
    try:
        versions = sm.list_secret_versions(parent=f"projects/{project}/secrets/{name}")
        for v in versions:
            print(f"{v.name.rsplit('/', 1)[-1]}: {v.state.name} created {v.create_time}")
    except gapi_exceptions.NotFound:
        print("no stored refresh token yet")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    authorize = sub.add_parser("authorize", help="run the consent flow and store the refresh token")
    authorize.add_argument("--mode", choices=["testing", "production"], required=True,
                           help="label for the consent-screen publishing mode being tested")
    authorize.add_argument("--replay-test", action="store_true",
                           help="after a successful exchange, replay the same code and expect rejection")
    refresh = sub.add_parser("refresh", help="refresh an access token from the stored refresh token")
    refresh.add_argument("--version", default=None, help="specific stored secret version (default: latest)")
    sub.add_parser("revoke", help="revoke the stored refresh token")
    sub.add_parser("status", help="show stored refresh-token secret versions")
    args = parser.parse_args()

    settings = Settings.load()
    if args.command == "authorize":
        cmd_authorize.replay_test = args.replay_test
        return cmd_authorize(settings, args.mode)
    if args.command == "refresh":
        return cmd_refresh(settings, args.version)
    if args.command == "revoke":
        return cmd_revoke(settings)
    return cmd_status(settings)


if __name__ == "__main__":
    raise SystemExit(main())
