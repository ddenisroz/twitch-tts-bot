import pytest

from core import internal_service_auth


def test_build_tts_auth_headers_gateway_strict(monkeypatch):
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_url", "http://gateway:8010")
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_api_key", "gateway-key")

    headers = internal_service_auth.build_tts_auth_headers(
        provider="qwen",
        upstream="synthesis",
        use_gateway=True,
        strict=True,
    )

    assert headers["Authorization"] == "Bearer gateway-key"
    assert headers["X-API-Key"] == "gateway-key"


def test_build_tts_auth_headers_gateway_missing_key_raises(monkeypatch):
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_url", "http://gateway:8010")
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_api_key", "")

    with pytest.raises(internal_service_auth.TTSAuthConfigError, match="TTS_GATEWAY_API_KEY"):
        internal_service_auth.build_tts_auth_headers(
            provider="qwen",
            upstream="synthesis",
            use_gateway=True,
            strict=True,
        )


def test_build_tts_auth_headers_provider_f5_strict(monkeypatch):
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_url", "")
    monkeypatch.setattr(internal_service_auth.settings, "f5_tts_service_api_key", "f5-key")
    monkeypatch.setattr(internal_service_auth.settings, "tts_internal_api_key", "shared-key")
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_api_key", "gateway-key")

    headers = internal_service_auth.build_tts_auth_headers(
        provider="f5",
        upstream="voice",
        strict=True,
    )

    assert headers["Authorization"] == "Bearer f5-key"
    assert headers["X-API-Key"] == "f5-key"


def test_build_tts_auth_headers_provider_qwen_strict(monkeypatch):
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_url", "")
    monkeypatch.setattr(internal_service_auth.settings, "qwen_tts_service_api_key", "qwen-key")
    monkeypatch.setattr(internal_service_auth.settings, "tts_internal_api_key", "shared-key")
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_api_key", "gateway-key")

    headers = internal_service_auth.build_tts_auth_headers(
        provider="qwen",
        upstream="voice",
        strict=True,
    )

    assert headers["Authorization"] == "Bearer qwen-key"
    assert headers["X-API-Key"] == "qwen-key"


def test_build_tts_auth_headers_provider_qwen_falls_back_to_shared_key(monkeypatch, caplog):
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_url", "")
    monkeypatch.setattr(internal_service_auth.settings, "qwen_tts_service_api_key", "")
    monkeypatch.setattr(internal_service_auth.settings, "tts_internal_api_key", "shared-key")
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_api_key", "gateway-key")

    headers = internal_service_auth.build_tts_auth_headers(
        provider="qwen",
        upstream="voice",
        strict=True,
    )

    assert headers["Authorization"] == "Bearer shared-key"
    assert headers["X-API-Key"] == "shared-key"
    assert "fell back to TTS_GATEWAY_API_KEY" not in caplog.text


def test_build_tts_auth_headers_provider_f5_falls_back_to_gateway_key(monkeypatch, caplog):
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_url", "")
    monkeypatch.setattr(internal_service_auth.settings, "f5_tts_service_api_key", "")
    monkeypatch.setattr(internal_service_auth.settings, "tts_internal_api_key", "")
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_api_key", "gateway-key")

    headers = internal_service_auth.build_tts_auth_headers(
        provider="f5",
        upstream="voice",
        strict=True,
    )

    assert headers["Authorization"] == "Bearer gateway-key"
    assert headers["X-API-Key"] == "gateway-key"
    assert "fell back to TTS_GATEWAY_API_KEY" in caplog.text
    assert "F5_TTS_SERVICE_API_KEY" in caplog.text


def test_build_tts_auth_headers_provider_qwen_falls_back_to_gateway_key_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_url", "")
    monkeypatch.setattr(internal_service_auth.settings, "qwen_tts_service_api_key", "")
    monkeypatch.setattr(internal_service_auth.settings, "tts_internal_api_key", "")
    monkeypatch.setattr(internal_service_auth.settings, "tts_gateway_api_key", "gateway-key")

    headers = internal_service_auth.build_tts_auth_headers(
        provider="qwen",
        upstream="voice",
        strict=True,
    )

    assert headers["Authorization"] == "Bearer gateway-key"
    assert headers["X-API-Key"] == "gateway-key"
    assert "fell back to TTS_GATEWAY_API_KEY" in caplog.text
    assert "QWEN_TTS_SERVICE_API_KEY" in caplog.text


def test_build_tts_auth_headers_local_with_saved_key():
    headers = internal_service_auth.build_tts_auth_headers(
        provider="f5",
        upstream="local",
        local_api_key="local-key",
        strict=False,
    )

    assert headers["Authorization"] == "Bearer local-key"
    assert headers["X-API-Key"] == "local-key"


def test_build_tts_auth_headers_local_without_key_returns_empty():
    headers = internal_service_auth.build_tts_auth_headers(
        provider="f5",
        upstream="local",
        local_api_key="",
        strict=False,
    )
    assert headers == {}
