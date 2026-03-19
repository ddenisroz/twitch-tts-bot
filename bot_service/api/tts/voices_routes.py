import logging
import mimetypes
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Form, File, UploadFile, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from core.database import get_db
from core.config import settings
from auth.auth import get_current_user
from core.permissions import require_permission, Permission
from core.internal_service_auth import TTSAuthConfigError, build_tts_auth_headers, build_tts_httpx_client_kwargs
from core.project_paths import TEMP_DIR
from services.tts.tts_core import check_user_whitelisted
from services.voice_management_service import VoiceManagementService
from repositories.user_repository import UserRepository
from repositories.local_tts_repository import LocalTTSRepository
from repositories.tts_settings_repository import TTSSettingsRepository
from services.tts.provider_utils import (
    ProviderRoutingError,
    get_all_provider_capabilities,
    get_qwen_model_family,
    get_voice_management_upstream_params,
    get_voice_management_upstream_url,
    normalize_provider,
    normalize_qwen_model_selection,
    QWEN_BASE_MODEL,
    qwen_voice_crud_not_available_detail,
)
logger = logging.getLogger('bot_service')
voices_router = APIRouter(prefix='/api/voices', tags=['voices'])
user_voices_router = APIRouter(prefix='/api/user/voices', tags=['user_voices'])

QWEN_VOICE_PREVIEW_TIMEOUT_SECONDS = max(
    30.0,
    float(getattr(settings, "qwen_voice_preview_timeout_seconds", 60.0)),
)

class VoiceSchema(BaseModel):
    id: int
    name: str
    file_path: str
    voice_type: str
    owner_id: Optional[int] = None
    is_public: bool = False
    is_active: bool = True
    reference_text: Optional[str] = None
    created_at: Optional[str] = None

def get_voice_service(db: Session=Depends(get_db)) -> VoiceManagementService:
    return VoiceManagementService(db)

def _current_user_id(user: dict) -> int:
    user_id = user.get('id', user.get('user_id'))
    if not isinstance(user_id, int) or user_id <= 0:
        raise HTTPException(status_code=401, detail='Authentication required.')
    return user_id

def _tts_auth_headers(provider: str) -> dict:
    try:
        return build_tts_auth_headers(
            provider=provider,
            upstream='voice',
            strict=True,
        )
    except TTSAuthConfigError as error:
        raise HTTPException(
            status_code=500,
            detail={
                'code': 'tts_upstream_auth_not_configured',
                'message': str(error),
            },
        ) from error

def _is_admin(user: dict) -> bool:
    return user.get('role') == 'admin' or bool(user.get('is_admin', False))

def _normalize_voice_provider(provider: Optional[str]) -> str:
    normalized = normalize_provider(provider or 'f5')
    if normalized == 'gcloud':
        raise HTTPException(
            status_code=400,
            detail={
                'code': 'gcloud_voice_management_not_supported',
                'message': 'Google Cloud voice management is not supported via bot_service.',
            },
        )
    return 'qwen' if normalized == 'qwen' else 'f5'

def _provider_base_url(provider: str) -> str:
    resolved_provider = _normalize_voice_provider(provider)
    try:
        return get_voice_management_upstream_url(resolved_provider)
    except ProviderRoutingError as error:
        if str(error) == 'qwen_voice_crud_not_available':
            raise HTTPException(status_code=501, detail=qwen_voice_crud_not_available_detail()) from error
        raise HTTPException(status_code=400, detail={'code': str(error), 'message': str(error)}) from error


def _provider_upstream_params(provider: str, extra_params: Optional[dict] = None) -> dict:
    resolved_provider = _normalize_voice_provider(provider)
    return get_voice_management_upstream_params(resolved_provider, extra_params=extra_params)


def _raise_tts_upstream_error(
    response: httpx.Response,
    *,
    operation: str,
    default_detail: str,
) -> None:
    status_code = response.status_code
    raw_body = (response.text or "").strip()
    if raw_body:
        logger.warning(
            "TTS upstream error during %s: status=%s body=%s",
            operation,
            status_code,
            raw_body[:500],
        )
    else:
        logger.warning("TTS upstream error during %s: status=%s", operation, status_code)

    detail = default_detail
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = (
                str(payload.get("detail") or payload.get("message") or payload.get("error") or "").strip()
                or default_detail
            )
    except Exception:
        pass

    if status_code in (401, 403):
        raise HTTPException(status_code=503, detail="TTS service authorization failed")

    raise HTTPException(status_code=status_code, detail=detail)


def _guess_audio_suffix(*, audio_url: str, content_type: Optional[str]) -> str:
    guessed_suffix = mimetypes.guess_extension(str(content_type or "").split(";")[0].strip()) or ""
    if guessed_suffix:
        return guessed_suffix

    parsed_path = Path(urlparse(audio_url).path)
    if parsed_path.suffix:
        return parsed_path.suffix.lower()

    return ".wav"


def _resolve_provider_audio_url(*, upstream_base_url: str, audio_url: str) -> str:
    raw_audio_url = str(audio_url or "").strip()
    if not raw_audio_url:
        return ""
    if raw_audio_url.startswith(("http://", "https://")):
        return raw_audio_url
    base = upstream_base_url.rstrip("/")
    if raw_audio_url.startswith("/"):
        return f"{base}{raw_audio_url}"
    return f"{base}/api/tts/audio/{raw_audio_url}"


async def _materialize_preview_audio(
    *,
    provider: str,
    payload: dict,
    upstream_base_url: str,
) -> dict:
    raw_audio_url = str(payload.get("audio_url") or "").strip()
    if not raw_audio_url:
        return payload

    backend_base_url = str(settings.backend_url or "").strip().rstrip("/")
    if backend_base_url and raw_audio_url.startswith(backend_base_url):
        return payload

    resolved_audio_url = _resolve_provider_audio_url(
        upstream_base_url=upstream_base_url,
        audio_url=raw_audio_url,
    )
    if not resolved_audio_url:
        return payload

    async with httpx.AsyncClient(timeout=30.0, **build_tts_httpx_client_kwargs()) as client:
        audio_response = await client.get(
            resolved_audio_url,
            headers=_tts_auth_headers(provider),
        )
    if audio_response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Provider preview audio is unavailable.",
        )

    suffix = _guess_audio_suffix(
        audio_url=resolved_audio_url,
        content_type=audio_response.headers.get("content-type"),
    )
    output_dir = TEMP_DIR / "tts_audio"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"preview_{provider}_{uuid.uuid4().hex}{suffix}"
    output_path = output_dir / filename
    output_path.write_bytes(audio_response.content)

    localized_payload = dict(payload)
    localized_payload["audio_url"] = f"{backend_base_url}/api/tts/audio/{filename}" if backend_base_url else f"/api/tts/audio/{filename}"
    localized_payload["audio_path"] = str(output_path.resolve())
    return localized_payload

@voices_router.get('/whitelist-status')
async def check_whitelist_status(user: dict=Depends(get_current_user), db: Session=Depends(get_db)):
    """Check whether the current user can manage advanced voices."""
    try:
        if not user or not user.get('id') or user.get('id') <= 0:
            return {'is_whitelisted': False, 'can_manage_voices': False, 'message': 'Authentication required.'}
        user_repo = UserRepository(db)
        local_repo = LocalTTSRepository(db)
        db_user = user_repo.get_by_id(user['id'])
        if not db_user:
            return {'is_whitelisted': False, 'can_manage_voices': False}
        local_f5_endpoint = local_repo.get_active(user_id=user['id'], provider='f5')
        local_qwen_endpoint = local_repo.get_active(user_id=user['id'], provider='qwen')
        has_local_setup = bool(local_f5_endpoint and local_f5_endpoint.is_healthy or (local_qwen_endpoint and local_qwen_endpoint.is_healthy))
        if has_local_setup:
            logger.info('[LOCAL] User %s has local TTS setup, allowing voice management', user['id'])
            return {'is_whitelisted': True, 'can_manage_voices': True, 'has_local_setup': True}
        from utils.whitelist_cache import is_channel_whitelisted_cached, is_user_whitelisted_cached
        is_whitelisted = is_user_whitelisted_cached(db_user, db)
        if is_whitelisted:
            if db_user.twitch_username and is_channel_whitelisted_cached(db_user.twitch_username.lower(), 'twitch', db):
                logger.info('[OK] User %s (%s) whitelisted on Twitch', user['id'], db_user.twitch_username)
                return {'is_whitelisted': True, 'can_manage_voices': True, 'platform': 'twitch'}
            vk_channel = db_user.vk_channel_name or db_user.vk_username
            if vk_channel and is_channel_whitelisted_cached(vk_channel.lower(), 'vk', db):
                logger.info('[OK] User %s (%s) whitelisted on VK', user['id'], vk_channel)
                return {'is_whitelisted': True, 'can_manage_voices': True, 'platform': 'vk'}
            channel_name = db_user.twitch_username or db_user.vk_username or db_user.vk_channel_name or 'unknown'
            login_platform = user.get('login_platform')
            logger.warning('[WARN] User %s (%s) is_whitelisted=True but platform not found, allowing access anyway', user['id'], channel_name)
            return {'is_whitelisted': True, 'can_manage_voices': True, 'platform': login_platform or 'unknown'}
        channel_name = db_user.twitch_username or db_user.vk_username or db_user.vk_channel_name or 'unknown'
        logger.warning('[ERROR] User %s (%s) NOT whitelisted', user['id'], channel_name)
        return {'is_whitelisted': False, 'can_manage_voices': False}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error checking whitelist status')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.get('/providers/capabilities')
async def get_provider_capabilities(current_user: dict=Depends(get_current_user)):
    _ = current_user
    return {
        'success': True,
        'providers': get_all_provider_capabilities(),
    }

@voices_router.get('/', response_model=List[VoiceSchema])
async def get_all_voices(request: Request, user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Get merged voice list for selected provider."""
    try:
        resolved_provider = _normalize_voice_provider(provider)
        return await service.get_global_voices(provider=resolved_provider)
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error getting voices')
        raise HTTPException(status_code=500, detail='Internal server error.')

@user_voices_router.get('/{user_id}')
async def get_user_voices(user_id: int, request: Request, user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Get custom voices for the selected owner."""
    try:
        actor_id = _current_user_id(user)
        if actor_id != user_id and (not _is_admin(user)):
            raise HTTPException(status_code=403, detail='Insufficient permissions.')
        resolved_provider = _normalize_voice_provider(provider)
        return await service.get_user_custom_voices(user_id, provider=resolved_provider)
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error getting user voices')
        raise HTTPException(status_code=500, detail='Internal server error.')

@user_voices_router.post('/upload')
async def upload_user_voice(
    request: Request,
    user_id: int,
    file: UploadFile = File(...),
    name: str = Form(...),
    reference_text: Optional[str] = Form(default=None),
    sample_text: Optional[str] = Form(default=None),
    user: dict = Depends(check_user_whitelisted),
    service: VoiceManagementService = Depends(get_voice_service),
    provider: str = 'f5',
):
    """Upload a custom voice for the selected owner."""
    try:
        if user['id'] != user_id and (not _is_admin(user)):
            raise HTTPException(status_code=403, detail='Operation is not permitted.')
        file_content = await file.read()
        resolved_reference_text = (reference_text or sample_text or '').strip() or None
        return await service.upload_user_voice(
            user_id=user_id,
            name=name,
            filename=file.filename,
            content=file_content,
            content_type=file.content_type,
            provider=_normalize_voice_provider(provider),
            reference_text=resolved_reference_text,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error uploading voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@user_voices_router.get('/enabled/{user_id}')
async def get_user_enabled_voices(user_id: int, user: dict=Depends(get_current_user), db: Session=Depends(get_db), provider: str='f5'):
    """Get the enabled custom voices for the selected user."""
    try:
        if user['id'] != user_id and (not _is_admin(user)):
            raise HTTPException(status_code=403, detail='Operation is not permitted.')
        resolved_provider = _normalize_voice_provider(provider)
        tts_service_url = _provider_base_url(resolved_provider)
        async with httpx.AsyncClient(timeout=10.0, **build_tts_httpx_client_kwargs()) as client:
            response = await client.get(
                f'{tts_service_url}/api/tts/user/voices/enabled/{user_id}',
                headers=_tts_auth_headers(resolved_provider),
                params=_provider_upstream_params(resolved_provider),
            )
        if response.status_code == 200:
            return response.json()
        _raise_tts_upstream_error(
            response,
            operation='get enabled voices',
            default_detail='Failed to fetch enabled voices.',
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error getting enabled voices')
        raise HTTPException(status_code=500, detail='Internal server error.')

@user_voices_router.post('/enabled/{user_id}')
async def update_user_enabled_voices(user_id: int, voice_ids: List[int], user: dict=Depends(get_current_user), db: Session=Depends(get_db), provider: str='f5'):
    """Update the enabled custom voices for the selected user."""
    try:
        if user['id'] != user_id and (not _is_admin(user)):
            raise HTTPException(status_code=403, detail='Operation is not permitted.')
        resolved_provider = _normalize_voice_provider(provider)
        tts_service_url = _provider_base_url(resolved_provider)
        async with httpx.AsyncClient(timeout=10.0, **build_tts_httpx_client_kwargs()) as client:
            response = await client.post(
                f'{tts_service_url}/api/tts/user/voices/enabled/{user_id}',
                json=voice_ids,
                headers=_tts_auth_headers(resolved_provider),
                params=_provider_upstream_params(resolved_provider),
            )
        if response.status_code == 200:
            return response.json()
        _raise_tts_upstream_error(
            response,
            operation='update enabled voices',
            default_detail='Failed to update enabled voices.',
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error updating enabled voices')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.get('/user/custom')
async def get_user_custom_voices(current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Get user's custom voices (user-uploaded voices)"""
    try:
        user_id = _current_user_id(current_user)
        voices = await service.get_user_custom_voices(user_id, provider=_normalize_voice_provider(provider))
        return {'success': True, 'voices': voices}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error fetching custom voices')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.get('/global')
async def get_global_voices(current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Get all global voices"""
    try:
        resolved_provider = _normalize_voice_provider(provider)
        voices_data = await service.get_global_voices(provider=resolved_provider)
        user_id = _current_user_id(current_user)
        user_settings = service.repository.get_by_user_id(user_id, tts_provider=resolved_provider)
        settings_map = {setting.voice_id: {'cfg_strength': setting.cfg_strength, 'speed_preset': setting.speed_preset, 'volume': setting.volume} for setting in user_settings}
        for voice in voices_data:
            voice_id = voice.get('id')
            if voice_id in settings_map:
                voice['user_settings'] = settings_map[voice_id]
            else:
                voice['user_settings'] = None
        return {'success': True, 'voices': voices_data}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error fetching global voices')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.put('/user/settings/{voice_id}')
async def update_user_voice_settings(voice_id: int, settings_data: dict, current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Update per-user voice settings for the current user."""
    try:
        user_id = _current_user_id(current_user)
        result = await service.update_user_voice_settings(user_id, voice_id, settings_data, provider=_normalize_voice_provider(provider))
        return {'success': True, 'message': 'Voice settings updated.', 'settings': result}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error updating voice settings')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.delete('/user/custom/{voice_id}')
async def delete_custom_voice(voice_id: int, current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Delete a custom voice."""
    try:
        user_id = _current_user_id(current_user)
        await service.delete_custom_voice(user_id, voice_id, provider=_normalize_voice_provider(provider))
        return {'success': True, 'message': 'Custom voice deleted.'}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error deleting custom voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.post('/{voice_id}/test')
async def test_voice(voice_id: int, payload: dict=Body(default={}), current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Synthesize a short preview for selected voice."""
    try:
        actor_id = _current_user_id(current_user)
        resolved_provider = _normalize_voice_provider(provider)
        voice_info = await service.get_voice_info(voice_id, provider=resolved_provider)
        if not voice_info:
            raise HTTPException(status_code=404, detail='Voice not found.')
        voice_name = voice_info.get('name')
        if not voice_name:
            raise HTTPException(status_code=400, detail='Could not resolve the voice name.')
        owner_id_raw = voice_info.get('owner_id')
        owner_id = owner_id_raw if isinstance(owner_id_raw, int) and owner_id_raw > 0 else None
        is_global_voice = bool(voice_info.get('is_global')) or voice_info.get('type') == 'global' or owner_id is None
        if not is_global_voice and owner_id is not None and (owner_id != actor_id) and (not _is_admin(current_user)):
            raise HTTPException(status_code=403, detail='Insufficient permissions.')
        test_text = str(payload.get('text') or payload.get('test_text') or '').strip()
        if not test_text:
            raise HTTPException(status_code=400, detail='The text parameter is required.')
        upstream_data = {'voice_name': voice_name, 'user_id': str(owner_id or actor_id), 'test_text': test_text}
        if payload.get('cfg_strength') is not None:
            upstream_data['cfg_strength'] = str(payload['cfg_strength'])
        if payload.get('speed_preset') is not None:
            upstream_data['speed_preset'] = str(payload['speed_preset'])
        if resolved_provider == 'qwen':
            settings_repo = TTSSettingsRepository(service.db)
            user_settings = settings_repo.get_or_create(owner_id or actor_id)
            requested_model = normalize_qwen_model_selection(getattr(user_settings, 'qwen_model', None))
            # Stored qwen voices are reference-audio clones, so preview must always use a Base model.
            if get_qwen_model_family(requested_model) != 'base':
                requested_model = QWEN_BASE_MODEL
            upstream_data['model'] = requested_model
        preview_timeout = QWEN_VOICE_PREVIEW_TIMEOUT_SECONDS if resolved_provider == 'qwen' else 30.0
        upstream_base_url = _provider_base_url(resolved_provider)
        async with httpx.AsyncClient(timeout=preview_timeout, **build_tts_httpx_client_kwargs()) as client:
            response = await client.post(
                f'{upstream_base_url}/api/admin/voices/test',
                data=upstream_data,
                headers=_tts_auth_headers(resolved_provider),
                params=_provider_upstream_params(resolved_provider),
            )
        if response.status_code == 200:
            preview_payload = response.json()
            if isinstance(preview_payload, dict) and preview_payload.get('success') and preview_payload.get('audio_url'):
                return await _materialize_preview_audio(
                    provider=resolved_provider,
                    payload=preview_payload,
                    upstream_base_url=upstream_base_url,
                )
            return preview_payload
        detail = 'Failed to synthesize the test voice.'
        try:
            detail = response.json().get('detail', detail)
        except Exception:
            pass
        _raise_tts_upstream_error(
            response,
            operation='test voice',
            default_detail=detail,
        )
    except httpx.TimeoutException as error:
        if resolved_provider == 'qwen':
            logger.warning(
                'Qwen voice preview timed out while waiting for worker warmup timeout=%ss error=%s',
                preview_timeout,
                error,
            )
            raise HTTPException(
                status_code=504,
                detail='Qwen worker is still loading the selected model. Try the preview again in a few seconds.',
            ) from error
        logger.warning('Voice preview timed out provider=%s error=%s', resolved_provider, error)
        raise HTTPException(status_code=504, detail='Voice preview timed out. Try again.') from error
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error testing voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.put('/user/{voice_id}/rename')
async def rename_user_voice(voice_id: int, payload: dict=Body(default={}), current_user: dict=Depends(get_current_user), provider: str='f5'):
    """Rename a custom voice for the selected provider."""
    try:
        actor_id = _current_user_id(current_user)
        user_id = payload.get('user_id')
        if not isinstance(user_id, int):
            raise HTTPException(status_code=400, detail='The user_id parameter is required.')
        if actor_id != user_id and (not _is_admin(current_user)):
            raise HTTPException(status_code=403, detail='Insufficient permissions.')
        new_name = str(payload.get('new_name') or '').strip()
        if not new_name:
            raise HTTPException(status_code=400, detail='The new_name parameter is required.')
        resolved_provider = _normalize_voice_provider(provider)
        async with httpx.AsyncClient(timeout=10.0, **build_tts_httpx_client_kwargs()) as client:
            response = await client.put(
                f'{_provider_base_url(resolved_provider)}/api/tts/user/voices/{voice_id}/rename',
                params=_provider_upstream_params(resolved_provider, {'user_id': user_id}),
                data={'new_name': new_name},
                headers=_tts_auth_headers(resolved_provider),
            )
        if response.status_code == 200:
            return response.json()
        detail = 'Failed to rename the voice.'
        try:
            detail = response.json().get('detail', detail)
        except Exception:
            pass
        _raise_tts_upstream_error(
            response,
            operation='rename user voice',
            default_detail=detail,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error renaming user voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.post('/user/{voice_id}/retranscribe')
async def retranscribe_user_voice(voice_id: int, payload: dict=Body(default={}), current_user: dict=Depends(get_current_user), provider: str='f5'):
    """Retranscribe a custom voice."""
    try:
        actor_id = _current_user_id(current_user)
        payload_user_id = payload.get('user_id')
        user_id = payload_user_id if isinstance(payload_user_id, int) else actor_id
        if actor_id != user_id and (not _is_admin(current_user)):
            raise HTTPException(status_code=403, detail='Insufficient permissions.')
        resolved_provider = _normalize_voice_provider(provider)
        async with httpx.AsyncClient(timeout=60.0, **build_tts_httpx_client_kwargs()) as client:
            response = await client.post(
                f'{_provider_base_url(resolved_provider)}/api/tts/user/voices/{voice_id}/retranscribe',
                params=_provider_upstream_params(resolved_provider, {'user_id': user_id}),
                headers=_tts_auth_headers(resolved_provider),
            )
        if response.status_code == 200:
            return response.json()
        detail = 'Failed to retranscribe the custom voice.'
        try:
            detail = response.json().get('detail', detail)
        except Exception:
            pass
        _raise_tts_upstream_error(
            response,
            operation='retranscribe user voice',
            default_detail=detail,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error retranscribing user voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.get('/admin/global')
@require_permission(Permission.MANAGE_GLOBAL_VOICES)
async def admin_get_global_voices(current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Admin: get the list of all global voices."""
    try:
        voices = await service.admin_get_global_voices(provider=_normalize_voice_provider(provider))
        return {'success': True, 'voices': voices}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error fetching global voices')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.put('/admin/global/{voice_id}')
@require_permission(Permission.MANAGE_GLOBAL_VOICES)
async def admin_update_global_voice(voice_id: int, settings_data: dict, current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Admin: update global voice settings."""
    try:
        resolved_provider = _normalize_voice_provider(provider)
        result = await service.admin_update_global_voice(voice_id, settings_data, provider=resolved_provider)
        return {'success': True, 'message': 'Global voice settings updated.', 'settings': result}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error updating global voice settings')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.delete('/admin/global/{voice_id}')
@require_permission(Permission.MANAGE_GLOBAL_VOICES)
async def admin_delete_global_voice(voice_id: int, current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Admin: delete a global voice."""
    try:
        resolved_provider = _normalize_voice_provider(provider)
        await service.admin_delete_global_voice(voice_id, provider=resolved_provider)
        service.repository.delete_by_voice_id(voice_id, tts_provider=resolved_provider)
        return {'success': True, 'message': 'Global voice deleted.'}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error deleting global voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.put('/admin/global/{voice_id}/rename')
@require_permission(Permission.MANAGE_GLOBAL_VOICES)
async def admin_rename_global_voice(voice_id: int, payload: dict=Body(default={}), new_name: Optional[str]=None, current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Admin: rename a global voice."""
    try:
        resolved_name = (payload.get('new_name') if isinstance(payload, dict) else None) or new_name
        if not resolved_name:
            raise HTTPException(status_code=400, detail='The new_name parameter is required.')
        await service.admin_rename_global_voice(voice_id, resolved_name, provider=_normalize_voice_provider(provider))
        return {'success': True, 'message': 'Global voice renamed.', 'new_name': resolved_name}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error renaming global voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.post('/admin/global/{voice_id}/transcribe')
@require_permission(Permission.MANAGE_GLOBAL_VOICES)
async def admin_transcribe_global_voice(voice_id: int, current_user: dict=Depends(get_current_user), provider: str='f5'):
    """Admin: transcribe a global voice from scratch."""
    try:
        resolved_provider = _normalize_voice_provider(provider)
        async with httpx.AsyncClient(timeout=60.0, **build_tts_httpx_client_kwargs()) as client:
            response = await client.post(
                f'{_provider_base_url(resolved_provider)}/api/admin/voices/{voice_id}/retranscribe',
                headers=_tts_auth_headers(resolved_provider),
                params=_provider_upstream_params(resolved_provider),
            )
        if response.status_code == 200:
            return response.json()
        detail = 'Failed to transcribe the global voice.'
        try:
            detail = response.json().get('detail', detail)
        except Exception:
            pass
        _raise_tts_upstream_error(
            response,
            operation='transcribe global voice',
            default_detail=detail,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error transcribing global voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.post('/admin/global/{voice_id}/retranscribe')
@require_permission(Permission.MANAGE_GLOBAL_VOICES)
async def admin_retranscribe_global_voice(voice_id: int, current_user: dict=Depends(get_current_user), provider: str='f5'):
    """Admin: retranscribe a global voice."""
    try:
        resolved_provider = _normalize_voice_provider(provider)
        async with httpx.AsyncClient(timeout=60.0, **build_tts_httpx_client_kwargs()) as client:
            response = await client.post(
                f'{_provider_base_url(resolved_provider)}/api/admin/voices/{voice_id}/retranscribe',
                headers=_tts_auth_headers(resolved_provider),
                params=_provider_upstream_params(resolved_provider),
            )
        if response.status_code == 200:
            return response.json()
        detail = 'Failed to retranscribe the global voice.'
        try:
            detail = response.json().get('detail', detail)
        except Exception:
            pass
        _raise_tts_upstream_error(
            response,
            operation='retranscribe global voice',
            default_detail=detail,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error retranscribing global voice')
        raise HTTPException(status_code=500, detail='Internal server error.')

@voices_router.post('/admin/upload')
@require_permission(Permission.MANAGE_GLOBAL_VOICES)
async def admin_upload_voice(request: Request, file: UploadFile=File(...), voice_name: str=Form(...), current_user: dict=Depends(get_current_user), service: VoiceManagementService=Depends(get_voice_service), provider: str='f5'):
    """Admin: upload a global voice."""
    try:
        file_content = await file.read()
        result = await service.admin_upload_voice(name=voice_name, filename=file.filename, content=file_content, content_type=file.content_type, provider=_normalize_voice_provider(provider))
        return {'success': True, 'message': 'Global voice uploaded.', 'voice': result}
    except HTTPException:
        raise
    except Exception:
        logger.exception('Error uploading global voice')
        raise HTTPException(status_code=500, detail='Internal server error.')
