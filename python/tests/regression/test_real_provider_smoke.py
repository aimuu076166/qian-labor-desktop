from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "real_provider_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("qian_real_provider_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_provider_smoke_without_key_is_explicitly_not_run() -> None:
    env = os.environ.copy()
    for name in (
        "AI_API_KEY",
        "PII_HASH_PEPPER",
        "REAL_PROVIDER_SMOKE_API_KEY",
        "REAL_PROVIDER_SMOKE_PII_HASH_PEPPER",
    ):
        env.pop(name, None)

    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.splitlines() == [
        "REAL_PROVIDER_SMOKE=NOT_RUN",
        "REASON=AI_API_KEY_MISSING",
    ]


def test_source_trace_count_uses_loaded_fact_values_not_sql_json_comparison() -> None:
    smoke = _load_smoke_module()
    facts = [
        SimpleNamespace(source_locator_ids=["source-1"]),
        SimpleNamespace(source_locator_ids=[]),
        SimpleNamespace(source_locator_ids=["source-2", "source-3"]),
    ]

    assert smoke._count_sourced_facts(facts) == 2
