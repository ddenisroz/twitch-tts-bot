# bot_service/core/config.py
"""
Centralized configuration management using pydantic-settings
Replaces hardcoded values and os.getenv() calls throughout the application
"""
import logging
import os
import sys
import ipaddress
from typing import Optional, List
from pydantic import Field, field_validator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings with validation and type safety"""

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # === ENVIRONMENT ===
    environment: str = Field(default="development", description="Environment: development, production")
    debug: bool = Field(default=True, description="Enable debug mode")
    log_level: str = Field(default="INFO", description="Logging level")

    # === SECURITY ===
    secret_key: str = Field(
        default="your-secret-key-here-generate-with-openssl-rand-hex-32",
        description="Secret key for JWT token signing"
    )
    algorithm: str = Field(default="HS256", description="JWT algorithm")
    token_encryption_key: str = Field(
        default="your-encryption-key-here-generate-with-fernet",
        description="Fernet key for OAuth token encryption"
    )

    # === SERVICE URLS ===
    bot_service_host: str = Field(default="0.0.0.0", description="Bot service host")
    bot_service_port: int = Field(default=8000, description="Bot service port")
    backend_url: str = Field(default="http://localhost:8000", description="Backend URL")
    frontend_url: str = Field(default="http://localhost:5173", description="Frontend URL")
    tts_gateway_url: str = Field(
        default="",
        description="Optional unified TTS gateway URL for provider routing (f5/qwen)",
    )
    tts_gateway_api_key: Optional[str] = Field(
        default=None,
        description="Strict API key for bot_service -> tts-gateway requests",
    )
    f5_tts_service_url: str = Field(
        default="http://localhost:8011",
        description="F5 TTS service URL",
    )
    f5_tts_service_api_key: Optional[str] = Field(
        default=None,
        description="Strict API key for bot_service -> f5-tts-service requests",
    )
    qwen_tts_service_url: str = Field(
        default="http://localhost:8012",
        description="Qwen TTS service URL",
    )
    qwen_tts_service_api_key: Optional[str] = Field(
        default=None,
        description="Strict API key for bot_service -> qwen upstream requests",
    )
    qwen_voice_service_url: str = Field(
        default="",
        description="Optional dedicated Qwen voice-management API URL; when empty bot_service falls back to qwen_tts_service_url",
    )
    qwen_voice_preview_timeout_seconds: float = Field(
        default=60.0,
        description=(
            "Timeout in seconds for Qwen voice preview/test calls before bot_service returns "
            "a readable warmup/model-loading error"
        ),
    )
    qwen_cloud_allowed_models: str = Field(
        default=os.getenv(
            "QWEN_CLOUD_ALLOWED_MODELS",
            os.getenv("QWEN_ALLOWED_MODELS", os.getenv("QWEN_TTS_ALLOWED_MODELS", "")),
        ),
        description=(
            "Optional backend allowlist for managed Qwen runtime models "
            "(comma-separated family aliases and/or exact model ids)"
        ),
    )
    f5_tts_storage_root: Optional[str] = Field(
        default=None,
        description="Optional path to local F5 storage root when running maintenance in split deployment",
    )
    tts_internal_api_key: Optional[str] = Field(
        default=None,
        description="Legacy internal key (compatibility only; strict API-key contract uses *_TTS_SERVICE_API_KEY vars)",
    )
    internal_service_jwt_enabled: bool = Field(
        default=True,
        description="Enable signed JWT for internal service-to-service calls",
    )
    internal_service_jwt_issuer: str = Field(
        default="bot_service",
        description="Issuer claim for internal service JWT",
    )
    internal_service_jwt_audience_tts: str = Field(
        default="f5_tts",
        description="Audience claim for bot_service -> F5 TTS service JWT",
    )
    internal_service_jwt_secret: Optional[str] = Field(
        default=None,
        description="Optional dedicated signing secret for internal service JWT (falls back to SECRET_KEY)",
    )
    internal_service_jwt_ttl_seconds: int = Field(
        default=120,
        description="TTL for internal service JWT in seconds",
    )
    internal_service_mtls_enabled: bool = Field(
        default=False,
        description="Enable mTLS client certs for internal service-to-service HTTP calls",
    )
    internal_service_ca_cert_path: Optional[str] = Field(
        default=None,
        description="Optional CA bundle path used to verify internal service TLS certificate",
    )
    internal_service_client_cert_path: Optional[str] = Field(
        default=None,
        description="Optional client certificate path for mTLS internal calls",
    )
    internal_service_client_key_path: Optional[str] = Field(
        default=None,
        description="Optional client private key path for mTLS internal calls",
    )
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        description="CORS allowed origins (comma-separated)"
    )
    allowed_origins: Optional[str] = Field(
        default=None,
        description="Legacy alias for CORS_ORIGINS (comma-separated)",
    )
    local_tts_allowed_hosts: str = Field(
        default="localhost,127.0.0.1,::1,host.docker.internal,f5_tts,tts_service,qwen_tts,qwen_service",
        description="Allowed hostnames/IPs for user-defined local TTS endpoint URLs (comma-separated)",
    )
    local_tts_allowed_cidrs: str = Field(
        default="127.0.0.0/8,::1/128",
        description="Allowed CIDRs for local TTS endpoint IPs when raw IP host is used (comma-separated)",
    )

    # === DATABASE ===
    database_url: str = Field(
        default="postgresql://user:password@localhost:5432/bot_service_db",
        description="Database connection URL"
    )
    chat_messages_db_limit_per_user: int = Field(
        default=3000,
        description="Maximum messages per user"
    )
    chat_messages_db_limit_total: int = Field(
        default=100000,
        description="Maximum total messages in database"
    )
    chat_messages_retention_days: int = Field(
        default=30,
        description="Message retention period in days"
    )

    # === RATE LIMITING ===
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting")
    max_requests_per_minute: int = Field(default=60, description="Max requests per minute")
    rate_limit_default: str = Field(default="60/minute", description="Default rate limit")
    rate_limit_login: str = Field(default="5/15minute", description="Login rate limit")
    rate_limit_tts: str = Field(default="30/minute", description="TTS rate limit")
    max_login_attempts: int = Field(default=10, description="Max login attempts")

    # === TWITCH INTEGRATION ===
    twitch_client_id: Optional[str] = Field(default=None, description="Twitch client ID")
    twitch_client_secret: Optional[str] = Field(default=None, description="Twitch client secret")
    twitch_redirect_uri: str = Field(
        default="http://localhost:8000/auth/twitch/callback",
        description="Twitch OAuth redirect URI"
    )

    # === VK LIVE INTEGRATION ===
    vk_client_id: Optional[str] = Field(default=None, description="VK client ID")
    vk_client_secret: Optional[str] = Field(default=None, description="VK client secret")
    vk_redirect_uri: str = Field(
        default="http://localhost:8000/auth/vk/callback",
        description="VK OAuth redirect URI"
    )
    vk_auth_base_url: str = Field(
        default="https://auth.live.vkvideo.ru/app/oauth2/authorize",
        description="VK auth base URL"
    )

    # === YOUTUBE INTEGRATION ===
    youtube_api_key: Optional[str] = Field(default=None, description="YouTube Data API key")

    # === DONATION ALERTS INTEGRATION ===
    donationalerts_client_id: Optional[str] = Field(default=None, description="DonationAlerts client ID")
    donationalerts_client_secret: Optional[str] = Field(default=None, description="DonationAlerts client secret")
    donationalerts_redirect_uri: str = Field(
        default="http://localhost:8000/auth/donationalerts/callback",
        description="DonationAlerts OAuth redirect URI"
    )
    donationalerts_webhook_secret: Optional[str] = Field(
        default=None,
        description="Shared secret for DonationAlerts webhook verification (header/query secret)",
    )

    # === EXTERNAL APIS ===
    google_cloud_api_key: Optional[str] = Field(default=None, description="Google Cloud API key (YouTube + TTS)")
    google_tts_api_key: Optional[str] = Field(default=None, description="Google Cloud TTS API key (fallback alias)")
    google_cloud_project_id: Optional[str] = Field(
        default=None,
        description="Google Cloud Project ID used for ADC quota project/x-goog-user-project",
    )
    huggingface_token: Optional[str] = Field(default=None, description="HuggingFace API token")
    deepseek_api_key: Optional[str] = Field(default=None, description="DeepSeek API key")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", description="DeepSeek API base URL")
    deepseek_model: str = Field(default="deepseek-chat", description="DeepSeek model name")

    # === CHAT ANALYSIS ===
    chat_analysis_channel_limit: int = Field(default=80, description="Messages from current channel for analysis")
    chat_analysis_global_limit: int = Field(default=200, description="Messages across all channels for analysis")
    chat_analysis_min_messages: int = Field(default=10, description="Minimum messages required for analysis")
    chat_analysis_output_max_chars: int = Field(default=150, description="Max analysis output length")
    chat_analysis_save_results: bool = Field(default=False, description="Save analysis results to DB")

    # === REDIS & CELERY ===
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis URL")
    celery_broker_url: str = Field(default="redis://localhost:6379/0", description="Celery broker URL")
    celery_result_backend: str = Field(default="redis://localhost:6379/1", description="Celery result backend")

    # === GTTS SETTINGS ===
    gtts_voice: str = Field(default="com", description="gTTS accent/voice (tld parameter)")

    # === LOGGING ===
    log_file: str = Field(default="logs/bot_service.log", description="Log file path")
    log_file_level: str = Field(
        default="WARNING",
        description="Log level for rotating file handler (DEBUG/INFO/WARNING/ERROR/CRITICAL)",
    )
    enable_json_logs: bool = Field(default=False, description="Enable JSON formatted logs")
    enable_log_rotation: bool = Field(default=True, description="Enable log rotation")

    # === SENTRY (ERROR TRACKING) ===
    sentry_dsn: Optional[str] = Field(default=None, description="Sentry DSN for error tracking")
    sentry_traces_sample_rate: float = Field(default=0.1, description="Sentry traces sample rate (0.0-1.0)")
    sentry_profiles_sample_rate: float = Field(default=0.1, description="Sentry profiles sample rate (0.0-1.0)")
    sentry_release: str = Field(default="bot_service@0.03", description="Sentry release version")
    sentry_debug: bool = Field(default=False, description="Enable Sentry debug mode")

    # === TESTING ===
    testing: bool = Field(default=False, description="Enable testing mode")

    # === BOT TOKEN BOOTSTRAP ===
    bot_token_auto_bootstrap_enabled: bool = Field(
        default=True,
        description="Allow auto-bootstrap of missing bot_tokens from existing user OAuth tokens",
    )
    bot_token_auto_bootstrap_admin_only: bool = Field(
        default=True,
        description="When auto-bootstrap is enabled, use only admin user tokens as source",
    )
    bot_token_auto_bootstrap_require_refresh_token: bool = Field(
        default=False,
        description="Require refresh_token on source user token for bot token auto-bootstrap",
    )

    # === COMPUTED FIELDS ===
    @computed_field
    @property
    def cors_origins_list(self) -> List[str]:
        """Parse effective CORS origins into list (supports legacy ALLOWED_ORIGINS alias)."""
        default_cors = "http://localhost:5173,http://localhost:3000"
        configured_cors = (self.cors_origins or "").strip()
        legacy_cors = (self.allowed_origins or "").strip()

        if configured_cors and (configured_cors != default_cors or not legacy_cors):
            origins_raw = configured_cors
        elif legacy_cors:
            origins_raw = legacy_cors
        else:
            origins_raw = configured_cors or default_cors

        return [origin.strip() for origin in origins_raw.split(",") if origin.strip()]

    @computed_field
    @property
    def local_tts_allowed_hosts_list(self) -> List[str]:
        """Parse local TTS endpoint host allowlist."""
        return [host.strip().lower() for host in (self.local_tts_allowed_hosts or "").split(",") if host.strip()]

    @computed_field
    @property
    def local_tts_allowed_cidrs_list(self) -> List[str]:
        """Parse local TTS endpoint CIDR allowlist."""
        return [cidr.strip() for cidr in (self.local_tts_allowed_cidrs or "").split(",") if cidr.strip()]

    @computed_field
    @property
    def is_production(self) -> bool:
        """Check if running in production"""
        return self.environment.lower() == "production"

    @computed_field
    @property
    def is_development(self) -> bool:
        """Check if running in development"""
        return self.environment.lower() == "development"

    # === VALIDATORS ===
    @field_validator('secret_key')
    @classmethod
    def validate_secret_key(cls, v: str, info) -> str:
        """Validate secret key is changed in production"""
        environment = info.data.get('environment', 'development')
        default_key = "your-secret-key-here-generate-with-openssl-rand-hex-32"

        if environment.lower() == 'production' and v == default_key:
            raise ValueError(
                " PRODUCTION ERROR: SECRET_KEY must be changed from default value! "
                "Generate with: openssl rand -hex 32"
            )

        if len(v) < 32:
            logger.warning(f"[WARN] SECRET_KEY is short ({len(v)} chars), recommended 32+ characters")

        return v

    @field_validator('token_encryption_key')
    @classmethod
    def validate_encryption_key(cls, v: str, info) -> str:
        """Validate encryption key is changed in production"""
        environment = info.data.get('environment', 'development')
        default_key = "your-encryption-key-here-generate-with-fernet"

        if environment.lower() == 'production' and v == default_key:
            raise ValueError(
                " PRODUCTION ERROR: TOKEN_ENCRYPTION_KEY must be changed from default value! "
                "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

        return v

    @field_validator('database_url')
    @classmethod
    def validate_database_url(cls, v: str, info) -> str:
        """Validate database URL format"""
        if not v:
            raise ValueError("DATABASE_URL is required")

        environment = str(info.data.get('environment', 'development')).lower()
        testing_from_env = os.getenv("TESTING", "false").strip().lower() == "true"
        pytest_mode = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
        is_testing_mode = environment in {"test", "testing"} or testing_from_env or pytest_mode

        # Runtime policy: PostgreSQL only (SQLite allowed only in explicit test mode).
        if v.startswith('sqlite') and not is_testing_mode:
            raise ValueError("Only PostgreSQL is supported for runtime DATABASE_URL")

        return v

    @field_validator('bot_service_port')
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number"""
        if not 1 <= v <= 65535:
            raise ValueError(f"Port must be between 1 and 65535, got {v}")
        return v

    @field_validator("local_tts_allowed_cidrs")
    @classmethod
    def validate_local_tts_allowed_cidrs(cls, v: str) -> str:
        """Validate LOCAL_TTS_ALLOWED_CIDRS values."""
        raw = (v or "").strip()
        if not raw:
            return v

        for cidr in [item.strip() for item in raw.split(",") if item.strip()]:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as error:
                raise ValueError(f"Invalid LOCAL_TTS_ALLOWED_CIDRS value '{cidr}'") from error

        return v

    @field_validator("internal_service_jwt_ttl_seconds")
    @classmethod
    def validate_internal_jwt_ttl(cls, v: int) -> int:
        """Validate internal JWT TTL to avoid near-eternal tokens."""
        if v < 30 or v > 3600:
            raise ValueError("INTERNAL_SERVICE_JWT_TTL_SECONDS must be between 30 and 3600")
        return v

    @field_validator('max_requests_per_minute')
    @classmethod
    def validate_rate_limit(cls, v: int) -> int:
        """Validate rate limit"""
        if v < 1:
            raise ValueError("Rate limit must be at least 1")
        return v

    @field_validator('chat_messages_db_limit_per_user', 'chat_messages_db_limit_total')
    @classmethod
    def validate_positive(cls, v: int) -> int:
        """Validate positive integers"""
        if v < 1:
            raise ValueError("Value must be positive")
        return v


def validate_settings():
    """Validate critical settings on startup"""
    required_for_production = [
        'secret_key',
        'token_encryption_key',
        'database_url',
    ]

    if settings.is_production:
        missing = []
        for key in required_for_production:
            value = getattr(settings, key, None)
            if not value or value.startswith('your-'):
                missing.append(key.upper())

        if missing:
            raise ValueError(
                f" PRODUCTION ERROR: Missing or invalid required settings: {', '.join(missing)}"
            )

    logger.info("[OK] Configuration validated successfully")


# Global settings instance
settings = Settings()

# Validate on import
try:
    validate_settings()
    logger.info(f"[FIX] Configuration loaded: environment={settings.environment}, debug={settings.debug}")
except Exception as e:
    logger.error(f"[ERROR] Configuration validation failed: {e}")
    if settings.is_production:
        raise
