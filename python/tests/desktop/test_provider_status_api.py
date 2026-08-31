from pathlib import Path

from fastapi.testclient import TestClient

import qian_labor.desktop.app as desktop_app
from qian_labor.ai.providers import AIProviderError
from qian_labor.ai.schemas import ExtractionResult
from qian_labor.desktop.app import create_desktop_app

TOKEN = "desktop-provider-status-token"
HEADERS = {"X-Qian-Desktop-Token": TOKEN}


class ExternalProviderStub:
    name = "zhipu"
    is_external = True

    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, bytes]] = []

    def extract(self, filename: str, content: bytes) -> ExtractionResult:
        self.calls.append((filename, content))
        if self.failure:
            raise AIProviderError(self.failure)
        return ExtractionResult(document_type="other")


def test_fake_provider_is_reported_as_demo_and_cannot_pass_real_connection_test(
    tmp_path: Path,
) -> None:
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)

    with TestClient(app) as client:
        status = client.get("/api/provider/status", headers=HEADERS)
        checked = client.post("/api/provider/connection-test", headers=HEADERS)

    assert status.status_code == 200
    assert status.json() == {"provider": "fake", "mode": "demo", "configured": False}
    assert checked.status_code == 409
    assert checked.json()["detail"]["code"] == "AI_PROVIDER_NOT_CONFIGURED"


def test_real_provider_connection_test_uses_only_bundled_synthetic_content(
    tmp_path: Path, monkeypatch
) -> None:
    provider = ExternalProviderStub()
    monkeypatch.setattr(desktop_app, "provider_from_settings", lambda _settings: provider)
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)

    with TestClient(app) as client:
        status = client.get("/api/provider/status", headers=HEADERS)
        checked = client.post("/api/provider/connection-test", headers=HEADERS)

    assert status.json() == {"provider": "zhipu", "mode": "real", "configured": True}
    assert checked.status_code == 200
    assert checked.json() == {"provider": "zhipu", "status": "connected"}
    assert provider.calls == [
        (
            "qian-provider-connection-check.txt",
            b"QIAN_SYNTHETIC_CONNECTION_CHECK: no employee or company data.",
        )
    ]


def test_real_provider_connection_failure_returns_only_a_stable_error_code(
    tmp_path: Path, monkeypatch
) -> None:
    provider = ExternalProviderStub("AI_PROVIDER_ERROR")
    monkeypatch.setattr(desktop_app, "provider_from_settings", lambda _settings: provider)
    app = create_desktop_app(data_dir=tmp_path / "app-data", launch_token=TOKEN)

    with TestClient(app) as client:
        checked = client.post("/api/provider/connection-test", headers=HEADERS)

    assert checked.status_code == 502
    assert checked.json() == {"detail": {"code": "AI_PROVIDER_ERROR"}}
