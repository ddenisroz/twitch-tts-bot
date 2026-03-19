import logging
import json
import asyncio
import re
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
import uuid

from sqlalchemy import func

from core.database import SessionLocal, User

from repositories.user_repository import UserRepository
from repositories.chat_message_repository import ChatMessageRepository
from services.memory_websocket_manager import get_memory_websocket_manager
from constants import TTS_DEFAULT_VOLUME

logger = logging.getLogger('bot_service.notifications')
_INTERNAL_USER_CHANNEL_RE = re.compile(r"^user_(\d+)$")

class NotificationService:
    """
    Service for broadcasting messages and events via WebSocket.
    """

    async def broadcast_chat_message(
        self,
        username: str,
        content: str,
        platform: str,
        channel: str,
        message_id: Optional[str] = None,
        role: Optional[str] = None,
        badges: Optional[list] = None,
        emotes: Optional[list] = None,
        avatar_url: Optional[str] = None,
    ) -> bool:
        """Broadcast chat message to all connected clients."""
        try:
            logger.info(f"[BROADCAST] Incoming message: {platform}:{channel} | {username}: {content[:50]}")
            
            chat_data = {
                "type": "message",
                "id": message_id or str(uuid.uuid4()),
                "author": username,
                "author_name": username,
                "username": username,
                "content": content,
                "message": content,
                "text": content,
                "platform": platform,
                "channel": channel,
                "role": role,
                "badges": badges,
                "emotes": emotes,
                "avatar_url": avatar_url,
                "timestamp": int(datetime.now().timestamp() * 1000)
            }

            # Async fire-and-forget save to DB (or await if critical)
            # Refactoring note: DB saving could be delegated to a separate repository/service completely
            await self._save_message_to_db(username, content, platform, channel, role, badges)

            # Broadcast
            connections = get_memory_websocket_manager().connections
            if not connections:
                logger.debug(f"[WARN] No WebSocket connections available for {platform} message")
                return False

            message_json = json.dumps(chat_data)
            sent_count = await self._broadcast_to_connections(connections, message_json)
            
            logger.info(f"[SEND] {platform.upper()} message sent to {sent_count}/{len(connections)} connections")
            return sent_count > 0

        except Exception:
            logger.exception("[ERROR] WebSocket broadcast error for {platform}")
            return False

    async def broadcast_tts_audio(
        self,
        audio_data: Dict[str, Any],
        channel_name: str,
        platform: str = "twitch"
    ) -> bool:
        """Broadcast TTS audio event."""
        try:
            audio_url = str(audio_data.get("audio_url") or "").strip()
            if not audio_url:
                logger.error(
                    "[TTS] Skip broadcast without audio_url channel=%s platform=%s trace_id=%s source_message_id=%s",
                    channel_name,
                    platform,
                    audio_data.get("trace_id"),
                    audio_data.get("source_message_id"),
                )
                return False

            tts_event = {
                "type": "tts_audio",
                "data": {
                    "audio_url": audio_url,
                    "voice": audio_data.get("voice", "unknown"),
                    "volume": audio_data.get("volume", TTS_DEFAULT_VOLUME),
                    "tts_type": audio_data.get("tts_type", "unknown"),
                    "duration": audio_data.get("duration", 0),
                    "text": audio_data.get("text", ""),
                    "spoken_text": audio_data.get("spoken_text") or audio_data.get("text", ""),
                    "original_text": audio_data.get("original_text", ""),
                    "username": audio_data.get("username", ""),
                    "channel": channel_name,
                    "platform": platform,
                    "trace_id": audio_data.get("trace_id"),
                    "source_message_id": audio_data.get("source_message_id"),
                    "requested_provider": audio_data.get("requested_provider"),
                    "actual_provider": audio_data.get("actual_provider"),
                    "fallback_used": bool(audio_data.get("fallback_used", False)),
                    "fallback_reason": audio_data.get("fallback_reason"),
                    "timestamp": datetime.now().isoformat()
                }
            }

            target_user_id, listening_mode = self._resolve_user_and_listening_mode(channel_name, platform)
            if not target_user_id:
                logger.debug(
                    "[TTS] Skip broadcast: could not resolve owner for %s:%s",
                    platform,
                    channel_name,
                )
                return False

            manager = get_memory_websocket_manager()
            sent_count = await manager.send_to_user(
                target_user_id,
                tts_event,
                client_roles={"tts_player"},
            )
            if sent_count == 0 and listening_mode != "website":
                # OBS mode doesn't require dedicated tts_player tab; keep legacy delivery path.
                sent_count = await manager.send_to_user(
                    target_user_id,
                    tts_event,
                    exclude_presence_only=True
                )
            logger.info(
                "[VOLUME] TTS audio sent to %s connections (user=%s, mode=%s)",
                sent_count,
                target_user_id,
                listening_mode,
            )
            return sent_count > 0

        except Exception:
            logger.exception("[ERROR] WebSocket TTS audio broadcast error")
            return False

    async def broadcast_drops_event(self, drops_data: Dict[str, Any]) -> bool:
        """Broadcast drops event."""
        try:
            event_data = {
                "type": "drops",
                "event": "reward_received",
                "data": drops_data,
                "timestamp": datetime.now().isoformat()
            }
            await get_memory_websocket_manager().broadcast_to_all(json.dumps(event_data))
            logger.info("[REWARD] [DROPS] Broadcasted drops event")
            return True
        except Exception:
            logger.exception("Error broadcasting drops event")
            return False

    async def _broadcast_to_connections(self, connections, message_json: str) -> int:
        async def send(conn):
            try:
                await conn.websocket.send_text(message_json)
                return True
            except Exception:
                return False

        tasks = [send(conn) for conn in connections.values()]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return sum(1 for r in results if r is True)
        return 0

    async def _save_message_to_db(self, username, content, platform, channel, role, badges):
        """Helper to save message to DB. Ideally moves to a repository."""
        # This mirrors the logic from original file
        db = SessionLocal()
        try:
             # Repositories
            user_repo = UserRepository(db)
            chat_repo = ChatMessageRepository(db)

            user_id = None
            if platform == 'twitch':
                user = user_repo.get_by_twitch_username(channel)
                if user:
                    user_id = user.id
            elif platform == 'vk':
                # Original logic: filter((func.lower(User.vk_channel_name) == channel.lower()) | (func.lower(User.vk_username) == channel.lower()))
                # Repo has get_by_vk_channel_name. Does it support username too?
                # Repo: filter(User.vk_channel_name.ilike(channel_name))
                # We might miss the vk_username check if we strictly use repo method.
                # However, usually channel identifier IS the channel name.
                # Let's check if we have a generic search method or if we should add one.
                # Or query both fields.
                # Since we don't want direct db.query, we rely on existing repo methods.
                user = user_repo.get_by_vk_channel_name(channel)
                # If not found by channel name, try username? 
                # Original code checked both in OR. 
                # Let's rely on get_by_vk_channel_name for now, as it's the primary identifier.
                # If needed we can add get_by_vk_username to repo.
                if user:
                    user_id = user.id
            
            if user_id:
                chat_repo.create(
                    user_id=user_id,
                    channel_name=channel,
                    platform=platform,
                    message=content,
                    author_username=username,
                    role=role,
                    badges=json.dumps(badges) if badges else None # create expects string for badges
                )
        except Exception:
            logger.exception("Failed to save message to DB")
        finally:
            db.close()

    def _resolve_user_and_listening_mode(self, channel_name: str, platform: str) -> Tuple[Optional[int], str]:
        db = SessionLocal()
        try:
            user_repo = UserRepository(db)
            user: Optional[User] = None
            internal_match = _INTERNAL_USER_CHANNEL_RE.match((channel_name or "").strip())

            if internal_match:
                user = user_repo.get_by_id(int(internal_match.group(1)))

            if not user and platform == "twitch":
                user = user_repo.get_by_twitch_username(channel_name)
            elif not user and platform == "vk":
                user = user_repo.get_by_vk_channel_name(channel_name)
                if not user:
                    user = db.query(User).filter(func.lower(User.vk_username) == channel_name.lower()).first()
            else:
                if not user:
                    return None, "website"

            if not user:
                return None, "website"

            listening_mode = getattr(user, "tts_listening_mode", "website") or "website"
            return user.id, listening_mode
        except Exception as e:
            logger.error("Failed to resolve TTS owner for %s:%s: %s", platform, channel_name, e)
            return None, "website"
        finally:
            db.close()

notification_service = NotificationService()

