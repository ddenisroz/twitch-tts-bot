from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from repositories.tts_settings_repository import TTSSettingsRepository
from services.tts.tts_service import (
    BlockTargetNotFoundError,
    BlockTargetVerificationUnavailableError,
    TTSService,
)


@pytest.fixture
def mock_db():
    return MagicMock(spec=Session)


@pytest.fixture
def tts_service(mock_db):
    return TTSService(mock_db)


@pytest.mark.asyncio
async def test_synthesize_success(tts_service):
    user_data = {"id": 1, "username": "test_user", "is_admin": False}
    text = "Hello world"

    tts_service.settings_repo = MagicMock(spec=TTSSettingsRepository)
    tts_service.audio_repo = MagicMock()

    mock_settings = MagicMock()
    tts_service.settings_repo.get_or_create.return_value = mock_settings
    tts_service.settings_repo.get_settings_dict.return_value = {"enable_twitch": True}

    mock_audio = MagicMock()
    mock_audio.website_volume = 50
    tts_service.audio_repo.get_or_create.return_value = mock_audio

    with patch("services.advanced_rate_limiter.advanced_rate_limiter.check_tts_rate_limit", new_callable=AsyncMock) as mock_limit:
        mock_limit.return_value = {"allowed": True}

        with patch("services.tts.tts_service.get_memory_tts_queue") as mock_get_queue:
            mock_queue = AsyncMock()
            mock_queue.add_task.return_value = "task-123"
            mock_get_queue.return_value = mock_queue

            with patch("services.advanced_rate_limiter.advanced_rate_limiter.add_tts_request", new_callable=AsyncMock):
                result = await tts_service.synthesize(text, user_data)

    assert result["success"] is True
    assert result["task_id"] == "task-123"
    mock_queue.add_task.assert_called_once()
    assert mock_queue.add_task.await_args.kwargs["user_id"] == 1


@pytest.mark.asyncio
async def test_synthesize_rate_limited(tts_service):
    user_data = {"id": 1, "username": "test_user"}

    with patch("services.advanced_rate_limiter.advanced_rate_limiter.check_tts_rate_limit", new_callable=AsyncMock) as mock_limit:
        mock_limit.return_value = {"allowed": False}

        result = await tts_service.synthesize("Hello world", user_data)

    assert result["success"] is False
    assert result["error"] == "Rate limit exceeded"


@pytest.mark.asyncio
async def test_update_settings_version_conflict(tts_service):
    tts_service.settings_repo = MagicMock()

    mock_settings = MagicMock()
    mock_settings.version = 2
    tts_service.settings_repo.get_or_create.return_value = mock_settings

    result = await tts_service.save_tts_settings(user_id=1, client_version=1)

    assert result["success"] is False
    assert result["error"] == "Version conflict"
    assert result["current_version"] == 2


@pytest.mark.asyncio
async def test_enable_tts_success(tts_service):
    tts_service.user_repo = MagicMock()
    tts_service.token_repo = MagicMock()
    tts_service.settings_repo = MagicMock()

    mock_user = MagicMock()
    mock_user.twitch_username = "test_channel"
    mock_user.tts_enabled = True
    tts_service.user_repo.get_by_id.return_value = mock_user
    tts_service.token_repo.get_all_by_user.return_value = []
    tts_service.settings_repo.get_or_create.return_value = MagicMock(engine="gtts")

    with patch("services.tts.tts_service.get_connection_manager") as mock_cm_getter:
        mock_cm = MagicMock()
        mock_cm_getter.return_value = mock_cm

        result = await tts_service.enable_tts(user_id=1)

    assert result is True
    tts_service.user_repo.update.assert_called_with(mock_user, {"tts_enabled": True})
    mock_cm.enable_tts_for_channel.assert_called_with("test_channel", tts_type="basic")


@pytest.mark.asyncio
async def test_enable_tts_uses_ai_channel_type_for_qwen(tts_service):
    tts_service.user_repo = MagicMock()
    tts_service.token_repo = MagicMock()
    tts_service.settings_repo = MagicMock()

    mock_user = MagicMock()
    mock_user.twitch_username = "test_channel"
    mock_user.tts_enabled = True
    tts_service.user_repo.get_by_id.return_value = mock_user
    tts_service.token_repo.get_all_by_user.return_value = []
    tts_service.settings_repo.get_or_create.return_value = MagicMock(engine="qwen")

    with patch("services.tts.tts_service.get_connection_manager") as mock_cm_getter:
        mock_cm = MagicMock()
        mock_cm_getter.return_value = mock_cm

        result = await tts_service.enable_tts(user_id=1)

    assert result is True
    mock_cm.enable_tts_for_channel.assert_called_with("test_channel", tts_type="ai")


@pytest.mark.asyncio
async def test_save_tts_settings_syncs_connection_manager_channel_type_when_enabled(tts_service):
    tts_service.settings_repo = MagicMock()
    tts_service.user_repo = MagicMock()
    tts_service.token_repo = MagicMock()

    current_settings = MagicMock()
    current_settings.version = 1
    current_settings.engine = "gtts"
    current_settings.advanced_provider = "f5"
    current_settings.f5_mode = "cloud"
    current_settings.qwen_mode = "cloud"
    tts_service.settings_repo.get_or_create.return_value = current_settings

    updated_settings = MagicMock(engine="qwen", version=2)
    tts_service.settings_repo.update_settings.return_value = updated_settings

    enabled_user = MagicMock()
    enabled_user.twitch_username = "test_channel"
    enabled_user.tts_enabled = True
    tts_service.user_repo.get_by_id.return_value = enabled_user
    tts_service.token_repo.get_all_by_user.return_value = []

    with patch("services.tts.tts_service.get_connection_manager") as mock_cm_getter:
        mock_cm = MagicMock()
        mock_cm_getter.return_value = mock_cm
        with patch("services.memory_websocket_manager.get_memory_websocket_manager") as mock_get_ws_manager:
            mock_ws_manager = AsyncMock()
            mock_get_ws_manager.return_value = mock_ws_manager

            result = await tts_service.save_tts_settings(
                user_id=1,
                engine="qwen",
                advanced_provider="qwen",
                qwen_mode="cloud",
            )

    assert result["success"] is True
    mock_cm.enable_tts_for_channel.assert_called_with("test_channel", tts_type="ai")
    mock_ws_manager.sync_user_tts_generation.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_save_tts_settings_keeps_provider_mode_authoritative_over_legacy_use_local_flag(tts_service):
    tts_service.settings_repo = MagicMock()
    tts_service.user_repo = MagicMock()
    tts_service.token_repo = MagicMock()

    current_settings = MagicMock()
    current_settings.version = 1
    current_settings.engine = "qwen"
    current_settings.advanced_provider = "qwen"
    current_settings.f5_mode = "cloud"
    current_settings.qwen_mode = "cloud"
    tts_service.settings_repo.get_or_create.return_value = current_settings
    tts_service.settings_repo.update_settings.side_effect = lambda settings, payload: MagicMock(**payload, version=2)

    disabled_user = MagicMock()
    disabled_user.tts_enabled = False
    disabled_user.twitch_username = None
    tts_service.user_repo.get_by_id.return_value = disabled_user
    tts_service.token_repo.get_all_by_user.return_value = []

    with patch("services.memory_websocket_manager.get_memory_websocket_manager") as mock_get_ws_manager:
        mock_ws_manager = AsyncMock()
        mock_get_ws_manager.return_value = mock_ws_manager
        result = await tts_service.save_tts_settings(
            user_id=1,
            advanced_provider="qwen",
            qwen_mode="cloud",
            use_local_tts=True,
        )

    assert result["success"] is True
    saved_payload = tts_service.settings_repo.update_settings.call_args.args[1]
    assert saved_payload["qwen_mode"] == "cloud"
    assert saved_payload["use_local_tts"] is False


@pytest.mark.asyncio
async def test_save_tts_settings_preserves_exact_qwen_model_id(tts_service):
    tts_service.settings_repo = MagicMock()
    tts_service.user_repo = MagicMock()
    tts_service.token_repo = MagicMock()

    current_settings = MagicMock()
    current_settings.version = 1
    current_settings.engine = "qwen"
    current_settings.advanced_provider = "qwen"
    current_settings.f5_mode = "cloud"
    current_settings.qwen_mode = "cloud"
    tts_service.settings_repo.get_or_create.return_value = current_settings
    tts_service.settings_repo.update_settings.side_effect = lambda settings, payload: MagicMock(**payload, version=2)

    disabled_user = MagicMock()
    disabled_user.tts_enabled = False
    disabled_user.twitch_username = None
    tts_service.user_repo.get_by_id.return_value = disabled_user
    tts_service.token_repo.get_all_by_user.return_value = []

    custom_model = "Qwen/Qwen3-TTS-12Hz-9.9B-Base"
    with patch("services.memory_websocket_manager.get_memory_websocket_manager") as mock_get_ws_manager:
        mock_ws_manager = AsyncMock()
        mock_get_ws_manager.return_value = mock_ws_manager
        result = await tts_service.save_tts_settings(
            user_id=1,
            advanced_provider="qwen",
            qwen_model=custom_model,
        )

    assert result["success"] is True
    saved_payload = tts_service.settings_repo.update_settings.call_args.args[1]
    assert saved_payload["qwen_model"] == custom_model


@pytest.mark.asyncio
async def test_ensure_block_target_exists_accepts_known_vk_chat_user(tts_service):
    tts_service.user_repo = MagicMock()
    tts_service.chat_repo = MagicMock()
    tts_service.user_repo.get_by_vk_username.return_value = None
    tts_service.user_repo.get_by_vk_channel_name.return_value = None
    tts_service.chat_repo.author_exists_in_channel.return_value = True

    resolved = await tts_service.ensure_block_target_exists(
        user_id=1,
        channel_name="owner_channel",
        platform="vk",
        username="@ViewerName",
    )

    assert resolved == "viewername"


@pytest.mark.asyncio
async def test_ensure_block_target_exists_rejects_unknown_twitch_user(tts_service):
    tts_service.user_repo = MagicMock()
    tts_service.chat_repo = MagicMock()
    tts_service.chat_repo.author_exists_in_channel.return_value = False
    tts_service.user_repo.get_by_twitch_username.return_value = None

    with patch.object(tts_service, "_resolve_twitch_username_exists", AsyncMock(return_value=False)):
        with pytest.raises(BlockTargetNotFoundError):
            await tts_service.ensure_block_target_exists(
                user_id=1,
                channel_name="owner_channel",
                platform="twitch",
                username="ghost_user",
            )


@pytest.mark.asyncio
async def test_ensure_block_target_exists_returns_503_when_verification_unavailable(tts_service):
    tts_service.user_repo = MagicMock()
    tts_service.chat_repo = MagicMock()
    tts_service.chat_repo.author_exists_in_channel.return_value = False
    tts_service.user_repo.get_by_twitch_username.return_value = None

    with patch.object(tts_service, "_resolve_twitch_username_exists", AsyncMock(return_value=None)):
        with pytest.raises(BlockTargetVerificationUnavailableError):
            await tts_service.ensure_block_target_exists(
                user_id=1,
                channel_name="owner_channel",
                platform="twitch",
                username="ghost_user",
            )
