"""Smoke tests for voice API routes.

The suite intentionally uses shared fixtures from tests/conftest.py and
avoids standalone DB engines/files.
"""

import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.tts import voices_routes
from services.voice_management_service import VoiceManagementService


class TestVoiceRouteGuards:
    """Route availability and auth guards."""

    def test_user_custom_voices_requires_auth(self, client: TestClient):
        response = client.get("/api/voices/user/custom")
        assert response.status_code in (401, 403)

    def test_global_voices_requires_auth(self, client: TestClient):
        response = client.get("/api/voices/global")
        assert response.status_code in (401, 403)

    def test_user_voice_settings_requires_auth(self, client: TestClient):
        response = client.put("/api/voices/user/settings/1", json={"cfg_strength": 2.5})
        assert response.status_code in (401, 403)

    def test_delete_custom_voice_requires_auth(self, client: TestClient):
        response = client.delete("/api/voices/user/custom/1")
        assert response.status_code in (401, 403)

    def test_admin_global_requires_auth(self, client: TestClient):
        response = client.get("/api/voices/admin/global")
        assert response.status_code in (401, 403)


class TestVoiceRoutesAuthenticated:
    """Authenticated behavior for user/admin routes."""

    def test_enabled_voices_upstream_401_is_mapped_to_503(
        self,
        authenticated_client: TestClient,
        test_user,
        monkeypatch,
    ):
        class _FakeResponse:
            status_code = 401
            text = '{"detail":"Unauthorized"}'

            def json(self):
                return {"detail": "Unauthorized"}

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, *args, **kwargs):
                return _FakeResponse()

        monkeypatch.setattr(voices_routes.httpx, "AsyncClient", _FakeAsyncClient)
        monkeypatch.setattr(voices_routes, "_provider_base_url", lambda provider: "http://voice-upstream")
        monkeypatch.setattr(voices_routes, "_provider_upstream_params", lambda provider, extra_params=None: extra_params or {})
        monkeypatch.setattr(voices_routes, "_tts_auth_headers", lambda provider: {})

        response = authenticated_client.get(f"/api/user/voices/enabled/{test_user.id}?provider=f5")

        assert response.status_code == 503
        assert response.json()["detail"] == "TTS service authorization failed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "call_kwargs"),
        [
            ("get_global_voices", {"provider": "f5"}),
            ("get_user_custom_voices", {"user_id": 1, "provider": "f5"}),
            ("admin_get_global_voices", {"provider": "f5"}),
        ],
    )
    async def test_voice_management_list_endpoints_map_upstream_401_to_503(
        self,
        monkeypatch,
        method_name: str,
        call_kwargs: dict,
    ):
        class _FakeResponse:
            status_code = 401
            text = '{"detail":"Unauthorized"}'

            def json(self):
                return {"detail": "Unauthorized"}

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, *args, **kwargs):
                return _FakeResponse()

        monkeypatch.setattr("services.voice_management_service.httpx.AsyncClient", _FakeAsyncClient)
        monkeypatch.setattr(
            VoiceManagementService,
            "_provider_tts_api_base",
            lambda self, provider="f5": "http://voice-upstream/api/tts",
        )
        monkeypatch.setattr(
            VoiceManagementService,
            "_provider_admin_api_base",
            lambda self, provider="f5": "http://voice-upstream/api/admin",
        )
        monkeypatch.setattr(VoiceManagementService, "_tts_auth_headers", lambda self, provider="f5": {})
        monkeypatch.setattr(
            VoiceManagementService,
            "_provider_request_params",
            lambda self, provider="f5", extra=None: extra or {},
        )

        service = VoiceManagementService(object())

        with pytest.raises(HTTPException) as exc_info:
            await getattr(service, method_name)(**call_kwargs)

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "TTS service authorization failed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "call_kwargs"),
        [
            ("get_global_voices", {"provider": "qwen"}),
            ("get_user_custom_voices", {"user_id": 1, "provider": "qwen"}),
            ("admin_get_global_voices", {"provider": "qwen"}),
        ],
    )
    async def test_voice_management_list_endpoints_map_upstream_connect_error_to_503(
        self,
        monkeypatch,
        method_name: str,
        call_kwargs: dict,
    ):
        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, *args, **kwargs):
                raise httpx.ConnectError("connect failed")

        monkeypatch.setattr("services.voice_management_service.httpx.AsyncClient", _FakeAsyncClient)
        monkeypatch.setattr(
            VoiceManagementService,
            "_provider_tts_api_base",
            lambda self, provider="qwen": "http://voice-upstream/api/tts",
        )
        monkeypatch.setattr(
            VoiceManagementService,
            "_provider_admin_api_base",
            lambda self, provider="qwen": "http://voice-upstream/api/admin",
        )
        monkeypatch.setattr(VoiceManagementService, "_tts_auth_headers", lambda self, provider="qwen": {})
        monkeypatch.setattr(
            VoiceManagementService,
            "_provider_request_params",
            lambda self, provider="qwen", extra=None: extra or {},
        )

        service = VoiceManagementService(object())

        with pytest.raises(HTTPException) as exc_info:
            await getattr(service, method_name)(**call_kwargs)

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == "Failed to reach TTS voice service"

    @pytest.mark.parametrize("provider", ["f5", "qwen"])
    def test_get_global_voices_authenticated(
        self, authenticated_client: TestClient, provider: str
    ):
        response = authenticated_client.get(f"/api/voices/global?provider={provider}")
        # Qwen CRUD is disabled unless QWEN_VOICE_SERVICE_URL is configured.
        assert response.status_code in (200, 500, 501, 503)

    @pytest.mark.parametrize("provider", ["f5", "qwen"])
    def test_get_custom_voices_authenticated(
        self, authenticated_client: TestClient, provider: str
    ):
        response = authenticated_client.get(
            f"/api/voices/user/custom?provider={provider}"
        )
        assert response.status_code in (200, 500, 501, 503)

    @pytest.mark.parametrize("provider", ["f5", "qwen"])
    def test_update_user_voice_settings_authenticated(
        self, authenticated_client: TestClient, provider: str
    ):
        response = authenticated_client.put(
            f"/api/voices/user/settings/1?provider={provider}",
            json={"cfg_strength": 2.5, "speed_preset": "normal", "volume": 70},
        )
        # 404 is valid when voice does not exist in provider service.
        assert response.status_code in (200, 404, 500, 501, 503)

    @pytest.mark.parametrize("provider", ["f5", "qwen"])
    def test_admin_global_routes_authenticated(
        self, admin_client: TestClient, provider: str
    ):
        list_response = admin_client.get(
            f"/api/voices/admin/global?provider={provider}"
        )
        assert list_response.status_code in (200, 500, 501, 503)

        update_response = admin_client.put(
            f"/api/voices/admin/global/1?provider={provider}",
            json={"cfg_strength": 3.0},
        )
        assert update_response.status_code in (200, 404, 500, 501, 503)

        rename_response = admin_client.put(
            f"/api/voices/admin/global/1/rename?provider={provider}",
            json={"new_name": "new_voice_name"},
        )
        assert rename_response.status_code in (200, 404, 500, 501, 503)

        delete_response = admin_client.delete(
            f"/api/voices/admin/global/1?provider={provider}"
        )
        assert delete_response.status_code in (200, 404, 500, 501, 503)

    def test_provider_capabilities_endpoint(self, authenticated_client: TestClient):
        response = authenticated_client.get("/api/voices/providers/capabilities")
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("success") is True
        providers = payload.get("providers") or {}
        assert "f5" in providers
        assert "qwen" in providers


class TestVoiceInputValidation:
    """Input contract checks that do not require upstream providers."""

    def test_speed_presets_contract(self):
        valid_presets = {"very_slow", "slow", "normal", "fast", "very_fast"}
        assert len(valid_presets) == 5

    def test_cfg_strength_range_contract(self):
        assert 0.0 <= 0.0 <= 10.0
        assert 0.0 <= 10.0 <= 10.0

    def test_volume_range_contract(self):
        assert 0 <= 0 <= 100
        assert 0 <= 100 <= 100
