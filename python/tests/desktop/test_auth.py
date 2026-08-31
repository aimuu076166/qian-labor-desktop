from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from qian_labor.ai.zhipu_provider import ZhipuChatCompletionsProvider
from qian_labor.desktop.app import create_desktop_app
from qian_labor.settings import Settings


PEPPER = "desktop-zhipu-runtime-pepper-32-characters-minimum"
TAURI_PRODUCTION_ORIGINS = ("tauri://localhost", "http://tauri.localhost")


@pytest.mark.parametrize("origin", TAURI_PRODUCTION_ORIGINS)
def test_business_api_allows_tauri_production_webview_origin(
    tmp_path: Path, origin: str
) -> None:
    token = "cors-test-launch-token-32-characters-minimum"
    app = create_desktop_app(data_dir=tmp_path, launch_token=token)

    with TestClient(app) as client:
        preflight = client.options(
            "/api/analyses",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-qian-desktop-token",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["Access-Control-Allow-Origin"] == origin
        allowed_headers = preflight.headers["Access-Control-Allow-Headers"].lower()
        assert "content-type" in allowed_headers
        assert "x-qian-desktop-token" in allowed_headers

        created = client.post(
            "/api/analyses",
            headers={"Origin": origin, "X-Qian-Desktop-Token": token},
            json={"name": "桌面体检", "company_display_name": "虚构企业"},
        )
        assert created.status_code == 201
        assert created.headers["Access-Control-Allow-Origin"] == origin


def test_business_api_rejects_untrusted_browser_origin(tmp_path: Path) -> None:
    app = create_desktop_app(
        data_dir=tmp_path,
        launch_token="untrusted-origin-token-32-characters-minimum",
    )

    with TestClient(app) as client:
        preflight = client.options(
            "/api/analyses",
            headers={
                "Origin": "https://example.invalid",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-qian-desktop-token",
            },
        )

    assert preflight.status_code != 200
    assert "Access-Control-Allow-Origin" not in preflight.headers


def test_health_is_public_but_business_api_requires_launch_token(tmp_path: Path) -> None:
    token = "test-launch-token-32-characters-minimum"
    app = create_desktop_app(data_dir=tmp_path, launch_token=token)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "qian-labor-desktop-sidecar"}

        assert client.get("/api/status").status_code == 401
        assert (
            client.get("/api/status", headers={"X-Qian-Desktop-Token": "wrong"}).status_code
            == 401
        )
        authorized = client.get(
            "/api/status",
            headers={"X-Qian-Desktop-Token": token},
        )
        assert authorized.status_code == 200
        assert authorized.json()["status"] == "ready"


def test_internal_shutdown_requires_token_and_is_idempotent(tmp_path: Path) -> None:
    token = "shutdown-test-token-32-characters-minimum"
    requested = Event()
    app = create_desktop_app(
        data_dir=tmp_path,
        launch_token=token,
        shutdown_callback=requested.set,
    )

    with TestClient(app) as client:
        assert client.post("/api/internal/shutdown").status_code == 401
        assert (
            client.post(
                "/api/internal/shutdown",
                headers={"X-Qian-Desktop-Token": "wrong"},
            ).status_code
            == 401
        )
        assert not requested.is_set()

        first = client.post(
            "/api/internal/shutdown",
            headers={"X-Qian-Desktop-Token": token},
        )
        second = client.post(
            "/api/internal/shutdown",
            headers={"X-Qian-Desktop-Token": token},
        )

        assert first.status_code == 202
        assert first.json() == {"status": "shutdown_requested"}
        assert second.status_code == 202
        assert second.json() == first.json()
        assert requested.is_set()
        assert token not in first.text
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "service": "qian-labor-desktop-sidecar",
        }


def test_internal_shutdown_preserves_lifespan_cleanup(tmp_path: Path) -> None:
    token = "lifespan-shutdown-token-32-characters-minimum"
    requested = Event()
    app = create_desktop_app(
        data_dir=tmp_path,
        launch_token=token,
        shutdown_callback=requested.set,
    )
    queue_shutdown = Mock(wraps=app.state.processing_queue.shutdown)
    database_dispose = Mock(wraps=app.state.database.dispose)
    app.state.processing_queue.shutdown = queue_shutdown
    app.state.database.dispose = database_dispose

    with TestClient(app) as client:
        response = client.post(
            "/api/internal/shutdown",
            headers={"X-Qian-Desktop-Token": token},
        )
        assert response.status_code == 202

    queue_shutdown.assert_called_once_with()
    database_dispose.assert_called_once_with()


def test_creating_desktop_app_places_sqlite_inside_private_data_dir(tmp_path: Path) -> None:
    app = create_desktop_app(
        data_dir=tmp_path,
        launch_token="another-test-launch-token-32-chars",
    )
    assert app.state.database_path == tmp_path / "qian-labor.db"
    assert app.state.database_path.exists()


def test_desktop_runtime_uses_configured_zhipu_provider_and_call_limit(tmp_path: Path) -> None:
    settings = Settings(
        app_secret="separate-desktop-runtime-secret",
        pii_hash_pepper=PEPPER,
        ai_provider="zhipu",
        ai_api_key="synthetic-zhipu-key-never-real",
        ai_base_url="",
        ai_text_model="",
        ai_vision_model="",
        ai_max_calls_per_analysis=7,
    )

    app = create_desktop_app(
        data_dir=tmp_path,
        launch_token="zhipu-test-launch-token-32-chars",
        settings=settings,
    )
    pipeline = app.state.processing_queue.pipeline_factory()

    assert isinstance(pipeline.provider, ZhipuChatCompletionsProvider)
    assert pipeline.provider.text_model == "glm-5.3-flash"
    assert pipeline.provider.vision_model == "glm-5.3-flash"
    assert pipeline.max_provider_calls == 7
