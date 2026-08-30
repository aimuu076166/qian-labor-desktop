from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "scan_sensitive.py"


def _load_scan_module():
    spec = importlib.util.spec_from_file_location("qian_sensitive_scan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_zhipu_style_key_assignments_are_detected_for_supported_env_names() -> None:
    scan = _load_scan_module()
    synthetic_key = b"1234567890abcdef.abcdefghijklmnopqrstuvwxyz012345"
    pattern = scan.PATTERNS["ZHIPU_KEY_ASSIGNMENT"]

    for name in (b"AI_API_KEY", b"ZAI_API_KEY", b"ZHIPU_API_KEY", b"BIGMODEL_API_KEY"):
        assert pattern.search(name + b"=" + synthetic_key), name
        assert pattern.search(b"export " + name + b"=" + synthetic_key), name


def test_zhipu_key_placeholders_are_not_reported_as_secrets() -> None:
    scan = _load_scan_module()
    pattern = scan.PATTERNS["ZHIPU_KEY_ASSIGNMENT"]

    for prefix in (b"", b"export "):
        for value in (b"<your-zhipu-api-key>", b"YOUR_API_KEY", b"${AI_API_KEY}"):
            assert pattern.search(prefix + b"AI_API_KEY=" + value) is None


def test_lowercase_python_setting_is_not_treated_as_an_env_assignment() -> None:
    scan = _load_scan_module()
    pattern = scan.PATTERNS["ZHIPU_KEY_ASSIGNMENT"]

    source = b'    ai_api_key="synthetic-zhipu-key-never-real",'

    assert pattern.search(source) is None
