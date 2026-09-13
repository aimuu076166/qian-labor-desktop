import json
from pathlib import Path


def test_macos_app_does_not_advertise_a_system_older_than_its_ocr_runtime():
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / "apps/desktop/src-tauri/tauri.conf.json").read_text())
    version = config["bundle"]["macOS"].get("minimumSystemVersion", "10.13")
    assert tuple(int(part) for part in version.split(".")) >= (11, 0)
