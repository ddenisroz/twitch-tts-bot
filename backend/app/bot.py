import logging
from pathlib import Path
from dotenv import load_dotenv
from twitchio.ext import commands
from app.core.config import settings
from app.services.state_service import StateService
from app.services.tts_service import TTSService
from app.services.audio_service import AudioService

load_dotenv()

logger = logging.getLogger(__name__)

class Bot(commands.Bot):
    def __init__(self, tts_service: TTSService, audio_service: AudioService, state_service: StateService):
        super().__init__(token=settings.TWITCH_BOT_TOKEN, prefix=settings.BOT_PREFIX, initial_channels=[])
        self.tts_service = tts_service
        self.audio_service = audio_service
        self.state_service = state_service
        logger.info("Bot initialized and waiting for channels...")

    async def event_ready(self):
        logger.info(f'Logged in as | {self.nick}')
        logger.info(f'User id is | {self.user_id}')

    async def add_channel(self, channel_name: str):
        """Joins a channel if not already in it."""
        channel_name_lower = channel_name.lower()
        if channel_name_lower not in [ch.name for ch in self.connected_channels]:
            await self.join_channels([channel_name_lower])
            logger.info(f"Successfully joined channel: {channel_name_lower}")
            self.state_service.register_channel(channel_name_lower)
        else:
            logger.info(f"Bot is already in channel: {channel_name_lower}")

    async def remove_channel(self, channel_name: str):
        """Leaves a channel if in it."""
        channel_name_lower = channel_name.lower()
        if channel_name_lower in [ch.name for ch in self.connected_channels]:
            await self.part_channels([channel_name_lower])
            logger.info(f"Successfully left channel: {channel_name_lower}")
            self.state_service.unregister_channel(channel_name_lower)
        else:
            logger.warning(f"Attempted to leave channel '{channel_name_lower}' but bot was not in it.")


    @commands.command(name='voice')
    async def set_voice_command(self, ctx: commands.Context, *, voice_name: str):
        """Sets the user's preferred voice for TTS."""
        author_name = ctx.author.name.lower()
        channel_name = ctx.channel.name

        cleaned_voice_name = voice_name.strip().lower()
        if not cleaned_voice_name:
            await ctx.send(f"@{ctx.author.name}, пожалуйста, укажите название голоса. Пример: !voice yourchy")
            return

        if self.tts_service.voice_exists(cleaned_voice_name, channel_name):
            self.state_service.set_user_voice(channel_name, author_name, cleaned_voice_name)
            await ctx.send(f"@{ctx.author.name}, ваш голос изменен на '{cleaned_voice_name}'.")
        else:
            await ctx.send(f"@{ctx.author.name}, голос '{cleaned_voice_name}' не найден для этого канала.")


    async def event_message(self, message):
        from app.core.config import settings
        if message.echo or message.content.startswith(settings.BOT_PREFIX):
            # Let the command handler process the command
            await self.handle_commands(message)
            return

        channel_name = message.channel.name
        author_name = message.author.name.lower()

        # Check if TTS is enabled for the channel
        if not self.state_service.is_tts_enabled(channel_name):
            return

        logger.info(f"Received message from {author_name} in {channel_name}: {message.content}")

        # Get generation settings for the channel
        settings = self.state_service.get_generation_settings(channel_name)
        
        # --- RVC ---
        rvc_config = self.state_service.get_rvc_config()
        apply_rvc = rvc_config.get("enabled", False)
        
        # Determine which voice to use
        selected_voice_name = self.state_service.get_user_voice(channel_name, author_name) or "default"
        
        # Verify that the selected voice still exists, otherwise revert to default
        if selected_voice_name != "default" and not self.tts_service.voice_exists(selected_voice_name, channel_name):
            logger.warning(f"Voice file '{selected_voice_name}' for user '{author_name}' not found. Reverting to default.")
            self.state_service.remove_user_voice(channel_name, author_name)
            selected_voice_name = "default"

        try:
            wav_path = await self.tts_service.synthesize_speech(
                text=message.content,
                voice_name=selected_voice_name,
                channel_name=channel_name,
                temperature=settings.get("temperature", 0.75),
                length_penalty=settings.get("length_penalty", 1.0),
                repetition_penalty=settings.get("repetition_penalty", 5.0),
                top_k=settings.get("top_k", 50),
                top_p=settings.get("top_p", 0.85),
                apply_rvc=apply_rvc,
                rvc_config=rvc_config
            )
            if wav_path:
                await self.audio_service.add_to_queue(wav_path)
        except Exception as e:
            logger.error(f"Error synthesizing audio: {e}", exc_info=True)
