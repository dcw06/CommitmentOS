"""Loads the backend test harness (Phase1App and fakes) for the fault suite.

The backend conftest owns the composition of production repository code over
the in-memory Firestore twin; loading it under a distinct module name keeps
the fault matrix on exactly the same stack without colliding with the
backend suite's own `conftest` module in a combined pytest run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "backend" / "src", ROOT / "backend" / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _load_backend_harness():
    module_name = "commitmentos_backend_test_harness"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "backend" / "tests" / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


harness = _load_backend_harness()
