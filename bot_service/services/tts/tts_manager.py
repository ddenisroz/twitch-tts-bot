#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Provider-aware TTS manager for bot_service.

Priority order:
1. Advanced providers (F5/Qwen/GCloud) based on current settings.
2. Basic gTTS as always-on fallback.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import aiohttp

from constants import (
    TTS_DEFAULT_VOLUME,
    TTS_HEALTH_CHECK_INTERVAL,
    TTS_MAX_RETRIES,
    TTS_RETRY_DELAY,
)
from core.config import settings
from core.internal_service_auth import TTSAuthConfigError, build_tts_auth_headers
from core.project_paths import TEMP_DIR
from services.tts.basic_tts import get_basic_tts
from services.tts.google_cloud_tts import (
    get_google_cloud_tts,
    is_gemini_or_chirp_voice,
    normalize_gcloud_mood,
)
from services.tts.provider_utils import (
    ProviderRoutingError,
    QWEN_BASE_MODEL,
    QWEN_CUSTOMVOICE_MODEL,
    QWEN_DEFAULT_MODEL,
    QWEN_VOICEDESIGN_MODEL,
    get_provider_service_url,
    get_qwen_model_family,
    get_synthesis_upstream_params,
    get_synthesis_upstream_url,
    infer_provider_from_engine,
    normalize_local_tts_endpoint_url,
    normalize_provider,
    normalize_provider_mode,
    normalize_qwen_model_selection,
    should_route_provider_via_gateway,
)

logger = logging.getLogger(__name__)
_GEMINI_SPEAKER_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9_]{1,63}$")
_QWEN_LOCAL_DEFAULT_MODEL = QWEN_DEFAULT_MODEL
_QWEN_LOCAL_DEFAULT_SPEAKER = "serena"
_QWEN_LOCAL_DEFAULT_INSTRUCTION = "Neutral natural voice."
_QWEN_LOCAL_MODEL_ALIASES = {
    "0.6b-customvoice": QWEN_CUSTOMVOICE_MODEL,
    "1.7b-customvoice": QWEN_CUSTOMVOICE_MODEL,
    "1.7b-voicedesign": QWEN_VOICEDESIGN_MODEL,
    "0.6b-base": QWEN_BASE_MODEL,
    "1.7b-base": QWEN_BASE_MODEL,
    "customvoice": QWEN_CUSTOMVOICE_MODEL,
    "voicedesign": QWEN_VOICEDESIGN_MODEL,
    "base": QWEN_BASE_MODEL,
}


def _gcloud_voice_quality_rank(voice_name: Optional[str]) -> int:
    key = (voice_name or "").lower()
    if _GEMINI_SPEAKER_PATTERN.match(voice_name or ""):
        return 0
    if "gemini" in key:
        return 0
    if "chirp3-hd" in key:
        return 1
    if "neural2" in key:
        return 2
    if "wavenet" in key:
        return 3
    if "studio" in key:
        return 4
    if "journey" in key:
        return 5
    if "standard" in key:
        return 9
    return 6


class TTSManager:
    """Coordinates provider synthesis and fallback to basic TTS."""

    def __init__(self):
        from core import config as config_module

        current_settings = config_module.settings
        self.f5_tts_service_url = current_settings.f5_tts_service_url
        self.qwen_tts_service_url = current_settings.qwen_tts_service_url
        self.backend_url = settings.backend_url

        self.basic_tts = get_basic_tts()
        self.google_cloud_tts = get_google_cloud_tts()

        # Cache health per effective endpoint to avoid cross-endpoint poisoning:
        # a failing local endpoint must not mark the cloud endpoint as unhealthy.
        self._provider_health: Dict[tuple[str, str], bool] = {}
        self._provider_last_health_check: Dict[tuple[str, str], float] = {}
        self._health_check_interval = TTS_HEALTH_CHECK_INTERVAL

        logger.info(
            "[OK] TTS manager initialized: f5_url=%s qwen_url=%s",
            self.f5_tts_service_url,
            self.qwen_tts_service_url,
        )

    async def get_user_tts_endpoint(
        self,
        user_id: int,
        db_session,
        provider: str = "f5",
    ) -> Optional[Dict[str, Optional[str]]]:
        """Return healthy local endpoint payload for provider if configured."""
        try:
            from repositories.local_tts_repository import LocalTTSRepository

            repo = LocalTTSRepository(db_session)
            normalized_provider = normalize_provider(provider)
            local_config = repo.get_healthy(user_id=user_id, provider=normalized_provider)

            if local_config:
                try:
                    normalized_endpoint = normalize_local_tts_endpoint_url(local_config.endpoint_url)
                except ValueError as error:
                    logger.warning(
                        "[WARN] Ignoring invalid local endpoint for user_id=%s provider=%s: %s",
                        user_id,
                        normalized_provider,
                        error,
                    )
                    return None

                logger.info(
                    "[LOCAL] Using local endpoint for user_id=%s provider=%s endpoint=%s",
                    user_id,
                    normalized_provider,
                    normalized_endpoint,
                )
                return {
                    "endpoint_url": normalized_endpoint,
                    "api_key": str(local_config.api_key or "").strip() or None,
                }

            return None
        except Exception:
            logger.exception("Error getting user local TTS endpoint")
            return None

    def _normalize_qwen_local_model(self, raw_model: Optional[str]) -> str:
        candidate = str(raw_model or "").strip()
        if not candidate or candidate.lower() == "default":
            return _QWEN_LOCAL_DEFAULT_MODEL

        normalized_selection = normalize_qwen_model_selection(candidate)
        if normalized_selection in {QWEN_BASE_MODEL, QWEN_VOICEDESIGN_MODEL, QWEN_CUSTOMVOICE_MODEL}:
            return normalized_selection

        normalized = candidate.lower().replace("_", "").replace(" ", "").replace("/", "")
        for alias, resolved in _QWEN_LOCAL_MODEL_ALIASES.items():
            compact_alias = alias.lower().replace("_", "").replace(" ", "").replace("/", "")
            if normalized == compact_alias:
                return resolved

        return candidate

    def _normalize_qwen_local_speaker(self, raw_speaker: Optional[str]) -> str:
        candidate = str(raw_speaker or "").strip()
        if not candidate or candidate.lower() == "default":
            return _QWEN_LOCAL_DEFAULT_SPEAKER
        return candidate

    def _build_qwen_local_prepare_payload(
        self,
        *,
        channel_name: str,
        text: str,
        author: str,
        user_id: Optional[int],
        tts_settings: Optional[Dict[str, Any]],
    ) -> tuple[aiohttp.FormData, str]:
        request_settings = dict(tts_settings or {})

        model = self._normalize_qwen_local_model(request_settings.get("qwen_model"))
        qwen_voice_value = str(
            request_settings.get("qwen_voice") or request_settings.get("voice") or ""
        ).strip()
        speaker = self._normalize_qwen_local_speaker(qwen_voice_value)
        language = "Russian" if self.basic_tts.detect_language(text) == "ru" else "English"

        raw_temperature = request_settings.get("qwen_temperature", request_settings.get("temperature", 0.9))
        try:
            temperature = float(raw_temperature)
        except (TypeError, ValueError):
            temperature = 0.9
        if temperature <= 0:
            temperature = 0.9

        instruction = ""
        if get_qwen_model_family(model) != "base":
            raw_instruction = str(request_settings.get("qwen_instruction") or qwen_voice_value).strip()
            instruction = raw_instruction or _QWEN_LOCAL_DEFAULT_INSTRUCTION
            speaker = ""

        tenant_id = f"user:{user_id}" if user_id else f"channel:{channel_name.lower()}" if channel_name else "bot_service"

        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field("text", text)
        form.add_field("language", language)
        form.add_field("temperature", str(temperature))
        form.add_field("instruction", instruction)
        form.add_field("speaker", speaker)
        form.add_field("tenant_id", tenant_id)
        form.add_field("channel_name", channel_name or "")
        form.add_field("author", author or "")
        form.add_field("user_id", str(user_id or ""))

        selected_voice = instruction if instruction else speaker or _QWEN_LOCAL_DEFAULT_SPEAKER
        return form, selected_voice

    async def _check_qwen_local_compat_health(
        self,
        *,
        session: aiohttp.ClientSession,
        endpoint: str,
        headers: Dict[str, str],
    ) -> bool:
        probe_specs = (
            ("/health/ready", {200}),
            ("/health/live", {200}),
            ("/api/models", {200}),
            ("/api/prepare", {405, 422}),
            ("/api/status/__healthcheck__", {200, 404}),
            ("/", {200}),
        )

        for path, expected_statuses in probe_specs:
            try:
                async with session.get(f"{endpoint}{path}", headers=headers) as response:
                    if response.status in expected_statuses:
                        return True
            except aiohttp.ClientError:
                continue

        return False

    async def _fetch_qwen_local_status(
        self,
        *,
        endpoint: str,
        headers: Dict[str, str],
        stream_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not stream_id:
            return None

        timeout = aiohttp.ClientTimeout(total=5, connect=2, sock_read=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{endpoint}/api/status/{stream_id}", headers=headers) as response:
                    payload: Dict[str, Any]
                    try:
                        payload = await response.json()
                    except Exception:
                        payload = {"detail": await response.text()}
                    payload["http_status"] = response.status
                    return payload
        except Exception as error:
            return {"error": str(error)}

    async def _cancel_qwen_local_stream(
        self,
        *,
        endpoint: str,
        headers: Dict[str, str],
        stream_id: Optional[str],
    ) -> Optional[int]:
        if not stream_id:
            return None

        timeout = aiohttp.ClientTimeout(total=5, connect=2, sock_read=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{endpoint}/api/cancel/{stream_id}", headers=headers) as response:
                    return response.status
        except Exception as error:
            logger.warning(
                "[WARN] Failed to cancel qwen local stream endpoint=%s stream_id=%s error=%s",
                endpoint,
                stream_id,
                error,
            )
            return None

    async def _synthesize_via_qwen_local_compat(
        self,
        *,
        channel_name: str,
        text: str,
        author: str,
        user_id: Optional[int],
        volume_level: float,
        tts_settings: Optional[Dict[str, Any]],
        tts_endpoint: str,
        tts_endpoint_api_key: Optional[str],
    ) -> Dict[str, Any]:
        try:
            endpoint = normalize_local_tts_endpoint_url(tts_endpoint).rstrip("/")
        except ValueError as error:
            logger.warning("[WARN] Invalid qwen local endpoint during synthesis: %s", error)
            return {"success": False, "error": "Invalid local endpoint configuration"}

        headers = build_tts_auth_headers(
            provider="qwen",
            upstream="local",
            local_api_key=tts_endpoint_api_key,
            strict=False,
        )
        form, selected_voice = self._build_qwen_local_prepare_payload(
            channel_name=channel_name,
            text=text,
            author=author,
            user_id=user_id,
            tts_settings=tts_settings,
        )

        timeout = aiohttp.ClientTimeout(total=90, connect=10, sock_read=90)
        stream_id: Optional[str] = None
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{endpoint}/api/prepare", data=form, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(
                            "[ERROR] Qwen local prepare failed status=%s body=%s endpoint=%s",
                            response.status,
                            error_text,
                            endpoint,
                        )
                        return {"success": False, "error": f"Qwen local prepare failed: {response.status}"}

                    prepare_payload = await response.json()
                    stream_id = str(prepare_payload.get("stream_id") or "").strip()
                    if not stream_id:
                        return {"success": False, "error": "Qwen local prepare returned no stream_id"}

                async with session.get(f"{endpoint}/api/stream/{stream_id}", headers=headers) as stream_response:
                    if stream_response.status != 200:
                        error_text = await stream_response.text()
                        logger.error(
                            "[ERROR] Qwen local stream failed status=%s body=%s endpoint=%s stream_id=%s",
                            stream_response.status,
                            error_text,
                            endpoint,
                            stream_id,
                        )
                        return {"success": False, "error": f"Qwen local stream failed: {stream_response.status}"}

                    audio_bytes = await stream_response.read()

            if not audio_bytes:
                return {"success": False, "error": "Qwen local stream returned empty audio"}

            output_dir = TEMP_DIR / "tts_audio"
            output_dir.mkdir(parents=True, exist_ok=True)
            filename = f"qwen_local_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.wav"
            output_path = output_dir / filename
            await asyncio.to_thread(output_path.write_bytes, audio_bytes)

            return {
                "success": True,
                "voice": selected_voice,
                "volume": volume_level,
                "tts_type": "ai_qwen",
                "audio_url": f"{self.backend_url}/api/tts/audio/{filename}",
                "audio_path": str(output_path.resolve()),
            }
        except asyncio.TimeoutError:
            status_payload = await self._fetch_qwen_local_status(
                endpoint=endpoint,
                headers=headers,
                stream_id=stream_id,
            )
            cancel_status = await self._cancel_qwen_local_stream(
                endpoint=endpoint,
                headers=headers,
                stream_id=stream_id,
            )
            logger.warning(
                "[WARN] Qwen local compatibility synthesis timeout endpoint=%s stream_id=%s status_payload=%s cancel_status=%s",
                endpoint,
                stream_id or "-",
                status_payload,
                cancel_status,
            )
            return {"success": False, "error": "Request timeout"}
        except aiohttp.ClientError as error:
            logger.warning("[WARN] Qwen local compatibility synthesis connection error: %s", error)
            return {"success": False, "error": f"Connection error: {error}"}
        except Exception:
            logger.exception("[ERROR] Qwen local compatibility synthesis failed")
            return {"success": False, "error": "Internal server error"}

    def _resolve_provider_audio_fetch_headers(
        self,
        *,
        provider: str,
        endpoint: str,
        resolved_audio_url: str,
        headers: Dict[str, str],
    ) -> Dict[str, str]:
        resolved_netloc = urlparse(resolved_audio_url).netloc.strip().lower()
        endpoint_netloc = urlparse(endpoint).netloc.strip().lower()
        if not resolved_netloc or resolved_netloc == endpoint_netloc:
            return headers

        provider_service_url = get_provider_service_url(provider).rstrip("/")
        provider_service_netloc = urlparse(provider_service_url).netloc.strip().lower()
        if resolved_netloc == provider_service_netloc:
            provider_headers = build_tts_auth_headers(
                provider=provider,
                upstream="voice",
                strict=False,
            )
            if provider_headers:
                return provider_headers

        gateway_url = str(getattr(settings, "tts_gateway_url", "") or "").strip().rstrip("/")
        gateway_netloc = urlparse(gateway_url).netloc.strip().lower()
        if resolved_netloc == gateway_netloc:
            gateway_headers = build_tts_auth_headers(
                provider=provider,
                upstream="synthesis",
                use_gateway=True,
                strict=False,
            )
            if gateway_headers:
                return gateway_headers

        return headers

    async def check_tts_service_health(
        self,
        force_check: bool = False,
        provider: str = "f5",
        endpoint_override: Optional[str] = None,
        endpoint_api_key: Optional[str] = None,
    ) -> bool:
        """Health check for remote/local provider endpoint with endpoint-aware cache."""
        normalized_provider = normalize_provider(provider)
        use_gateway = False
        if endpoint_override:
            try:
                endpoint = normalize_local_tts_endpoint_url(endpoint_override)
            except ValueError as error:
                if not force_check:
                    logger.warning(
                        "[WARN] Invalid endpoint override for health check provider=%s error=%s",
                        normalized_provider,
                        error,
                    )
                return False
            request_headers = build_tts_auth_headers(
                provider=normalized_provider,
                upstream="local",
                local_api_key=endpoint_api_key,
                strict=False,
            )
            request_params: Dict[str, str] = {}
        else:
            try:
                endpoint = get_synthesis_upstream_url(normalized_provider).rstrip("/")
            except ProviderRoutingError as error:
                if not force_check:
                    if str(error) == "qwen_gateway_required":
                        logger.warning(
                            "[WARN] Qwen health check skipped: qwen synthesis requires configured gateway."
                        )
                    else:
                        logger.warning(
                            "[WARN] %s health check routing error: %s",
                            normalized_provider,
                            error,
                        )
                cache_key = (normalized_provider, "routing_error")
                self._provider_health[cache_key] = False
                self._provider_last_health_check[cache_key] = time.time()
                return False

            use_gateway = should_route_provider_via_gateway(normalized_provider)
            try:
                request_headers = build_tts_auth_headers(
                    provider=normalized_provider,
                    upstream="synthesis",
                    use_gateway=use_gateway,
                    strict=True,
                )
            except TTSAuthConfigError as error:
                if not force_check:
                    logger.warning(
                        "[WARN] %s health check auth configuration error: %s",
                        normalized_provider,
                        error,
                    )
                cache_key = (normalized_provider, endpoint)
                self._provider_health[cache_key] = False
                self._provider_last_health_check[cache_key] = time.time()
                return False

            request_params = (
                get_synthesis_upstream_params(normalized_provider)
                if use_gateway
                else {}
            )
        cache_key = (normalized_provider, endpoint)
        current_time = time.time()
        last_check = self._provider_last_health_check.get(cache_key, 0.0)

        if (not force_check) and (current_time - last_check < self._health_check_interval):
            return self._provider_health.get(cache_key, True)

        try:
            timeout = aiohttp.ClientTimeout(total=5, connect=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if endpoint_override and normalized_provider == "qwen":
                    is_healthy = await self._check_qwen_local_compat_health(
                        session=session,
                        endpoint=endpoint,
                        headers=request_headers,
                    )
                    self._provider_health[cache_key] = is_healthy
                    self._provider_last_health_check[cache_key] = current_time
                    return is_healthy

                health_payload = None
                last_status = None
                health_paths = (
                    ("/health/ready", "/api/health", "/health")
                    if use_gateway
                    else ("/api/health", "/health", "/health/ready")
                )
                for health_path in health_paths:
                    async with session.get(
                        f"{endpoint}{health_path}",
                        headers=request_headers,
                        params=request_params,
                    ) as response:
                        last_status = response.status
                        if response.status != 200:
                            continue
                        try:
                            health_payload = await response.json()
                        except Exception:
                            health_payload = {"status": "healthy"}
                        break

                if health_payload is None:
                    if not force_check:
                        logger.warning(
                            "[WARN] %s health check failed status=%s endpoint=%s",
                            normalized_provider,
                            last_status,
                            endpoint,
                        )
                    self._provider_health[cache_key] = False
                    self._provider_last_health_check[cache_key] = current_time
                    return False

                status_value = str(health_payload.get("status") or "").strip().lower()
                tts_engine_value = str(health_payload.get("tts_engine") or "").strip().lower()
                redis_state = str(health_payload.get("redis") or "").strip().lower()
                scheduler_state = str(health_payload.get("scheduler") or "").strip().lower()
                ready_flag = health_payload.get("ready")

                is_healthy = bool(
                    health_payload.get("tts_engine_loaded", False)
                    or status_value in {"healthy", "ok", "ready"}
                    or tts_engine_value == "ready"
                    or ready_flag is True
                )

                if use_gateway and status_value == "ok":
                    is_healthy = redis_state != "down" and scheduler_state != "stopped"

                previous_state = self._provider_health.get(cache_key)
                if previous_state is not None and previous_state != is_healthy:
                    if is_healthy:
                        logger.info("[OK] %s service is healthy again", normalized_provider)
                    else:
                        logger.warning("[WARN] %s service is unhealthy", normalized_provider)

                self._provider_health[cache_key] = is_healthy
                self._provider_last_health_check[cache_key] = current_time
                return is_healthy

        except asyncio.TimeoutError:
            if not force_check:
                logger.warning("[WARN] %s health check timeout endpoint=%s", normalized_provider, endpoint)
        except aiohttp.ClientError as error:
            if not force_check:
                logger.warning(
                    "[WARN] %s health check connection error endpoint=%s error=%s",
                    normalized_provider,
                    endpoint,
                    error,
                )
        except Exception:
            if not force_check:
                logger.exception("[ERROR] %s health check failed endpoint=%s", normalized_provider, endpoint)

        self._provider_health[cache_key] = False
        self._provider_last_health_check[cache_key] = current_time
        return False

    @staticmethod
    def _enrich_result(
        result: Dict[str, Any],
        *,
        requested_provider: str,
        actual_provider: str,
        fallback_used: bool,
        fallback_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = dict(result or {})
        payload["requested_provider"] = requested_provider
        payload["actual_provider"] = actual_provider
        payload["fallback_used"] = bool(fallback_used)
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        elif "fallback_reason" in payload:
            payload.pop("fallback_reason", None)
        return payload

    async def _persist_audio_bytes(
        self,
        *,
        audio_bytes: bytes,
        provider: str,
        source_url: Optional[str],
        content_type: Optional[str],
    ) -> Dict[str, str]:
        suffix = Path(urlparse(str(source_url or "")).path).suffix.lower()
        if suffix not in {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".aiff", ".au", ".wma"}:
            normalized_content_type = str(content_type or "").lower()
            if "mpeg" in normalized_content_type or "mp3" in normalized_content_type:
                suffix = ".mp3"
            elif "ogg" in normalized_content_type:
                suffix = ".ogg"
            else:
                suffix = ".wav"

        output_dir = TEMP_DIR / "tts_audio"
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{provider}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{suffix}"
        output_path = output_dir / filename
        await asyncio.to_thread(output_path.write_bytes, audio_bytes)
        return {
            "audio_url": f"{self.backend_url}/api/tts/audio/{filename}",
            "audio_path": str(output_path.resolve()),
        }

    async def _materialize_provider_audio(
        self,
        *,
        session: aiohttp.ClientSession,
        provider: str,
        audio_url: Optional[str],
        endpoint: str,
        headers: Dict[str, str],
    ) -> Dict[str, Optional[str]]:
        raw_audio_url = str(audio_url or "").strip()
        if not raw_audio_url:
            return {"audio_url": None, "audio_path": None}

        if raw_audio_url.startswith(self.backend_url):
            return {"audio_url": raw_audio_url, "audio_path": None}

        if raw_audio_url.startswith(("http://", "https://")):
            resolved_audio_url = raw_audio_url
        elif raw_audio_url.startswith("/"):
            resolved_audio_url = f"{endpoint}{raw_audio_url}"
        else:
            resolved_audio_url = f"{endpoint}/api/tts/audio/{raw_audio_url}"

        if resolved_audio_url.startswith(self.backend_url):
            return {"audio_url": resolved_audio_url, "audio_path": None}

        fetch_headers = self._resolve_provider_audio_fetch_headers(
            provider=provider,
            endpoint=endpoint,
            resolved_audio_url=resolved_audio_url,
            headers=headers,
        )

        async with session.get(resolved_audio_url, headers=fetch_headers) as audio_response:
            if audio_response.status != 200:
                body = await audio_response.text()
                raise RuntimeError(
                    f"Provider audio fetch failed provider={provider} status={audio_response.status} body={body[:200]}"
                )
            audio_bytes = await audio_response.read()
            if not audio_bytes:
                raise RuntimeError(f"Provider audio fetch returned empty payload provider={provider}")
            localized = await self._persist_audio_bytes(
                audio_bytes=audio_bytes,
                provider=provider,
                source_url=resolved_audio_url,
                content_type=audio_response.headers.get("content-type"),
            )
            return localized

    async def _build_provider_success_result(
        self,
        *,
        session: aiohttp.ClientSession,
        provider: str,
        endpoint: str,
        headers: Dict[str, str],
        tts_type: str,
        result_payload: Dict[str, Any],
        volume_level: float,
    ) -> Dict[str, Any]:
        if result_payload.get("success") is False:
            upstream_error = str(
                result_payload.get("error")
                or result_payload.get("detail")
                or "Provider returned unsuccessful payload"
            ).strip()
            raise RuntimeError(
                f"Provider returned unsuccessful payload provider={provider} error={upstream_error}"
            )

        raw_audio_url = str(result_payload.get("audio_url") or "").strip()
        if not raw_audio_url:
            upstream_error = str(result_payload.get("error") or result_payload.get("detail") or "").strip()
            raise RuntimeError(
                "Provider success payload missing audio_url "
                f"provider={provider} error={upstream_error or '-'} keys={sorted(result_payload.keys())}"
            )

        selected_voice = result_payload.get("selected_voice") or result_payload.get("voice")
        localized_audio = await self._materialize_provider_audio(
            session=session,
            provider=provider,
            audio_url=raw_audio_url,
            endpoint=endpoint,
            headers=headers,
        )
        return {
            "success": True,
            "voice": selected_voice,
            "volume": volume_level,
            "tts_type": tts_type,
            "audio_url": localized_audio.get("audio_url"),
            "audio_path": localized_audio.get("audio_path"),
            "duration": result_payload.get("duration"),
            "spoken_text": result_payload.get("spoken_text"),
        }

    async def synthesize_tts(
        self,
        channel_name: str,
        text: str,
        author: str,
        user_id: int = None,
        volume_level: float = TTS_DEFAULT_VOLUME,
        use_ai_tts: bool = False,
        use_basic_tts: bool = True,
        connection_manager=None,
        tts_settings: dict = None,
        word_filter: list = None,
        blocked_users: list = None,
        db_session=None,
        engine: Optional[str] = None,
    ) -> Dict:
        """Synthesize speech with provider-first routing and explicit fallback metadata."""

        settings_dict = tts_settings or {}
        resolved_engine = (engine or ("f5tts" if use_ai_tts else "gtts")).strip().lower()
        logger.info("[MIC] Engine resolved: %s", resolved_engine)
        requested_provider = "gtts"
        fallback_reason: Optional[str] = None

        if resolved_engine in {"f5tts", "qwen"}:
            requested_provider = infer_provider_from_engine(
                resolved_engine,
                advanced_provider=settings_dict.get("advanced_provider"),
            )
        elif resolved_engine == "gcloud":
            requested_provider = "gcloud"

        # Priority A: Google Cloud TTS
        if resolved_engine == "gcloud":
            try:
                result = await self._synthesize_via_google_cloud_tts(
                    text=text,
                    volume_level=volume_level,
                    tts_settings=settings_dict,
                )
                if result.get("success"):
                    logger.info("[OK] Google Cloud TTS synthesis succeeded")
                    self.cleanup_old_files_if_needed()
                    return self._enrich_result(
                        result,
                        requested_provider="gcloud",
                        actual_provider="gcloud",
                        fallback_used=False,
                    )
                fallback_reason = f"gcloud_error:{result.get('error') or 'unknown'}"
                logger.warning("[WARN] Google Cloud TTS failed: %s", result.get("error"))
            except Exception:
                fallback_reason = "gcloud_exception"
                logger.exception("[ERROR] Google Cloud TTS execution failed")

        # Priority B: Advanced providers (F5/Qwen) with retries
        elif resolved_engine in {"f5tts", "qwen"} and use_ai_tts:
            provider = requested_provider
            provider_mode_key = "qwen_mode" if provider == "qwen" else "f5_mode"
            preferred_mode = normalize_provider_mode(settings_dict.get(provider_mode_key))
            has_explicit_provider_mode = provider_mode_key in settings_dict and settings_dict.get(provider_mode_key) is not None
            if has_explicit_provider_mode:
                resolved_mode = preferred_mode
            else:
                resolved_mode = "local" if bool(settings_dict.get("use_local_tts", False)) else preferred_mode

            endpoint = get_provider_service_url(provider)
            endpoint_api_key: Optional[str] = None
            has_explicit_local_endpoint = False

            if resolved_mode == "local":
                if user_id and db_session:
                    local_endpoint_payload = await self.get_user_tts_endpoint(
                        user_id=user_id,
                        db_session=db_session,
                        provider=provider,
                    )
                    if local_endpoint_payload:
                        endpoint = str(local_endpoint_payload.get("endpoint_url") or endpoint)
                        endpoint_api_key = local_endpoint_payload.get("api_key")
                        has_explicit_local_endpoint = True
                if not has_explicit_local_endpoint:
                    logger.warning(
                        "[WARN] No local endpoint configured for provider=%s user_id=%s; fallback to basic TTS",
                        provider,
                        user_id,
                    )
                    fallback_reason = f"{provider}_local_endpoint_not_configured"

            if resolved_mode != "local" and provider == "qwen" and (not should_route_provider_via_gateway(provider)):
                logger.warning(
                    "[WARN] Qwen synthesis requires configured gateway; fallback to basic TTS"
                )
                resolved_mode = "cloud_gateway_unavailable"
                fallback_reason = "qwen_gateway_not_configured"

            if resolved_mode != "local" or has_explicit_local_endpoint:
                max_retries = TTS_MAX_RETRIES
                base_retry_delay = TTS_RETRY_DELAY

                is_healthy = await self.check_tts_service_health(
                    provider=provider,
                    endpoint_override=endpoint if has_explicit_local_endpoint else None,
                    endpoint_api_key=endpoint_api_key,
                )
                if not is_healthy:
                    logger.warning(
                        "[WARN] Provider unhealthy provider=%s endpoint=%s; fallback to basic TTS",
                        provider,
                        endpoint,
                    )
                    fallback_reason = f"{provider}_unhealthy"
                else:
                    last_advanced_error: Optional[str] = None
                    for attempt in range(1, max_retries + 1):
                        try:
                            result = await self._synthesize_via_tts_service(
                                channel_name=channel_name,
                                text=text,
                                author=author,
                                user_id=user_id,
                                volume_level=volume_level,
                                connection_manager=connection_manager,
                                tts_settings=settings_dict,
                                word_filter=word_filter,
                                blocked_users=blocked_users,
                                provider=provider,
                                tts_endpoint=endpoint if has_explicit_local_endpoint else None,
                                tts_endpoint_api_key=endpoint_api_key,
                            )
                            if result.get("success"):
                                logger.info(
                                    "[OK] Advanced synthesis succeeded provider=%s attempt=%s/%s",
                                    provider,
                                    attempt,
                                    max_retries,
                                )
                                self.cleanup_old_files_if_needed()
                                return self._enrich_result(
                                    result,
                                    requested_provider=provider,
                                    actual_provider=provider,
                                    fallback_used=False,
                                )

                            last_advanced_error = str(result.get("error") or "provider_failed")
                            logger.warning(
                                "[WARN] Advanced synthesis failed provider=%s attempt=%s/%s error=%s",
                                provider,
                                attempt,
                                max_retries,
                                result.get("error"),
                            )
                        except asyncio.TimeoutError:
                            last_advanced_error = "timeout"
                            logger.warning(
                                "[WARN] Advanced synthesis timeout provider=%s attempt=%s/%s",
                                provider,
                                attempt,
                                max_retries,
                            )
                        except aiohttp.ClientError as error:
                            last_advanced_error = f"connection:{error}"
                            logger.warning(
                                "[WARN] Advanced synthesis connection error provider=%s attempt=%s/%s error=%s",
                                provider,
                                attempt,
                                max_retries,
                                error,
                            )
                        except Exception:
                            last_advanced_error = "exception"
                            logger.exception(
                                "[ERROR] Advanced synthesis exception provider=%s attempt=%s/%s",
                                provider,
                                attempt,
                                max_retries,
                            )

                        if attempt < max_retries:
                            delay = base_retry_delay * (2 ** (attempt - 1))
                            await asyncio.sleep(delay)

                    logger.warning(
                        "[WARN] Advanced provider exhausted retries provider=%s; fallback to basic TTS",
                        provider,
                    )
                    fallback_reason = f"{provider}_failed:{last_advanced_error or 'unknown'}"
        elif resolved_engine in {"f5tts", "qwen"}:
            fallback_reason = f"{requested_provider}_disabled"

        # Priority C: Basic TTS or explicit fallback.
        if resolved_engine != "gtts" and not use_basic_tts:
            return self._enrich_result(
                {"success": False, "error": fallback_reason or "Advanced provider failed with fallback disabled"},
                requested_provider=requested_provider,
                actual_provider=requested_provider,
                fallback_used=False,
                fallback_reason=fallback_reason,
            )

        try:
            result = await self._synthesize_via_basic_tts(text, volume_level)
            if result.get("success"):
                logger.info("[OK] Basic TTS synthesis succeeded")
                self.cleanup_old_files_if_needed()
                return self._enrich_result(
                    result,
                    requested_provider=requested_provider,
                    actual_provider="gtts",
                    fallback_used=requested_provider != "gtts",
                    fallback_reason=fallback_reason,
                )
            logger.error("[ERROR] Basic TTS synthesis failed: %s", result.get("error"))
            return self._enrich_result(
                {"success": False, "error": "Basic TTS synthesis failed"},
                requested_provider=requested_provider,
                actual_provider="gtts",
                fallback_used=requested_provider != "gtts",
                fallback_reason=fallback_reason,
            )
        except Exception as error:
            logger.exception("[ERROR] Basic TTS execution failed")
            return self._enrich_result(
                {"success": False, "error": f"Basic TTS error: {error}"},
                requested_provider=requested_provider,
                actual_provider="gtts",
                fallback_used=requested_provider != "gtts",
                fallback_reason=fallback_reason,
            )

    async def _synthesize_via_tts_service(
        self,
        channel_name: str,
        text: str,
        author: str,
        user_id: int = None,
        volume_level: float = TTS_DEFAULT_VOLUME,
        connection_manager=None,
        tts_settings: dict = None,
        word_filter: list = None,
        blocked_users: list = None,
        provider: str = "f5",
        tts_endpoint: str = None,
        tts_endpoint_api_key: Optional[str] = None,
    ) -> Dict:
        """Synthesize through remote provider service endpoint."""
        normalized_provider = normalize_provider(provider)
        tts_type = "ai_qwen" if normalized_provider == "qwen" else "ai_f5"

        try:
            query_params: Dict[str, Any]
            if tts_endpoint:
                try:
                    endpoint = normalize_local_tts_endpoint_url(tts_endpoint)
                except ValueError as error:
                    logger.warning(
                        "[WARN] Invalid local endpoint during synthesis provider=%s error=%s",
                        normalized_provider,
                        error,
                    )
                    return {"success": False, "error": "Invalid local endpoint configuration"}
                headers = build_tts_auth_headers(
                    provider=normalized_provider,
                    upstream="local",
                    local_api_key=tts_endpoint_api_key,
                    strict=False,
                )
                query_params = {}
            else:
                try:
                    endpoint = get_synthesis_upstream_url(normalized_provider).rstrip("/")
                except ProviderRoutingError as error:
                    if str(error) == "qwen_gateway_required":
                        return {
                            "success": False,
                            "error": "Qwen synthesis requires configured tts-gateway",
                        }
                    return {
                        "success": False,
                        "error": f"Provider routing error: {error}",
                    }

                use_gateway = should_route_provider_via_gateway(normalized_provider)
                headers = build_tts_auth_headers(
                    provider=normalized_provider,
                    upstream="synthesis",
                    use_gateway=use_gateway,
                    strict=True,
                )
                query_params = get_synthesis_upstream_params(normalized_provider)

            if normalized_provider == "qwen" and tts_endpoint:
                return await self._synthesize_via_qwen_local_compat(
                    channel_name=channel_name,
                    text=text,
                    author=author,
                    user_id=user_id,
                    volume_level=volume_level,
                    tts_settings=tts_settings,
                    tts_endpoint=endpoint,
                    tts_endpoint_api_key=tts_endpoint_api_key,
                )

            timeout = aiohttp.ClientTimeout(total=30, connect=10)

            request_settings = dict(tts_settings or {})
            request_settings.setdefault("advanced_provider", normalized_provider)
            trace_id = str(request_settings.get("trace_id") or "").strip()
            source_message_id = str(request_settings.get("source_message_id") or "").strip()
            f5_voice = str(request_settings.get("voice") or "").strip()
            qwen_model = normalize_qwen_model_selection(request_settings.get("qwen_model"))
            qwen_family = get_qwen_model_family(qwen_model)
            request_settings["qwen_model"] = qwen_model
            raw_qwen_voice = str(request_settings.get("qwen_voice") or "").strip()
            if normalized_provider == "qwen" and qwen_family != "base":
                request_settings["qwen_instruction"] = str(
                    request_settings.get("qwen_instruction") or raw_qwen_voice
                ).strip() or _QWEN_LOCAL_DEFAULT_INSTRUCTION
            qwen_voice = raw_qwen_voice or f5_voice
            voice_map = {}
            if f5_voice:
                voice_map["f5"] = f5_voice
            if qwen_voice and not (normalized_provider == "qwen" and qwen_family != "base"):
                voice_map["qwen"] = qwen_voice
            if normalized_provider == "qwen":
                selected_request_voice = voice_map.get("qwen") or "default"
            else:
                selected_request_voice = voice_map.get("f5") or f5_voice or "default_voice"

            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"{endpoint}/api/tts/synthesize-channel"
                payload = {
                    "channel_name": channel_name,
                    "text": text,
                    "author": author,
                    "user_id": user_id,
                    "volume_level": volume_level,
                    "tts_settings": request_settings,
                    "word_filter": word_filter or [],
                    "blocked_users": blocked_users or [],
                    "provider": normalized_provider,
                    "voice": selected_request_voice,
                    "voice_map": voice_map,
                    "request_id": source_message_id or trace_id or uuid.uuid4().hex,
                    "event_id": source_message_id or None,
                }
                logger.info(
                    "[TRACE] Provider request provider=%s trace_id=%s source_message_id=%s voice=%s endpoint=%s",
                    normalized_provider,
                    trace_id or "-",
                    source_message_id or "-",
                    selected_request_voice,
                    endpoint,
                )

                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    params=query_params,
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(
                            "[ERROR] Provider returned error provider=%s trace_id=%s source_message_id=%s status=%s body=%s",
                            normalized_provider,
                            trace_id or "-",
                            source_message_id or "-",
                            response.status,
                            error_text,
                        )
                        return {
                            "success": False,
                            "error": f"Provider error: {response.status}",
                        }

                    result = await response.json()
                    if isinstance(result, dict) and result.get("success") is False:
                        upstream_error = str(
                            result.get("error")
                            or result.get("detail")
                            or "Provider returned unsuccessful payload"
                        ).strip()
                        logger.error(
                            "[ERROR] Provider returned unsuccessful payload provider=%s trace_id=%s source_message_id=%s error=%s body=%s",
                            normalized_provider,
                            trace_id or "-",
                            source_message_id or "-",
                            upstream_error,
                            result,
                        )
                        return {
                            "success": False,
                            "error": upstream_error or "Provider returned unsuccessful payload",
                        }

                    provider_result = await self._build_provider_success_result(
                        session=session,
                        provider=normalized_provider,
                        endpoint=endpoint,
                        headers=headers,
                        tts_type=tts_type,
                        result_payload=result,
                        volume_level=volume_level,
                    )
                    selected_voice = provider_result.get("voice")

                    # Preserve existing behavior: if voice has a per-channel priority volume,
                    # trigger one more provider request with that volume.
                    if connection_manager and selected_voice:
                        priority_volume = connection_manager.get_voice_volume(channel_name, selected_voice)
                        if priority_volume != TTS_DEFAULT_VOLUME:
                            payload["volume_level"] = priority_volume
                            async with session.post(
                                url,
                                json=payload,
                                headers=headers,
                                params=query_params,
                                timeout=timeout,
                            ) as priority_response:
                                if priority_response.status == 200:
                                    priority_payload = await priority_response.json()
                                    return await self._build_provider_success_result(
                                        session=session,
                                        provider=normalized_provider,
                                        endpoint=endpoint,
                                        headers=headers,
                                        tts_type=tts_type,
                                        result_payload=priority_payload,
                                        volume_level=priority_volume,
                                    )

                    return provider_result

        except asyncio.TimeoutError:
            logger.warning("[WARN] Provider request timeout provider=%s", normalized_provider)
            return {"success": False, "error": "Request timeout"}
        except TTSAuthConfigError as error:
            logger.warning(
                "[WARN] Provider request auth configuration error provider=%s error=%s",
                normalized_provider,
                error,
            )
            return {"success": False, "error": str(error)}
        except aiohttp.ClientError as error:
            logger.warning("[WARN] Provider request connection error provider=%s error=%s", normalized_provider, error)
            return {"success": False, "error": f"Connection error: {error}"}
        except Exception:
            logger.exception("[ERROR] Provider request failed provider=%s", normalized_provider)
            return {"success": False, "error": "Internal server error"}

    async def _synthesize_via_basic_tts(self, text: str, volume_level: float) -> Dict:
        """Synthesize through local basic gTTS implementation."""
        try:
            audio_path = self.basic_tts.synthesize_speech(
                text=text,
                volume_level=volume_level,
                speed=1.0,
            )

            if not audio_path:
                logger.error("[ERROR] Basic TTS synthesize_speech returned no path")
                return {"success": False, "error": "Basic TTS synthesis failed"}

            filename = Path(audio_path).name
            audio_url = f"{self.backend_url}/api/tts/audio/{filename}"

            return {
                "success": True,
                "voice": "basic_gtts",
                "volume": volume_level,
                "tts_type": "basic_gtts",
                "audio_url": audio_url,
                "audio_path": audio_path,
            }

        except Exception:
            logger.exception("[ERROR] Basic TTS synthesis exception")
            return {"success": False, "error": "Internal server error"}

    async def _synthesize_via_google_cloud_tts(
        self,
        text: str,
        volume_level: float,
        tts_settings: dict,
    ) -> Dict:
        """Synthesize via Google Cloud TTS provider."""
        try:
            voice_pool = []
            if tts_settings:
                voice_pool = tts_settings.get("gcloud_voices") or tts_settings.get("gcloudVoices") or []

            cleaned_voice_pool = [
                str(voice).strip()
                for voice in voice_pool
                if isinstance(voice, str) and str(voice).strip()
            ]
            filtered_voice_pool = [
                voice
                for voice in cleaned_voice_pool
                if is_gemini_or_chirp_voice(voice)
            ]

            if cleaned_voice_pool and not filtered_voice_pool:
                logger.warning(
                    "[WARN] All saved Google voices are legacy/non-premium. Gemini/Chirp only is supported."
                )

            gemini_voice_pool = [
                voice
                for voice in filtered_voice_pool
                if _gcloud_voice_quality_rank(voice) == 0
            ]
            random_pool = gemini_voice_pool or filtered_voice_pool

            fallback_voice = tts_settings.get("voice") if tts_settings else None
            if fallback_voice and not is_gemini_or_chirp_voice(fallback_voice):
                fallback_voice = None

            voice_name = random.choice(random_pool) if random_pool else fallback_voice
            gcloud_mood = normalize_gcloud_mood(
                (tts_settings or {}).get("gcloud_mood")
                or (tts_settings or {}).get("gcloudMood")
            )

            result = await self.google_cloud_tts.synthesize_speech(
                text=text,
                volume_level=volume_level,
                speed=1.0,
                voice_name=voice_name,
                mood=gcloud_mood,
            )

            if not result.get("success"):
                return result

            if result.get("fallback_used"):
                logger.warning(
                    "[WARN] Google Cloud runtime fallback requested_model=%s resolved_voice=%s",
                    result.get("requested_model") or "-",
                    result.get("voice") or "-",
                )

            audio_path = result.get("audio_path")
            if not audio_path:
                return {"success": False, "error": "No audio_path returned"}

            filename = Path(audio_path).name
            audio_url = f"{self.backend_url}/api/tts/audio/{filename}"

            return {
                "success": True,
                "voice": result.get("voice") or "google_cloud",
                "volume": volume_level,
                "tts_type": "google_cloud",
                "audio_url": audio_url,
                "audio_path": audio_path,
                "auth_mode": result.get("auth_mode"),
                "requested_model": result.get("requested_model"),
                "fallback_used": bool(result.get("fallback_used")),
            }

        except Exception:
            logger.exception("[ERROR] Google Cloud TTS synthesis exception")
            return {"success": False, "error": "Internal server error"}

    async def _upload_to_tts_service(self, audio_path: str) -> Optional[str]:
        """Upload an audio file to the F5 service for temporary serving."""
        try:
            import aiofiles

            filename = Path(audio_path).name
            target_url = get_provider_service_url("f5").rstrip("/")

            async with aiofiles.open(audio_path, "rb") as file_handle:
                audio_data = await file_handle.read()

            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                data = aiohttp.FormData()
                data.add_field("file", audio_data, filename=filename, content_type="audio/wav")

                headers = build_tts_auth_headers(
                    provider="f5",
                    upstream="voice",
                    strict=True,
                )
                async with session.post(
                    f"{target_url}/api/upload-audio",
                    data=data,
                    headers=headers,
                ) as response:
                    if response.status != 200:
                        logger.warning("[WARN] Could not upload audio to F5 service status=%s", response.status)
                        return None

                    await response.json()
                    return f"{target_url}/api/audio/{filename}"

        except TTSAuthConfigError as error:
            logger.warning("[WARN] Upload to provider skipped due to auth configuration error: %s", error)
            return None
        except Exception:
            logger.exception("[ERROR] Upload to provider service failed")
            return None

    def cleanup_old_files(self):
        """Clean old temporary files from basic TTS runtime."""
        try:
            self.basic_tts.cleanup_old_files()
        except Exception:
            logger.exception("[ERROR] Basic TTS cleanup failed")

    def cleanup_old_files_if_needed(self):
        """Periodic cleanup every 10 synthesis operations."""
        if not hasattr(self, "_synthesis_count"):
            self._synthesis_count = 0

        self._synthesis_count += 1
        if self._synthesis_count % 10 == 0:
            self.cleanup_old_files()


_tts_manager_instance = None


def get_tts_manager() -> TTSManager:
    """Return singleton TTS manager instance."""
    global _tts_manager_instance
    if _tts_manager_instance is None:
        _tts_manager_instance = TTSManager()
    return _tts_manager_instance
