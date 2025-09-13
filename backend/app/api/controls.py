import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
import logging

from app.core.security import get_current_user
from app.bot import Bot as TwitchBot
from app.dependencies import get_bot
from app.services.state_service import StateService
from app.dependencies import get_state_service

router = APIRouter()
logger = logging.getLogger(__name__)

# --- НОВАЯ МОДЕЛЬ ДАННЫХ ДЛЯ RVC ---
class RVCConfigUpdate(BaseModel):
    pth_path: str
    index_path: str
    pitch: int = 0
    index_rate: float = 0.75
    f0method: str = "rmvpe"

class TTSState(BaseModel):
    is_enabled: bool

class VolumeState(BaseModel):
    volume: float # Should be between 0.0 and 1.0

class GenerationSettings(BaseModel):
    temperature: float # [0.0, 1.0]
    stability: float   # [0.0, 1.0]

# --- НОВЫЙ ЭНДПОИНТ ДЛЯ RVC ---
@router.post("/rvc/configure")
async def configure_rvc(
    config: RVCConfigUpdate,
    user: dict = Depends(get_current_user),
    state_service: StateService = Depends(get_state_service)
):
    """Saves RVC settings and triggers model loading in the RVC API."""
    
    state_service.set_rvc_config(
        pth_path=config.pth_path,
        index_path=config.index_path,
        pitch=config.pitch,
        index_rate=config.index_rate,
        f0method=config.f0method
    )
    
    # Now, forward this configuration to the RVC API to load the model
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:6242", timeout=60.0) as client:
            response = await client.post("/configure", json=config.dict())
            response.raise_for_status()
            
            rvc_api_response = response.json()
            return {
                "message": "RVC settings saved and model loaded successfully.",
                "rvc_api_message": rvc_api_response.get("message", "")
            }
    except httpx.RequestError as e:
        error_message = f"Failed to connect to RVC API: {e}"
        logger.error(error_message)
        raise HTTPException(status_code=503, detail=error_message)
    except httpx.HTTPStatusError as e:
        error_message = f"RVC API returned an error: {e.response.status_code} - {e.response.text}"
        logger.error(error_message)
        raise HTTPException(status_code=e.response.status_code, detail=error_message)


@router.post("/tts/toggle")
async def toggle_tts(tts_state: TTSState, user: dict = Depends(get_current_user), state_service: StateService = Depends(get_state_service)):
    channel_name = user.get("username")
    if not channel_name:
        raise HTTPException(status_code=400, detail="Channel name not found in token")
    
    state_service.set_tts_enabled(channel_name, tts_state.is_enabled)
    return {"message": f"TTS for channel {channel_name} has been {'enabled' if tts_state.is_enabled else 'disabled'}"}

@router.post("/volume")
async def set_volume(volume_state: VolumeState, user: dict = Depends(get_current_user), state_service: StateService = Depends(get_state_service)):
    channel_name = user.get("username")
    if not channel_name:
        raise HTTPException(status_code=400, detail="Channel name not found in token")

    state_service.set_volume(channel_name, volume_state.volume)
    return {"message": f"Volume for channel {channel_name} set to {volume_state.volume}"}

@router.post("/generation")
async def set_generation_params(settings: GenerationSettings, user: dict = Depends(get_current_user), state_service: StateService = Depends(get_state_service)):
    channel_name = user.get("username")
    if not channel_name:
        raise HTTPException(status_code=400, detail="Channel name not found in token")
    
    state_service.set_generation_settings(channel_name, settings.temperature, settings.stability)
    return {"message": "Generation settings updated successfully."}

@router.get("/generation/global")
async def get_global_generation_settings(
    user: dict = Depends(get_current_user),
    state_service: StateService = Depends(get_state_service)
):
    """Get global default generation settings"""
    return state_service.get_global_generation_settings()

@router.post("/generation/global")
async def set_global_generation_settings(
    settings: GenerationSettings,
    user: dict = Depends(get_current_user),
    state_service: StateService = Depends(get_state_service)
):
    """Set global default generation settings for all channels"""
    state_service.set_global_generation_settings(
        settings.temperature, 
        settings.stability
    )
    return {"message": "Global generation settings updated successfully"}

@router.post("/queue/clear")
async def clear_queue(user: dict = Depends(get_current_user), bot: TwitchBot = Depends(get_bot)):
    channel_name = user.get("username")
    if not channel_name:
        raise HTTPException(status_code=400, detail="Channel name not found in token")
    
    bot.audio_service.clear_queue()
    # Note: audio queue is global, not per-channel in this implementation
    return {"message": f"Audio queue cleared for channel {channel_name}"}


@router.get("/status")
async def get_status(user: dict = Depends(get_current_user), state_service: StateService = Depends(get_state_service)):
    channel_name = user.get("username")
    if not channel_name:
        raise HTTPException(status_code=400, detail="Channel name not found in token")

    state = state_service.get_channel_state(channel_name)
    if not state:
        # Return a default "off" state if the user has never logged in before
        return {
            "is_enabled": False, 
            "volume": 0.5,
            "temperature": 0.75,
            "stability": 0.5
        }

    return {
        "is_enabled": state.get("tts_enabled", False),
        "volume": state.get("volume", 0.5),
        "temperature": state.get("temperature", 0.3),
        "stability": state.get("stability", 0.7),
        "channel_name": channel_name,
        "note": "Settings apply to your channel only"
    }
