# bot_service/services/tts/tts_service.py
from typing import List, Optional, Dict, Any
import logging
import time

from sqlalchemy.orm import Session

from core.connection_manager import get_connection_manager
from integrations.base import IntegrationError

from repositories.tts_settings_repository import TTSSettingsRepository
from repositories.filtered_word_repository import FilteredWordRepository
from repositories.blocked_user_repository import BlockedUserRepository
from repositories.audio_settings_repository import AudioSettingsRepository
from repositories.chat_message_repository import ChatMessageRepository
from repositories.user_repository import UserRepository
from repositories.user_token_repository import UserTokenRepository
from repositories.local_tts_repository import LocalTTSRepository

from services.tts.memory_tts_queue import get_memory_tts_queue
from services.advanced_rate_limiter import advanced_rate_limiter
from services.user_identity_service import UserIdentityService
from services.voice_management_service import VoiceManagementService
import random
from services.tts.provider_utils import (
    infer_provider_from_engine,
    normalize_provider,
    normalize_provider_mode,
    normalize_qwen_model_selection,
    resolve_provider_mode_for_settings,
)

logger = logging.getLogger(__name__)


class BlockTargetValidationError(Exception):
    """Base exception for blocked-user validation failures."""


class BlockTargetNotFoundError(BlockTargetValidationError):
    """Raised when the target username cannot be resolved to a real/known user."""


class BlockTargetVerificationUnavailableError(BlockTargetValidationError):
    """Raised when upstream verification is temporarily unavailable."""



class TTSService:
    """
    Unified TTS Service Facade.
    Handles:
    - Settings Management (Delegates to Repositories)
    - Synthesis Requests (Delegates to Queue)
    - Validations (Rate Limits, Filters)
    """

    def __init__(self, db: Session):
        self.db = db
        self.settings_repo = TTSSettingsRepository(db)
        self.filter_repo = FilteredWordRepository(db)
        self.blocked_user_repo = BlockedUserRepository(db)
        self.audio_repo = AudioSettingsRepository(db)
        self.chat_repo = ChatMessageRepository(db)
        self.user_repo = UserRepository(db)
        self.token_repo = UserTokenRepository(db)

    @staticmethod
    def _resolve_connection_manager_tts_type(engine: Optional[str]) -> str:
        normalized_engine = str(engine or "").strip().lower()
        return "ai" if normalized_engine in {"f5tts", "qwen", "gcloud"} else "basic"

    def _sync_connection_manager_tts_channels(self, *, user_id: int, engine: Optional[str]) -> None:
        user = self.user_repo.get_by_id(user_id)
        if not user or not getattr(user, "tts_enabled", False):
            return

        connection_manager = get_connection_manager()
        tts_type = self._resolve_connection_manager_tts_type(engine)

        if user.twitch_username:
            connection_manager.enable_tts_for_channel(user.twitch_username.lower(), tts_type=tts_type)

        tokens = self.token_repo.get_all_by_user(user_id)
        for token in tokens:
            if token.platform == 'vk' and token.platform_user_id:
                connection_manager.enable_tts_for_channel(str(token.platform_user_id).lower(), tts_type=tts_type)

    @staticmethod
    def normalize_blocked_username(username: str) -> str:
        """Normalize usernames entered in blocked-user forms."""
        return (username or "").strip().lstrip("@").strip().lower()

    def _is_known_user_locally(
        self,
        *,
        user_id: int,
        channel_name: str,
        platform: str,
        username: str,
    ) -> bool:
        if platform == "twitch" and self.user_repo.get_by_twitch_username(username):
            return True

        if platform == "vk":
            if self.user_repo.get_by_vk_username(username) or self.user_repo.get_by_vk_channel_name(username):
                return True

        return self.chat_repo.author_exists_in_channel(
            user_id=user_id,
            author_username=username,
            channel_name=channel_name,
            platform=platform,
        )

    async def _resolve_twitch_username_exists(self, username: str) -> Optional[bool]:
        twitch_client = None
        try:
            from integrations.twitch.client import TwitchClient
            from integrations.twitch.oauth import TwitchOAuth

            twitch_client = TwitchClient(TwitchOAuth.from_settings())
            result = await twitch_client.get("users", params={"login": username})
            return bool((result or {}).get("data", []))
        except IntegrationError:
            logger.exception("Failed to validate Twitch username=%s", username)
            return None
        except Exception:
            logger.exception("Failed to validate Twitch username=%s", username)
            return None
        finally:
            if twitch_client is not None:
                try:
                    await twitch_client.close()
                except Exception:
                    logger.debug("Failed to close Twitch client after username validation", exc_info=True)

    async def ensure_block_target_exists(
        self,
        *,
        user_id: int,
        channel_name: str,
        platform: str,
        username: str,
    ) -> str:
        normalized_username = self.normalize_blocked_username(username)
        if not normalized_username:
            raise BlockTargetNotFoundError("Username is required")

        platform_name = platform.lower()
        local_match = self._is_known_user_locally(
            user_id=user_id,
            channel_name=channel_name,
            platform=platform_name,
            username=normalized_username,
        )

        if platform_name == "twitch":
            exists = await self._resolve_twitch_username_exists(normalized_username)
            if exists is True:
                return normalized_username
            if exists is False:
                if local_match:
                    return normalized_username
                raise BlockTargetNotFoundError(
                    f"Twitch user '{normalized_username}' does not exist"
                )
            if local_match:
                return normalized_username
            raise BlockTargetVerificationUnavailableError(
                "Failed to verify Twitch user right now. Try again later."
            )

        if local_match:
            return normalized_username

        raise BlockTargetNotFoundError(
            f"VK user '{normalized_username}' was not found in known channel users"
        )

    # === Synthesis Management ===

    async def synthesize(
        self,
        text: str,
        user: Dict[str, Any],
        voice: str = "default_voice",
        channel: str = None,
        platform: str = "twitch",
        priority: int = 1
    ) -> Dict[str, Any]:
        """
        Process a synthesis request.
        1. Validate User
        2. Check Rate Limits
        3. Determine Channel
        4. Add to Queue
        """
        try:
            user_id = user.get('id')
            
            # 1. Validate User
            if not UserIdentityService.validate_user_data(user):
                 raise ValueError("Invalid user data")

            # 2. Determine Channel (if not provided)
            if not channel:
                channel = UserIdentityService.get_tts_channel_name(user)

            # 3. Check Rate Limits
            rate_limit_id = UserIdentityService.get_rate_limit_id(user)
            limit_result = await advanced_rate_limiter.check_tts_rate_limit(
                user_id=rate_limit_id,
                text_length=len(text)
            )

            allowed = bool(limit_result.get("allowed")) if isinstance(limit_result, dict) else bool(limit_result)
            retry_after = (
                int(limit_result.get("retry_after", 10))
                if isinstance(limit_result, dict)
                else 10
            )

            if not allowed:
                logger.warning(f"Rate limit exceeded for user {rate_limit_id}")
                return {
                    "success": False,
                    "error": "Rate limit exceeded",
                    "retry_after": max(1, retry_after),
                }

            # 4. Fetch User Settings (for metadata in queue)
            # We fetch them here to snapshot state at time of request
            tts_settings = self.settings_repo.get_or_create(user_id=user_id)
            audio_settings = self.audio_repo.get_or_create(user_id=user_id)
            
            # Map settings to dicts because Queue stores simple types
            settings_dict = self.settings_repo.get_settings_dict(tts_settings)
            
            # Add to Queue
            # Add to Queue
            task_id = await get_memory_tts_queue().add_task(
                user_id=int(user_id),
                text=text,
                voice=voice,
                channel=channel,
                platform=platform,
                priority=priority,
                metadata={
                    "requested_at": time.time(),
                    "volume": audio_settings.website_volume,
                    "author": user.get('username', 'Unknown'),
                    "settings": settings_dict,
                    "use_ai": True # Default to trying AI
                }
            )

            # Record Usage
            await advanced_rate_limiter.add_tts_request(rate_limit_id, len(text))
            
            logger.info(f"TTS task {task_id} queued for user {user_id}")
            
            return {
                "success": True,
                "task_id": task_id,
                "message": "TTS task queued"
            }

        except Exception:
            logger.exception("Error in synthesize")
            return {"success": False, "error": "Internal server error"}

    # === Settings Management ===

    async def get_audio_settings(self, user_id: int) -> dict:
        settings = self.audio_repo.get_or_create(user_id)
        return {"websiteVolume": settings.website_volume}

    async def save_audio_settings(self, website_volume: int, user_id: int) -> bool:
        try:
            settings = self.audio_repo.get_or_create(user_id)
            self.audio_repo.update(settings, {"website_volume": website_volume})
            return True
        except Exception:
            logger.exception("Error saving audio settings")
            return False

    async def get_tts_settings(self, user_id: int) -> dict:
        settings = self.settings_repo.get_or_create(user_id)
        return self.settings_repo.get_settings_dict(settings)

    async def save_tts_settings(self, **kwargs) -> dict:
        """Save TTS settings with validation."""
        try:
            user_id = kwargs.get('user_id')
            if not user_id:
                return {"success": False, "error": "Authentication required"}

            settings = self.settings_repo.get_or_create(user_id=user_id)
            
            # Version check logic if needed (can be added to repo or here)
            client_version = kwargs.get('client_version')
            if client_version is not None and hasattr(settings, 'version'):
                 if settings.version != client_version:
                     return {"success": False, "error": "Version conflict", "current_version": settings.version}

            payload = dict(kwargs)
            payload.pop("user_id", None)
            payload.pop("client_version", None)
            payload = self._normalize_settings_payload(settings, payload)

            # Update
            updated_settings = self.settings_repo.update_settings(settings, payload)

            listening_mode = payload.get("listening_mode")
            if user_id and listening_mode in {"website", "obs"}:
                user = self.user_repo.get_by_id(user_id)
                if user:
                    self.user_repo.update(user, {"tts_listening_mode": listening_mode})

            if user_id:
                self._sync_connection_manager_tts_channels(
                    user_id=user_id,
                    engine=getattr(updated_settings, "engine", payload.get("engine")),
                )
                from services.memory_websocket_manager import get_memory_websocket_manager
                await get_memory_websocket_manager().sync_user_tts_generation(user_id)
            
            # Version is auto-incremented inside update_settings
            return {"success": True, "version": getattr(updated_settings, 'version', 1)}
        
        except Exception:
            logger.exception("Error saving TTS settings")
            return {"success": False, "error": "Internal server error"}

    @staticmethod
    def _normalize_settings_payload(settings, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(payload)
        has_explicit_f5_mode = "f5_mode" in normalized
        has_explicit_qwen_mode = "qwen_mode" in normalized

        current_engine = getattr(settings, "engine", "gtts")
        current_provider = infer_provider_from_engine(
            current_engine,
            advanced_provider=getattr(settings, "advanced_provider", None),
        )
        current_f5_mode = normalize_provider_mode(getattr(settings, "f5_mode", "cloud"))
        current_qwen_mode = normalize_provider_mode(getattr(settings, "qwen_mode", "cloud"))

        if "qwen_model" in normalized:
            normalized["qwen_model"] = normalize_qwen_model_selection(normalized.get("qwen_model"))

        engine = str(normalized.get("engine") or current_engine or "gtts").strip().lower()
        provider = normalize_provider(normalized.get("advanced_provider") or current_provider)

        if "advanced_provider" in normalized and "engine" not in normalized:
            if provider == "gcloud":
                engine = "gcloud"
            elif provider == "qwen":
                engine = "qwen"
            else:
                engine = "f5tts"

        if engine == "gcloud":
            provider = "gcloud"
        elif engine == "qwen":
            provider = "qwen"
        elif engine == "f5tts":
            provider = "f5"

        normalized["engine"] = engine
        normalized["advanced_provider"] = provider

        if "f5_mode" in normalized:
            normalized["f5_mode"] = normalize_provider_mode(normalized.get("f5_mode"))
        else:
            normalized["f5_mode"] = current_f5_mode

        if "qwen_mode" in normalized:
            normalized["qwen_mode"] = normalize_provider_mode(normalized.get("qwen_mode"))
        else:
            normalized["qwen_mode"] = current_qwen_mode

        explicit_use_local = normalized.get("use_local_tts")
        if explicit_use_local is not None:
            use_local_tts = bool(explicit_use_local)
            if provider == "qwen" and not has_explicit_qwen_mode:
                normalized["qwen_mode"] = "local" if use_local_tts else "cloud"
            elif provider == "f5" and not has_explicit_f5_mode:
                normalized["f5_mode"] = "local" if use_local_tts else "cloud"

        if provider == "qwen":
            normalized["use_local_tts"] = normalize_provider_mode(normalized.get("qwen_mode")) == "local"
        elif provider == "f5":
            normalized["use_local_tts"] = normalize_provider_mode(normalized.get("f5_mode")) == "local"
        else:
            normalized["use_local_tts"] = False

        if engine in {"gtts", "gcloud"}:
            normalized["use_local_tts"] = False

        return normalized

    # === Filter Management ===

    async def get_filtered_words(self, user_id: int) -> List[dict]:
        return self.filter_repo.get_words_list(user_id=user_id)

    async def add_filtered_word(self, user_id: int, word: str, platform: str = 'all') -> bool:
        new_word = self.filter_repo.add_word(word, platform, user_id=user_id)
        return new_word is not None

    async def remove_filtered_word(self, user_id: int, word_id: int) -> bool:
        return self.filter_repo.remove_word(word_id, user_id=user_id)

    # === Blocked Users ===
    
    async def get_blocked_users(self, user_id: int) -> List[dict]:
        return self.blocked_user_repo.get_blocked_list(user_id=user_id)
        
    async def block_user(
        self,
        user_id: int,
        channel_name: str,
        platform: str,
        username: str,
    ) -> bool:
        normalized_username = self.normalize_blocked_username(username)

        return self.blocked_user_repo.block_user(
            channel_name=channel_name, 
            platform=platform, 
            username=normalized_username, 
            user_id=user_id,
        ) is not None
        
    async def unblock_user(
        self,
        user_id: int,
        channel_name: str,
        platform: str,
        username: str,
    ) -> bool:
        return self.blocked_user_repo.unblock_user(
            channel_name=channel_name, 
            platform=platform, 
            username=username, 
            user_id=user_id,
        )

    # === TTS Status ===

    async def get_tts_status(self, user_id: int) -> dict:
        """Get TTS enabled status and listening mode for a user."""
        user = self.user_repo.get_by_id(user_id)
        if not user:
            return {"enabled": False, "listening_mode": "website", "error": "User not found"}
        enabled = getattr(user, 'tts_enabled', False)

        settings = self.settings_repo.get_or_create(user_id=user_id)
        engine = getattr(settings, 'engine', 'gtts')
        provider = infer_provider_from_engine(
            engine,
            advanced_provider=getattr(settings, "advanced_provider", None),
        )
        f5_mode = normalize_provider_mode(getattr(settings, "f5_mode", "cloud"))
        qwen_mode = normalize_provider_mode(getattr(settings, "qwen_mode", "cloud"))
        _, resolved_mode = resolve_provider_mode_for_settings(
            engine=engine,
            use_local_tts=bool(getattr(settings, "use_local_tts", False)),
            advanced_provider=getattr(settings, "advanced_provider", None),
            f5_mode=f5_mode,
            qwen_mode=qwen_mode,
        )

        if engine == 'f5tts':
            engine_type = f'f5_{resolved_mode}'
        elif engine == 'qwen':
            engine_type = f'qwen_{resolved_mode}'
        elif engine == 'gcloud':
            engine_type = 'gcloud'
        else:
            engine_type = 'gtts'

        listening_mode = getattr(settings, 'listening_mode', None) or getattr(user, 'tts_listening_mode', 'website')

        has_local_setup = False
        has_local_setup_f5 = False
        has_local_setup_qwen = False
        is_whitelisted = False

        try:
            local_repo = LocalTTSRepository(self.db)
            local_f5 = local_repo.get_active(user_id=user_id, provider="f5")
            has_local_setup_f5 = bool(local_f5 and local_f5.is_healthy)
            local_qwen = local_repo.get_active(user_id=user_id, provider="qwen")
            has_local_setup_qwen = bool(local_qwen and local_qwen.is_healthy)
            has_local_setup = has_local_setup_qwen if provider == "qwen" else has_local_setup_f5
        except Exception:
            logger.exception("Failed to resolve local TTS status for user %s", user_id)

        try:
            from utils.whitelist_cache import is_user_whitelisted_cached
            is_whitelisted = bool(is_user_whitelisted_cached(user, self.db))
        except Exception:
            logger.exception("Failed to resolve whitelist status for user %s", user_id)

        return {
            "enabled": enabled,
            "listening_mode": listening_mode,
            "listeningMode": listening_mode,
            "engine_type": engine_type,
            "advanced_provider": provider,
            "f5_mode": f5_mode,
            "qwen_mode": qwen_mode,
            "has_local_setup": has_local_setup,
            "has_local_setup_f5": has_local_setup_f5,
            "has_local_setup_qwen": has_local_setup_qwen,
            "is_whitelisted": is_whitelisted,
        }

    # === Platform Settings ===

    async def set_platform_settings(self, user_id: int, enabled_platforms: list) -> bool:
        """Set enabled platforms for TTS."""
        try:
            settings = self.settings_repo.get_or_create(user_id=user_id)
            # Repository handles commit
            self.settings_repo.update_settings(settings, {"enabled_platforms": enabled_platforms})
            return True
        except Exception:
            logger.exception("Error setting platform settings")
            return False

    # === Status Management (Enable/Disable) ===

    async def enable_tts(self, user_id: int) -> bool:
        """Enable TTS and register in ConnectionManager."""
        try:
            if not user_id:
                return False

            user = self.user_repo.get_by_id(user_id)
            if not user:
                return False

            # Use repository for update
            self.user_repo.update(user, {'tts_enabled': True})

            settings = self.settings_repo.get_or_create(user_id=user_id)
            self._sync_connection_manager_tts_channels(
                user_id=user_id,
                engine=getattr(settings, "engine", None),
            )

            from services.memory_websocket_manager import get_memory_websocket_manager
            await get_memory_websocket_manager().sync_user_tts_generation(user_id)
            
            return True
        except Exception:
            logger.exception("Error enabling TTS")
            return False

    async def disable_tts(self, user_id: int) -> bool:
        """Disable TTS and unregister from ConnectionManager."""
        try:
            if not user_id:
                return False

            user = self.user_repo.get_by_id(user_id)
            if not user:
                return False

            # Use repository for update
            self.user_repo.update(user, {'tts_enabled': False})
            
            connection_manager = get_connection_manager()
            if user.twitch_username:
                connection_manager.disable_tts_for_channel(user.twitch_username.lower())
                
            tokens = self.token_repo.get_all_by_user(user_id)
            for t in tokens:
                if t.platform == 'vk' and t.platform_user_id:
                     connection_manager.disable_tts_for_channel(t.platform_user_id)

            from services.memory_websocket_manager import get_memory_websocket_manager
            await get_memory_websocket_manager().sync_user_tts_generation(user_id)

            return True
        except Exception:
             logger.exception("Error disabling TTS")
             return False



    async def set_voice(self, user_id: int, voice_name: str, db: Session = None) -> bool:
        """Set TTS voice for user."""
        try:
            settings = self.settings_repo.get_or_create(user_id=user_id)
            provider = infer_provider_from_engine(
                getattr(settings, "engine", None),
                advanced_provider=getattr(settings, "advanced_provider", None),
            )
            target_voice = (voice_name or "").strip().lower()
            if not target_voice:
                return False

            available_voices: List[str] = []
            if provider == "gcloud":
                available_voices = [
                    str(voice).strip()
                    for voice in (getattr(settings, "gcloud_voices", None) or [])
                    if isinstance(voice, str) and str(voice).strip()
                ]
            else:
                voice_service = VoiceManagementService(self.db)
                global_voices = await voice_service.get_global_voices(provider=provider)
                user_voices = await voice_service.get_user_custom_voices(user_id, provider=provider)
                available_voices = [
                    str(v.get("name", "")).strip()
                    for v in (global_voices + user_voices)
                    if isinstance(v, dict) and v.get("name")
                ]

            resolved_voice = None
            for candidate in available_voices:
                if candidate.lower() == target_voice:
                    resolved_voice = candidate
                    break

            if not resolved_voice:
                return False

            self.settings_repo.update_settings(settings, {"voice": resolved_voice})
            return True
            
        except Exception:
            logger.exception("Error setting voice")
            return False

    async def set_random_voice(self, user_id: int, db: Session = None) -> Optional[str]:
        """Set random TTS voice for user."""
        try:
            settings = self.settings_repo.get_or_create(user_id=user_id)
            provider = infer_provider_from_engine(
                getattr(settings, "engine", None),
                advanced_provider=getattr(settings, "advanced_provider", None),
            )

            available_voices: List[str] = []
            if provider == "gcloud":
                available_voices = [
                    str(voice).strip()
                    for voice in (getattr(settings, "gcloud_voices", None) or [])
                    if isinstance(voice, str) and str(voice).strip()
                ]
            else:
                voice_service = VoiceManagementService(self.db)
                global_voices = await voice_service.get_global_voices(provider=provider)
                user_voices = await voice_service.get_user_custom_voices(user_id, provider=provider)
                available_voices = [
                    str(v.get("name", "")).strip()
                    for v in (global_voices + user_voices)
                    if isinstance(v, dict) and v.get("name")
                ]

            if not available_voices:
                return None
                
            voice_name = random.choice(available_voices)
            
            self.settings_repo.update_settings(settings, {'voice': voice_name})
            # Repository handles commit
            
            return voice_name
            
        except Exception:
            logger.exception("Error setting random voice")
            return None

    async def set_volume(self, user_id: int, volume: int, db: Session = None) -> bool:
        """Set TTS website volume."""
        try:
            return await self.save_audio_settings(website_volume=volume, user_id=user_id)
        except Exception:
            logger.exception("Error setting volume")
            return False

