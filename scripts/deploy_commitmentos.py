"""Deploy or verify the judge-facing Cloud Run release contract.

Deployment is deliberately opt-in and owner-run. With no flag this script is
read-only: it checks Cloud Run configuration and the public sandbox bundle/API.
Use ``--deploy`` to build, route traffic to the new revision, then run the same
checks. No evidence artifact is written or overwritten.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.bootstrap.cloud_run_contract import (  # noqa: E402
    SANDBOX_SECRET_ENV,
    build_deploy_command,
    build_describe_command,
    build_to_latest_command,
    cloud_run_contract_errors,
    public_surface_errors,
)
from commitmentos.bootstrap.settings import Settings  # noqa: E402


def _run(command: tuple[str, ...], *, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture,
    )
    return completed.stdout if capture else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="owner only: deploy, route 100%% to latest, then verify",
    )
    args = parser.parse_args()

    settings = Settings.load()
    settings.validate_live_mode_guards()
    sandbox_secret_ref = settings.sandbox_gemini_api_key_secret_ref
    if sandbox_secret_ref is None:
        raise RuntimeError(f"{SANDBOX_SECRET_ENV} is required")

    if args.deploy:
        _run(build_deploy_command(settings))
        _run(build_to_latest_command(settings))

    description = json.loads(_run(build_describe_command(settings), capture=True))
    errors = [
        *cloud_run_contract_errors(description, sandbox_secret_ref),
        *public_surface_errors(
            str(settings.service_base_url), probe_live_model=args.deploy
        ),
    ]
    if errors:
        for error in errors:
            print(f"FAIL  {error}")
        return 1
    print("PASS  Cloud Run affinity, scale, key isolation, latest traffic, API, and UI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
