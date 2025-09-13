import asyncio
import sounddevice as sd
import soundfile as sf
from pydub import AudioSegment
import numpy as np
import logging

logger = logging.getLogger(__name__)

class AudioService:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(AudioService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, 'initialized'):
            return

        logger.info("Initializing Async Audio Service...")
        self.audio_queue = asyncio.Queue()
        self.volume = 0.7
        self._playback_task = asyncio.create_task(self._process_queue())
        self.initialized = True

    async def add_to_queue(self, audio_path: str):
        """Add the path of a WAV file to the playback queue."""
        logger.info(f"Adding to audio queue: {audio_path}")
        await self.audio_queue.put(audio_path)

    def set_volume(self, volume: float):
        """Set the playback volume (0.0 to 1.0)."""
        self.volume = max(0.0, min(1.0, volume))

    def get_volume(self) -> float:
        """Get the current playback volume."""
        return self.volume

    async def clear_queue(self) -> int:
        """Clears all items from the audio queue."""
        cleared_count = self.audio_queue.qsize()
        # To clear asyncio.Queue, we just create a new one.
        self.audio_queue = asyncio.Queue()
        logger.info(f"Audio queue cleared. {cleared_count} items removed.")
        return cleared_count

    async def _process_queue(self):
        """Continuously process the audio queue."""
        while True:
            try:
                audio_path = await self.audio_queue.get()
                logger.info(f"Now playing: {audio_path}")
                await self._play_audio(audio_path)
                self.audio_queue.task_done()
            except asyncio.CancelledError:
                logger.info("Audio processing task cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in audio processing queue: {e}", exc_info=True)

    async def _play_audio(self, audio_path: str):
        """Play a single audio file using sounddevice in a separate thread."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._blocking_play, audio_path)
            logger.info(f"Finished playing: {audio_path}")
        except Exception as e:
            logger.error(f"Error playing audio file {audio_path}: {e}", exc_info=True)

    def _blocking_play(self, audio_path: str):
        """This function runs in a thread and performs blocking IO."""
        try:
            audio = AudioSegment.from_wav(audio_path)
            
            # Normalization and volume adjustment logic remains the same
            normalized_audio = audio.apply_gain(-20.0 - audio.dBFS)
            if self.volume > 0:
                db_change = 20 * np.log10(self.volume)
                normalized_audio += db_change
            else:
                normalized_audio -= 100

            samples = np.array(normalized_audio.get_array_of_samples()).astype(np.int16)
            sd.play(samples, samplerate=normalized_audio.frame_rate)
            sd.wait()
        except Exception as e:
            # Log from the thread
            logger.error(f"Error in _blocking_play for {audio_path}: {e}", exc_info=True)

    async def stop(self):
        """Stops the audio processing task."""
        if self._playback_task:
            self._playback_task.cancel()
            try:
                await self._playback_task
            except asyncio.CancelledError:
                pass
        logger.info("Audio service stopped.")

# Singleton instance is not ideal with asyncio, but we'll keep it for now
# It should be managed as part of the application lifecycle.
audio_service_instance = AudioService()


