import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.tts.tts_manager import TTSManager


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(
        "services.tts.tts_manager.get_basic_tts",
        lambda: SimpleNamespace(cleanup_old_files=lambda: None),
    )
    monkeypatch.setattr(
        "services.tts.tts_manager.get_google_cloud_tts",
        lambda: SimpleNamespace(),
    )
    return TTSManager()


@pytest.mark.asyncio
async def test_f5_cloud_unhealthy_falls_back_to_basic(manager):
    manager.check_tts_service_health = AsyncMock(return_value=False)
    manager._synthesize_via_tts_service = AsyncMock(return_value={"success": True})
    manager._synthesize_via_basic_tts = AsyncMock(
        return_value={"success": True, "tts_type": "basic_gtts"}
    )

    result = await manager.synthesize_tts(
            channel_name="chan",
        text="hello",
        author="user",
        user_id=1,
        use_ai_tts=True,
        engine="f5tts",
        tts_settings={
            "advanced_provider": "f5",
            "f5_mode": "cloud",
            "use_local_tts": False,
        },
    )

    assert result["success"] is True
    manager.check_tts_service_health.assert_awaited_once()
    manager._synthesize_via_tts_service.assert_not_awaited()
    manager._synthesize_via_basic_tts.assert_awaited_once()


@pytest.mark.asyncio
async def test_qwen_local_without_endpoint_falls_back_to_basic(manager):
    manager.get_user_tts_endpoint = AsyncMock(return_value=None)
    manager.check_tts_service_health = AsyncMock(return_value=True)
    manager._synthesize_via_tts_service = AsyncMock(return_value={"success": True})
    manager._synthesize_via_basic_tts = AsyncMock(
        return_value={"success": True, "tts_type": "basic_gtts"}
    )

    result = await manager.synthesize_tts(
            channel_name="chan",
        text="hello",
        author="user",
        user_id=7,
        db_session=object(),
        use_ai_tts=True,
        engine="qwen",
        tts_settings={
            "advanced_provider": "qwen",
            "qwen_mode": "local",
            "use_local_tts": False,
        },
    )

    assert result["success"] is True
    manager.get_user_tts_endpoint.assert_awaited_once()
    manager.check_tts_service_health.assert_not_awaited()
    manager._synthesize_via_tts_service.assert_not_awaited()
    manager._synthesize_via_basic_tts.assert_awaited_once()


@pytest.mark.asyncio
async def test_gcloud_failure_falls_back_to_basic(manager):
    manager._synthesize_via_google_cloud_tts = AsyncMock(
        return_value={"success": False, "error": "provider down"}
    )
    manager._synthesize_via_basic_tts = AsyncMock(
        return_value={"success": True, "tts_type": "basic_gtts"}
    )

    result = await manager.synthesize_tts(
        channel_name="chan",
        text="hello",
        author="user",
        user_id=1,
        use_ai_tts=False,
        engine="gcloud",
        tts_settings={"gcloud_voices": ["ru-RU-Chirp3-HD-Zephyr"]},
    )

    assert result["success"] is True
    manager._synthesize_via_google_cloud_tts.assert_awaited_once()
    manager._synthesize_via_basic_tts.assert_awaited_once()


@pytest.mark.asyncio
async def test_qwen_cloud_without_gateway_falls_back_to_basic(manager, monkeypatch):
    monkeypatch.setattr("services.tts.provider_utils.settings.tts_gateway_url", "")
    manager.check_tts_service_health = AsyncMock(return_value=False)
    manager._synthesize_via_tts_service = AsyncMock(return_value={"success": True})
    manager._synthesize_via_basic_tts = AsyncMock(
        return_value={"success": True, "tts_type": "basic_gtts"}
    )

    result = await manager.synthesize_tts(
        channel_name="chan",
        text="hello",
        author="user",
        user_id=5,
        use_ai_tts=True,
        engine="qwen",
        tts_settings={
            "advanced_provider": "qwen",
            "qwen_mode": "cloud",
            "use_local_tts": False,
        },
    )

    assert result["success"] is True
    manager.check_tts_service_health.assert_awaited_once()
    manager._synthesize_via_tts_service.assert_not_awaited()
    manager._synthesize_via_basic_tts.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_cache_scoped_by_endpoint(manager, monkeypatch):
    calls: list[str] = []
    responses = {
        "http://endpoint-a/api/health": (200, {"status": "offline"}),
        "http://endpoint-b/api/health": (200, {"status": "healthy"}),
    }

    class _FakeResponse:
        def __init__(self, status: int, payload: dict):
            self.status = status
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return self._payload

    class _FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str, **kwargs):
            _ = kwargs
            calls.append(url)
            status, payload = responses[url]
            return _FakeResponse(status, payload)

    monkeypatch.setattr(
        "services.tts.tts_manager.aiohttp.ClientSession",
        lambda timeout=None: _FakeClientSession(),
    )
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_hosts",
        "endpoint-a,endpoint-b",
    )
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_cidrs",
        "",
    )

    result_a = await manager.check_tts_service_health(
        provider="f5",
        endpoint_override="http://endpoint-a",
    )
    result_b = await manager.check_tts_service_health(
        provider="f5",
        endpoint_override="http://endpoint-b",
    )

    assert result_a is False
    assert result_b is True
    assert "http://endpoint-b/api/health" in calls

    calls_before_cached = len(calls)
    cached_result = await manager.check_tts_service_health(
        provider="f5",
        endpoint_override="http://endpoint-b",
    )
    assert cached_result is True
    assert len(calls) == calls_before_cached


@pytest.mark.asyncio
async def test_gateway_health_status_ok_is_considered_healthy(manager, monkeypatch):
    calls: list[str] = []

    class _FakeResponse:
        def __init__(self, status: int, payload: dict):
            self.status = status
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return self._payload

    class _FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str, **kwargs):
            calls.append(url)
            _ = kwargs
            return _FakeResponse(200, {"status": "ok", "redis": "ok", "scheduler": "running"})

    monkeypatch.setattr(
        "services.tts.tts_manager.aiohttp.ClientSession",
        lambda timeout=None: _FakeClientSession(),
    )
    monkeypatch.setattr("services.tts.provider_utils.settings.tts_gateway_url", "http://gateway")
    monkeypatch.setattr("core.internal_service_auth.settings.tts_gateway_url", "http://gateway")
    monkeypatch.setattr("core.internal_service_auth.settings.tts_gateway_api_key", "gateway-key")

    result = await manager.check_tts_service_health(provider="qwen", force_check=True)

    assert result is True
    assert "http://gateway/health/ready" in calls


@pytest.mark.asyncio
async def test_materialize_provider_audio_uses_provider_auth_for_direct_provider_url(manager, monkeypatch):
    requested_headers = {}

    class _FakeResponse:
        def __init__(self, status: int, payload: bytes):
            self.status = status
            self._payload = payload
            self.headers = {"content-type": "audio/wav"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def read(self):
            return self._payload

        async def text(self):
            return self._payload.decode("utf-8", errors="ignore")

    class _FakeSession:
        def get(self, url: str, **kwargs):
            requested_headers["url"] = url
            requested_headers["headers"] = kwargs.get("headers") or {}
            return _FakeResponse(200, b"wav-bytes")

    monkeypatch.setattr("services.tts.tts_manager.get_provider_service_url", lambda provider: "http://provider-upstream")
    monkeypatch.setattr("core.internal_service_auth.settings.tts_gateway_url", "http://gateway")
    monkeypatch.setattr("core.internal_service_auth.settings.tts_gateway_api_key", "gateway-key")
    monkeypatch.setattr("core.internal_service_auth.settings.f5_tts_service_api_key", "provider-key")
    monkeypatch.setattr("core.internal_service_auth.settings.tts_internal_api_key", "")

    manager._persist_audio_bytes = AsyncMock(
        return_value={
            "audio_url": "http://backend/api/tts/audio/local.wav",
            "audio_path": "C:/tmp/local.wav",
        }
    )

    result = await manager._materialize_provider_audio(
        session=_FakeSession(),
        provider="f5",
        audio_url="http://provider-upstream/api/tts/audio/test.wav",
        endpoint="http://gateway",
        headers={"Authorization": "Bearer gateway-key", "X-API-Key": "gateway-key"},
    )

    assert result["audio_url"] == "http://backend/api/tts/audio/local.wav"
    assert requested_headers["url"] == "http://provider-upstream/api/tts/audio/test.wav"
    assert requested_headers["headers"]["Authorization"] == "Bearer provider-key"
    assert requested_headers["headers"]["X-API-Key"] == "provider-key"
    manager._persist_audio_bytes.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_user_tts_endpoint_returns_saved_api_key(manager, monkeypatch):
    class _LocalConfig:
        endpoint_url = "http://endpoint-a"
        api_key = "local-secret-key"

    class _FakeRepo:
        def __init__(self, db_session):
            _ = db_session

        def get_healthy(self, user_id: int, provider: str):
            _ = (user_id, provider)
            return _LocalConfig()

    monkeypatch.setattr("repositories.local_tts_repository.LocalTTSRepository", _FakeRepo)
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_hosts",
        "endpoint-a",
    )
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_cidrs",
        "",
    )

    endpoint_payload = await manager.get_user_tts_endpoint(
        user_id=1,
        db_session=object(),
        provider="f5",
    )

    assert endpoint_payload == {
        "endpoint_url": "http://endpoint-a",
        "api_key": "local-secret-key",
    }


@pytest.mark.asyncio
async def test_build_provider_success_result_raises_when_audio_url_missing(manager):
    class _FakeSession:
        pass

    with pytest.raises(RuntimeError, match="missing audio_url"):
        await manager._build_provider_success_result(
            session=_FakeSession(),
            provider="f5",
            endpoint="http://gateway",
            headers={},
            tts_type="ai_f5",
            result_payload={"selected_voice": "female_1", "duration": 1.5},
            volume_level=50.0,
        )


@pytest.mark.asyncio
async def test_synthesize_via_tts_service_rejects_unsuccessful_provider_payload(manager, monkeypatch):
    class _FakeResponse:
        def __init__(self, status: int, payload: dict):
            self.status = status
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return self._payload

        async def text(self):
            return str(self._payload)

    class _FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, **kwargs):
            _ = (url, kwargs)
            return _FakeResponse(200, {"success": False, "error": "worker failed"})

    monkeypatch.setattr(
        "services.tts.tts_manager.aiohttp.ClientSession",
        lambda timeout=None: _FakeClientSession(),
    )
    monkeypatch.setattr("services.tts.provider_utils.settings.tts_gateway_url", "http://gateway")
    monkeypatch.setattr("core.internal_service_auth.settings.tts_gateway_url", "http://gateway")
    monkeypatch.setattr("core.internal_service_auth.settings.tts_gateway_api_key", "gateway-key")

    result = await manager._synthesize_via_tts_service(
        channel_name="chan",
        text="hello",
        author="tester",
        user_id=1,
        volume_level=50.0,
        provider="f5",
        tts_settings={"advanced_provider": "f5", "voice": "female_1"},
    )

    assert result == {"success": False, "error": "worker failed"}


@pytest.mark.asyncio
async def test_qwen_local_health_check_uses_compat_probe(manager, monkeypatch):
    calls: list[str] = []

    class _FakeResponse:
        def __init__(self, status: int):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {}

    class _FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url: str, **kwargs):
            _ = kwargs
            calls.append(url)
            if url.endswith("/api/prepare"):
                return _FakeResponse(405)
            return _FakeResponse(404)

    monkeypatch.setattr(
        "services.tts.tts_manager.aiohttp.ClientSession",
        lambda timeout=None: _FakeClientSession(),
    )
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_hosts",
        "endpoint-qwen",
    )
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_cidrs",
        "",
    )

    result = await manager.check_tts_service_health(
        provider="qwen",
        endpoint_override="http://endpoint-qwen",
    )

    assert result is True
    assert "http://endpoint-qwen/api/prepare" in calls


@pytest.mark.asyncio
async def test_qwen_local_compat_synthesis_saves_audio(manager, monkeypatch):
    class _FakeResponse:
        def __init__(self, status: int, payload: dict | None = None, body: bytes = b""):
            self.status = status
            self._payload = payload or {}
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return self._payload

        async def text(self):
            return self._body.decode("utf-8", errors="ignore")

        async def read(self):
            return self._body

    class _FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url: str, **kwargs):
            _ = kwargs
            assert url.endswith("/api/prepare")
            return _FakeResponse(200, payload={"stream_id": "stream-1"})

        def get(self, url: str, **kwargs):
            _ = kwargs
            assert url.endswith("/api/stream/stream-1")
            return _FakeResponse(200, body=b"RIFFfake-wav")

    monkeypatch.setattr(
        "services.tts.tts_manager.aiohttp.ClientSession",
        lambda timeout=None: _FakeClientSession(),
    )
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_hosts",
        "endpoint-qwen",
    )
    monkeypatch.setattr(
        "services.tts.provider_utils.settings.local_tts_allowed_cidrs",
        "",
    )
    temp_root = Path("H:/Programming/raw_code/AI/Python/TTS_TTV_0.02/.pytest_tmp/qwen_local_compat")
    shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("services.tts.tts_manager.TEMP_DIR", temp_root)
    manager.basic_tts = SimpleNamespace(detect_language=lambda text: "ru", cleanup_old_files=lambda: None)

    try:
        result = await manager._synthesize_via_qwen_local_compat(
        channel_name="chan",
            text="Привет",
            author="tester",
            user_id=42,
            volume_level=50.0,
            tts_settings={"qwen_mode": "local"},
            tts_endpoint="http://endpoint-qwen",
            tts_endpoint_api_key=None,
        )

        assert result["success"] is True
        assert result["tts_type"] == "ai_qwen"
        assert result["audio_url"].endswith(".wav")
        assert (temp_root / "tts_audio").exists()
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
