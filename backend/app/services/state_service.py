import json
from pathlib import Path
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

class StateService:
    def __init__(self):
        self._state_file_path = settings.BASE_DIR / "state.json"
        logger.info("Initializing State Service...")
        self.channels = {}
        self.rvc_config = {} # НОВЫЙ АТРИБУТ
        self._load_state_from_disk()
        
    def _load_state_from_disk(self):
        try:
            if self._state_file_path.exists():
                with open(self._state_file_path, 'r') as f:
                    data = json.load(f)
                    self.channels = data.get("channels", {})
                    self.rvc_config = data.get("rvc_config", {})
                logger.info(f"Loaded state from {self._state_file_path}.")
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Could not load state from disk: {e}. Starting with a fresh state.")
            self.channels = {}
            self.rvc_config = {}

    def _save_state_to_disk(self):
        try:
            with open(self._state_file_path, 'w') as f:
                # Сохраняем все состояние в одном объекте
                state_to_save = {
                    "channels": self.channels,
                    "rvc_config": self.rvc_config
                }
                json.dump(state_to_save, f, indent=4)
        except IOError as e:
            logger.error(f"Could not save state to disk: {e}")

    # --- НОВЫЕ МЕТОДЫ ДЛЯ RVC ---
    def get_rvc_config(self) -> dict:
        return self.rvc_config
    
    def set_rvc_config(self, pth_path: str, index_path: str, pitch: int, index_rate: float, f0method: str):
        self.rvc_config = {
            "pth_path": pth_path,
            "index_path": index_path,
            "pitch": pitch,
            "index_rate": index_rate,
            "f0method": f0method,
            "enabled": True # Добавим флаг включения
        }
        self._save_state_to_disk()
        logger.info(f"Updated RVC config: {self.rvc_config}")


    def get_registered_channels(self):
        return list(self.channels.keys())

    def register_channel(self, channel_name: str):
        if channel_name not in self.channels:
            self.channels[channel_name] = {
                "tts_enabled": False,
                "volume": 0.5,
                "user_voices": {}, # New field for user voice preferences
                "temperature": 0.3, # Lower temperature to reduce repetition
                "stability": 0.5   # Default stability (exaggeration)
            }
            logger.info(f"Registered new channel: {channel_name}")
            self._save_state_to_disk()

    def unregister_channel(self, channel_name: str):
        if channel_name in self.channels:
            del self.channels[channel_name]
            logger.info(f"Unregistered channel: {channel_name}")
            self._save_state_to_disk()

    def get_channel_state(self, channel_name: str):
        return self.channels.get(channel_name)

    def is_tts_enabled(self, channel_name: str) -> bool:
        state = self.get_channel_state(channel_name)
        return state["tts_enabled"] if state else False

    def set_tts_enabled(self, channel_name: str, enabled: bool):
        state = self.get_channel_state(channel_name)
        if state:
            state["tts_enabled"] = enabled
            self._save_state_to_disk()

    def get_volume(self, channel_name: str) -> float:
        state = self.get_channel_state(channel_name)
        return state["volume"] if state else 0.5

    def set_volume(self, channel_name: str, volume: float):
        state = self.get_channel_state(channel_name)
        if state:
            state["volume"] = max(0.0, min(1.0, volume))
            self._save_state_to_disk()

    # Methods for managing generation settings
    def get_generation_settings(self, channel_name: str) -> dict:
        state = self.get_channel_state(channel_name)
        if state:
            return {
                "temperature": state.get("temperature", 0.3),
                "stability": state.get("stability", 0.7)
            }
        return {"temperature": 0.3, "stability": 0.7} # Default values
    
    def get_global_generation_settings(self) -> dict:
        """Get global default generation settings for all channels"""
        return {"temperature": 0.3, "stability": 0.7}
    
    def set_global_generation_settings(self, temperature: float, stability: float):
        """Set global default generation settings for new channels"""
        # This would be stored in a separate global config file
        # For now, we'll just update the default values
        pass

    def set_generation_settings(self, channel_name: str, temperature: float, stability: float):
        state = self.get_channel_state(channel_name)
        if state:
            state["temperature"] = max(0.0, min(1.0, temperature))
            state["stability"] = max(0.0, min(1.0, stability))
            self._save_state_to_disk()


    # Methods for managing user voices
    def get_user_voice(self, channel_name: str, user_name: str) -> str | None:
        state = self.get_channel_state(channel_name)
        if state:
            return state.get("user_voices", {}).get(user_name)
        return None

    def set_user_voice(self, channel_name: str, user_name: str, voice_name: str):
        state = self.get_channel_state(channel_name)
        if state:
            if "user_voices" not in state:
                state["user_voices"] = {}
            state["user_voices"][user_name] = voice_name
            self._save_state_to_disk()
    
    def remove_user_voice(self, channel_name: str, user_name: str):
        state = self.get_channel_state(channel_name)
        if state and "user_voices" in state and user_name in state["user_voices"]:
            del state["user_voices"][user_name]
            self._save_state_to_disk()

# state_service_instance = StateService()
