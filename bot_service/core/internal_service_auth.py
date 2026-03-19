"""Helpers for bot_service -> TTS upstream authentication headers."""

from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional

from core.config import settings

logger = logging.getLogger(__name__)

TTSUpstreamKind = Literal["synthesis", "voice", "gateway", "local"]


class TTSAuthConfigError(RuntimeError):
    """Raised when strict TTS upstream auth is requested but key is missing."""


def _normalize_api_key(value: Optional[str]) -> str:
    return str(value or "").strip()


def _build_api_key_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
    }


def _normalize_provider(provider: str) -> str:
    candidate = (provider or "").strip().lower()
    if candidate == "qwen":
        return "qwen"
    return "f5"


def _provider_api_key_setting_name(provider: str) -> str:
    return "QWEN_TTS_SERVICE_API_KEY" if _normalize_provider(provider) == "qwen" else "F5_TTS_SERVICE_API_KEY"


def _resolve_gateway_api_key(*, strict: bool) -> str:
    api_key = _normalize_api_key(settings.tts_gateway_api_key)
    if api_key:
        return api_key
    if strict:
        raise TTSAuthConfigError(
            "TTS_GATEWAY_API_KEY is required for gateway upstream requests."
        )
    return ""


def _resolve_provider_api_key(provider: str, *, strict: bool) -> tuple[str, str]:
    normalized_provider = _normalize_provider(provider)
    if normalized_provider == "qwen":
        api_key = _normalize_api_key(settings.qwen_tts_service_api_key)
        if api_key:
            return api_key, "provider"
    else:
        api_key = _normalize_api_key(settings.f5_tts_service_api_key)
        if api_key:
            return api_key, "provider"

    api_key = _normalize_api_key(settings.tts_internal_api_key)
    if api_key:
        return api_key, "internal"

    api_key = _normalize_api_key(settings.tts_gateway_api_key)
    if api_key:
        return api_key, "gateway"

    if not api_key and strict:
        raise TTSAuthConfigError(
            (
                "QWEN_TTS_SERVICE_API_KEY is required for qwen upstream requests. "
                "Fallbacks: TTS_INTERNAL_API_KEY, TTS_GATEWAY_API_KEY."
            )
            if normalized_provider == "qwen"
            else (
                "F5_TTS_SERVICE_API_KEY is required for f5 upstream requests. "
                "Fallbacks: TTS_INTERNAL_API_KEY, TTS_GATEWAY_API_KEY."
            )
        )
    return "", "missing"


def build_tts_auth_headers(
    *,
    provider: str = "f5",
    upstream: TTSUpstreamKind = "voice",
    local_api_key: Optional[str] = None,
    use_gateway: Optional[bool] = None,
    strict: bool = True,
) -> Dict[str, str]:
    """
    Build strict API-key headers for bot_service -> TTS calls.

    Contract:
    - `Authorization: Bearer <key>`
    - `X-API-Key: <key>`
    """
    if upstream == "local" or local_api_key is not None:
        local_key = _normalize_api_key(local_api_key)
        if not local_key:
            # Local endpoints may intentionally run without auth.
            return {}
        return _build_api_key_headers(local_key)

    gateway_enabled = bool(_normalize_api_key(settings.tts_gateway_url))
    route_via_gateway = (
        upstream == "gateway"
        or (upstream == "synthesis" and (gateway_enabled if use_gateway is None else bool(use_gateway)))
    )

    if route_via_gateway:
        gateway_key = _resolve_gateway_api_key(strict=strict)
        return _build_api_key_headers(gateway_key) if gateway_key else {}

    provider_key, provider_key_source = _resolve_provider_api_key(provider, strict=strict)
    if provider_key and provider_key_source == "gateway":
        logger.warning(
            "Direct TTS upstream auth fell back to TTS_GATEWAY_API_KEY provider=%s upstream=%s. "
            "Configure %s or TTS_INTERNAL_API_KEY explicitly.",
            _normalize_provider(provider),
            upstream,
            _provider_api_key_setting_name(provider),
        )
    return _build_api_key_headers(provider_key) if provider_key else {}


def build_tts_httpx_client_kwargs() -> Dict[str, Any]:
    """
    Build optional httpx TLS kwargs for bot_service -> TTS internal calls.

    Uses mTLS settings only when explicitly enabled.
    """
    if not settings.internal_service_mtls_enabled:
        return {}

    kwargs: Dict[str, Any] = {}

    if settings.internal_service_ca_cert_path:
        kwargs["verify"] = settings.internal_service_ca_cert_path
    else:
        kwargs["verify"] = True

    cert_path = settings.internal_service_client_cert_path
    key_path = settings.internal_service_client_key_path
    if cert_path and key_path:
        kwargs["cert"] = (cert_path, key_path)
    elif cert_path:
        kwargs["cert"] = cert_path
    else:
        logger.warning(
            "INTERNAL_SERVICE_MTLS_ENABLED=true but client cert is not configured; continuing without mTLS cert"
        )

    return kwargs
