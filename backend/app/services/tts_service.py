import logging
import tempfile
from pathlib import Path
from typing import Optional

import torch  # Import torch first to prevent conflicts
import numpy as np
import soundfile as sf
from ruaccent import RUAccent
from scipy import signal
import httpx # ДОБАВЛЯЕМ ИМПОРТ

from TTS.api import TTS

from app.core.config import settings

logger = logging.getLogger(__name__)

# Initialize the accentizer model once
try:
    accentizer = RUAccent()
    accentizer.load(omograph_model_size='turbo', use_dictionary=True)
    logger.info("RUAccent model loaded successfully.")
except Exception as e:
    logger.error(f"Failed to load RUAccent model: {e}", exc_info=True)
    accentizer = None


def _preprocess_text(text: str) -> str:
    """Cleans, accentuates, and prepares text for TTS synthesis."""

    # The acute accent is the proper unicode stress marker.
    STRESS_MARKER = '´'
    
    accented_text = ""
    # Handle manual stress marks first (priority)
    if '+' in text:
        # User is providing manual stress, replace '+' with the actual accent mark.
        accented_text = text.replace('+', STRESS_MARKER)
    elif accentizer:
        # No manual stress, use automatic accentuation and then replace its marker.
        text_with_plus = accentizer.process_all(text)
        accented_text = text_with_plus.replace('+', STRESS_MARKER)
    else:
        # Accentizer failed to load, use text as is.
        accented_text = text

    # Standard text cleaning on the correctly accented text.
    # We will pass the stress marker ´ to the TTS model this time.
    processed_text = accented_text.replace('…', '.').replace('..', '.').replace(' -- ', ', ')
    processed_text = processed_text.replace('—', '-').replace('"', '').replace('«', '').replace('»', '')
    processed_text = processed_text.replace(',', ', ').replace('.', '. ').replace('!', '! ').replace('?', '? ')
    
    # Collapse multiple spaces into one
    processed_text = ' '.join(processed_text.split())

    return processed_text.strip()


class TTSService:
    def __init__(self):
        logger.info("Initializing Coqui TTS Service...")

        # --- RVC HTTP Client ---
        self.rvc_client = httpx.AsyncClient(base_url="http://127.0.0.1:6242", timeout=30.0)

        self.voices_path = settings.VOICES_PATH
        self.voices_path.mkdir(exist_ok=True)

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Coqui TTS using device: {self.device}")

        try:
            # Initialize Coqui TTS with XTTS v2 (reliable multilingual model)
            self.tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(self.device)
            logger.info("Using XTTS v2 model for TTS")
            
            # XTTS v2 model is ready to use
                    
            logger.info("Coqui TTS model loaded successfully.")
        except Exception as e:
            logger.error(f"ERROR: Failed to load Coqui TTS model. Error: {e}", exc_info=True)
            self.tts = None

        self.default_voice_path = self.voices_path / "default.wav"
        if not self.default_voice_path.exists():
            logger.warning("="*50)
            logger.warning("WARNING: Global 'default.wav' not found in 'backend/voices/'.")
            logger.warning("The bot will use a generic built-in voice until a default is provided.")

    def voice_exists(self, voice_name: str, channel_name: str) -> bool:
        """Checks if a specific voice file exists for a channel."""
        if not voice_name or not channel_name:
            return False
        voice_path = self.voices_path / channel_name / f"{voice_name}.wav"
        return voice_path.exists()

    def _get_voice_path(self, voice_name: str, channel_name: str) -> Optional[str]:
        """Get the path to a voice file, checking channel-specific first, then default."""
        if voice_name and voice_name != "default":
            # Check for channel-specific voice first
            channel_voice_path = self.voices_path / channel_name / f"{voice_name}.wav"
            if channel_voice_path.exists():
                return str(channel_voice_path)

        # Fall back to default voice
        if self.default_voice_path.exists():
            return str(self.default_voice_path)

        return None

    async def _apply_rvc_conversion(self, input_wav_path: str, rvc_config: dict) -> str | None:
        """Sends an audio file to the RVC WebUI for voice conversion."""
        if not self.rvc_client:
            logger.error("RVC client not initialized.")
            return None

        url = "/process-file"
        params = {"f0method": rvc_config.get("f0method", "rmvpe")}
        
        try:
            with open(input_wav_path, "rb") as audio_file:
                files = {"audio_file": (Path(input_wav_path).name, audio_file, "audio/wav")}
                
                logger.info(f"Sending request to RVC API: {url} with params {params}")
                response = await self.rvc_client.post(url, params=params, files=files)
                
                response.raise_for_status()  # Вызовет исключение для статусов 4xx/5xx

                data = response.json()
                output_path = data.get("output_path")
                
                if output_path and Path(output_path).exists():
                    logger.info(f"RVC conversion successful. Output at: {output_path}")
                    return output_path
                else:
                    logger.error(f"RVC API returned no valid path: {data}")
                    return None

        except httpx.RequestError as e:
            logger.error(f"Error requesting RVC API: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred during RVC conversion: {e}", exc_info=True)
            return None

    async def synthesize_speech(self, text: str, voice_name: str = "default", channel_name: str = "default",
                          temperature: float = 0.75, length_penalty: float = 1.0, repetition_penalty: float = 5.0,
                          top_k: int = 50, top_p: float = 0.85, apply_rvc: bool = False, rvc_config: dict = None) -> str | None:
        """Synthesize speech using Coqui TTS with improved parameters and optional RVC."""
        if not self.tts:
            logger.error("Coqui TTS model is not available.")
            return None

        # Preprocess text
        text = _preprocess_text(text.strip()) # Use the new preprocessing function
        if not text:
            return None
        if len(text) > 250:  # Increased limit slightly
            text = text[:250]

        # Get voice path
        voice_path = self._get_voice_path(voice_name, channel_name)

        if not voice_path:
            logger.warning("No voice sample found (neither channel-specific nor default). Cannot perform TTS.")
            return None

        output_path = Path(tempfile.mktemp(suffix=".wav"))

        logger.info(f"Synthesizing audio for: '{text}'")
        logger.info(f"Using voice sample: {voice_path}")
        logger.info(
            f"TTS params: temp={temperature}, len_penalty={length_penalty}, rep_penalty={repetition_penalty}, "
            f"top_k={top_k}, top_p={top_p}"
        )

        try:
            # Generate audio with XTTS v2 using proper voice cloning parameters
            wav = self.tts.tts(
                text=text,
                speaker_wav=voice_path,
                language="ru",
                speed=1.0,
                split_sentences=True,
                temperature=temperature,
                length_penalty=length_penalty,
                repetition_penalty=repetition_penalty,
                top_k=top_k,
                top_p=top_p,
            )

            # The model's native sample rate is 24000 Hz.
            model_sample_rate = 24000

            # Simple normalization to prevent clipping
            wav_np = np.array(wav)
            wav_final = wav_np / (np.max(np.abs(wav_np)) + 1e-6) * 0.95

            # Save audio with the correct sample rate and format
            sf.write(str(output_path), wav_final, model_sample_rate, subtype='PCM_16')

            logger.info(f"Audio synthesized and saved to {output_path}")

            # --- RVC INTEGRATION ---
            if apply_rvc and rvc_config:
                logger.info("Applying RVC conversion...")
                rvc_output_path = await self._apply_rvc_conversion(str(output_path), rvc_config)
                if rvc_output_path:
                    # Optionally, remove the intermediate XTTS file
                    # output_path.unlink() 
                    return rvc_output_path
                else:
                    logger.warning("RVC conversion failed. Returning original XTTS audio.")

            return str(output_path)

        except Exception as e:
            logger.error(f"Error during TTS synthesis: {e}", exc_info=True)
            return None