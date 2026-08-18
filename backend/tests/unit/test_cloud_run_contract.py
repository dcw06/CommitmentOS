from __future__ import annotations

from commitmentos.bootstrap.cloud_run_contract import (
    cloud_run_contract_errors,
)

SANDBOX_REF = "projects/test/secrets/sandbox/versions/1"


def service_description(
    *, affinity: bool = True, max_scale: str = "2", sandbox_ref: str = SANDBOX_REF
) -> dict:
    return {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "autoscaling.knative.dev/maxScale": max_scale,
                        "run.googleapis.com/sessionAffinity": str(affinity).lower(),
                    }
                },
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {
                                    "name": "COMMITMENTOS_SANDBOX_GEMINI_API_KEY_SECRET_REF",
                                    "value": sandbox_ref,
                                }
                            ]
                        }
                    ]
                },
            }
        },
        "status": {
            "latestReadyRevisionName": "commitmentos-00002-new",
            "traffic": [
                {"revisionName": "commitmentos-00002-new", "percent": 100}
            ],
        },
    }


def test_cloud_run_contract_accepts_the_bounded_latest_revision() -> None:
    assert cloud_run_contract_errors(service_description(), SANDBOX_REF) == ()


def test_cloud_run_contract_reports_each_drift_dimension() -> None:
    description = service_description(
        affinity=False,
        max_scale="20",
        sandbox_ref="projects/test/secrets/wrong/versions/1",
    )
    description["status"]["traffic"] = [
        {"revisionName": "commitmentos-00001-old", "percent": 100}
    ]

    errors = cloud_run_contract_errors(description, SANDBOX_REF)

    assert len(errors) == 4
    assert any("session affinity" in error for error in errors)
    assert any("maxScale" in error for error in errors)
    assert any("sandbox Gemini secret" in error for error in errors)
    assert any("100% of traffic" in error for error in errors)
