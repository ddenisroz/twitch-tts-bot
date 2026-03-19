from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.notification_service import NotificationService


def test_resolve_user_and_listening_mode_supports_internal_user_channel():
    service = NotificationService()
    mock_db = MagicMock()
    mock_repo = MagicMock()
    mock_user = SimpleNamespace(id=5, tts_listening_mode="website")
    mock_repo.get_by_id.return_value = mock_user

    with patch("services.notification_service.SessionLocal", return_value=mock_db):
        with patch("services.notification_service.UserRepository", return_value=mock_repo):
            user_id, listening_mode = service._resolve_user_and_listening_mode("user_5", "twitch")

    assert user_id == 5
    assert listening_mode == "website"
    mock_repo.get_by_id.assert_called_once_with(5)
    mock_db.close.assert_called_once()


def test_resolve_user_and_listening_mode_falls_back_to_provider_channel_lookup():
    service = NotificationService()
    mock_db = MagicMock()
    mock_repo = MagicMock()
    mock_user = SimpleNamespace(id=7, tts_listening_mode="obs")
    mock_repo.get_by_id.return_value = None
    mock_repo.get_by_twitch_username.return_value = mock_user

    with patch("services.notification_service.SessionLocal", return_value=mock_db):
        with patch("services.notification_service.UserRepository", return_value=mock_repo):
            user_id, listening_mode = service._resolve_user_and_listening_mode("yourchy", "twitch")

    assert user_id == 7
    assert listening_mode == "obs"
    mock_repo.get_by_twitch_username.assert_called_once_with("yourchy")
    mock_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_broadcast_tts_audio_skips_when_audio_url_missing():
    service = NotificationService()
    manager = MagicMock()
    manager.send_to_user = AsyncMock(return_value=1)

    with patch.object(service, "_resolve_user_and_listening_mode", return_value=(1, "website")):
        with patch("services.notification_service.get_memory_websocket_manager", return_value=manager):
            result = await service.broadcast_tts_audio(
                audio_data={"trace_id": "trace-1", "source_message_id": "msg-1"},
                channel_name="yourchy",
                platform="twitch",
            )

    assert result is False
    manager.send_to_user.assert_not_awaited()
