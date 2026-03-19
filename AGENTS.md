# Repository Guidelines

Contribute with small, focused changes. If behavior changes, update the docs in `docs/` and this guide.

## Project Structure & Module Organization

- `bot_service/`: FastAPI backend. Layers include `api/` (routes), `services/` (logic), `repositories/` (data access), `core/` (config/auth), and `tests/`.
- `frontend/`: React + Vite app (`src/`) with assets in `public/`.
- ChatBox UI: settings modal in `frontend/src/components/ChatBoxSettingsModal.tsx`, preview in `frontend/src/features/chatbox/components/PreviewPanel.tsx` (keep preview/overlay behavior aligned).
- `F5_tts/`: Advanced F5 TTS service (`python main.py`), prepared for extraction to standalone repository.
- Local Qwen/F5 provider instances are external and connected via `bot_service` local TTS endpoints.
- `deploy/`: Docker compose and deployment assets.
- `docs/`: architecture and developer guides.
- `scripts/`: project tooling (e.g., design system migration).
- `scripts/dev/`: one-off debug/diagnostic scripts (keep root clean).
- `logs/`: runtime logs (for example `logs/bot_service.log`, `logs/f5_tts.log`).

## Build, Test, and Development Commands

- Activate venv (Windows): `.\.venv\Scripts\Activate.ps1`.
- Backend (local): `cd bot_service; python main.py` (Uvicorn is launched from `main.py`).
- Migrations: `cd bot_service; alembic upgrade head`.
- Frontend (local): `cd frontend; npm install; npm run dev`.
- Frontend build: `cd frontend; npm run build`.
- Frontend lint/format/type-check: `npm run lint`, `npm run format`, `npm run type-check`.
- Frontend tests: `npm run test` or `npm run test:coverage`.
- TTS services: `cd F5_tts; python main.py`.
- Docker dev stack: `.\start-dev.ps1` (uses compose files in `deploy/`).
- API types: `cd frontend; npm run generate-api-types`.

## Coding Style & Naming Conventions

- Python: 4-space indent, max line length 120; format with `ruff format .`, lint with `ruff check .`.
- TypeScript/React: ESLint + Prettier via `npm run lint` and `npm run format`.
- Tests follow pytest naming in `bot_service/pytest.ini` (`test_*.py`, `Test*`, `test_*`).

## Testing Guidelines

- Backend: `pytest` in `bot_service/` with coverage; `--cov-fail-under=80` enforced.
- Frontend: Vitest (`npm run test`, `npm run test:run`, `npm run test:coverage`).
- Place tests next to features in `bot_service/tests/` or `frontend/src/`.

## Commit & Pull Request Guidelines

- Commits are a mix of Conventional Commits and short summaries. Prefer `feat:`, `fix:`, `docs:`, `chore:`, `refactor:` with optional scopes (e.g., `fix(frontend): ...`).
- PRs should include: a short description, affected services, test commands run, and screenshots for UI changes.

## Security & Configuration

- Use per-service `.env` files: `bot_service/.env`, `F5_tts/.env`, `frontend/.env`. Never commit secrets.
- Deployment guidance lives in `docs/setup/DEPLOYMENT.md`.

## Repository Hygiene

- Keep temporary/local tooling artifacts out of commits (`.playwright-cli/`, `.playwright/`, `playwright-report/`, `*.har`).
- Remove stale cache/build outputs before release-oriented PRs (`__pycache__/`, `.ruff_cache/`, frontend build dirs, temp audio files, `tmp_runtime_logs/`, root debug logs like `Qwen_logs.txt`).
- Favor small, reviewable cleanup commits over large mixed refactors.
- Place one-off backend fix/migration scripts into `bot_service/scripts/archive/legacy/`; keep only operational scripts at `bot_service/scripts/` root.
- Keep text files in UTF-8 and fix mojibake instead of carrying broken strings/comments forward.

## Agent-Specific Instructions

- Automated agents should read `docs/PROJECT_CONTEXT.md` before large changes.

## Authentication & Authorization Rules

- User OAuth entrypoints are `/auth/twitch/login` and `/auth/vk/login` (plus `/auth/vk` alias for VK).
- User OAuth callbacks are `/auth/twitch/callback` and `/auth/vk/callback`; both must validate CSRF `state` from cookies (`oauth_state` / `oauth_state_vk`).
- User OAuth callbacks must continue to use shared `oauth_handler.handle_oauth_callback(...)` + `oauth_handler.create_oauth_response(...)` for unified session/token behavior.
- Session auth is cookie-based (`session_id`). Protected API auth uses `get_current_user`; source of truth is DB user state, not stale session payload.
- Login creates/replaces active session(s); sessions are intentionally long-lived and should not be dropped on tab switch/browser restart.
- On new login (not integration-link flow), tokens from other platforms are deactivated for security. In linking flow, existing platform tokens remain active.
- Guest mode is deprecated/removed; do not add new anonymous-auth flows by default.

### Bot OAuth Rules

- Bot OAuth login endpoints are `/auth/twitch/bot/login` and `/auth/vk/bot/login`; callback endpoints are `/auth/twitch/bot/callback` and `/auth/vk/bot/callback`.
- Bot OAuth login is admin-gated: valid admin session OR short-lived admin link token (`bot_oauth_token`).
- One-time admin link tokens are issued by `/api/admin/bot/twitch/login-link` and `/api/admin/bot/vk/login-link` (10-minute TTL).
- Bot tokens are stored encrypted in DB (`bot_tokens`) and are the runtime source of truth; legacy env token fallback is not used at runtime.
- After successful bot OAuth callback, token is saved and bot restart is triggered to apply fresh credentials.
- Bot token status/refresh endpoints must preserve `seconds_left`/`hours_left` fields and near-expiry refresh logic (`needs_refresh` based on threshold, not day rounding).
- If `bot_tokens` is empty, runtime may auto-bootstrap from eligible admin user OAuth tokens when `BOT_TOKEN_AUTO_BOOTSTRAP_*` settings allow it.

### Auth Performance & Security

- Keep auth endpoint rate limits (`limiter`) on login/callback/refresh/status routes.
- `/api/auth/status` is optimized: do not restore synchronous external token validation in this endpoint; validation should occur when token is actually used.
- Admin authorization source is `users.role='admin'`; `users.is_admin` is legacy compatibility only.
- Inter-service auth for external TTS upstreams uses strict API-key headers. `bot_service` sends both `Authorization: Bearer <key>` and `X-API-Key: <key>` where supported.
- Do not reintroduce service-JWT or `X-Internal-Service-Key` expectations into the current external TTS upstream contract unless a dedicated migration plan is documented.
- If internal HTTPS is used, `bot_service` may additionally enable mTLS client cert mode via `INTERNAL_SERVICE_MTLS_*`, but mTLS does not replace the API-key contract for current TTS upstreams.

## Behavior Notes

- ChatOverlay relies on `/api/chatbox/settings/by-token` returning `twitch_user_id` to load 7TV channel emotes. Keep this field in sync when touching chatbox settings.
- YouTube mini-player renders into the sidebar slot `#youtube-mini-player-slot` when present (GlobalPlayer uses a portal).
- Real-time app sync uses WebSocket (not SSE): client messages (`ping`/`pong`, setting broadcasts), role-aware presence (`client_role=tts_player`), and connection-state reconciliation depend on bidirectional transport.
- Shared frontend WebSocket is single-leader per user across tabs (BroadcastChannel election). Preserve this behavior when changing reconnect or heartbeat logic.
- Browser TTS playback is active only when `listening_mode` is `website`; when set to `obs`, in-app TTS playback is suppressed (mode is persisted in `tts_listening_mode`).
- Browser TTS now plays only via dedicated `/tts-player` tab (`client_role=tts_player`); without this tab, website-mode TTS generation stays disabled.
- Only one `/tts-player` tab is active for playback at a time; passive tabs stay connected but do not enqueue/play audio until they take control.
- TTS synthesis is now sink-aware: website mode requires an active `/tts-player` connection, OBS mode requires an active OBS socket; queued tasks are dropped when sinks disappear.
- Gateway-managed `f5` and proxied `qwen` synthesis must return gateway/backend-served audio URLs; frontend/runtime should not depend on direct provider `/api/tts/audio/*` URLs.
- Cloud/self-hosted routing source of truth is `advanced_provider` plus provider-specific `f5_mode` / `qwen_mode`; legacy `use_local_tts` and `local_tts_endpoints.use_local` are compatibility mirrors only.
- Managed Qwen model catalog may be backend-filtered by `QWEN_CLOUD_ALLOWED_MODELS`; self-hosted Qwen model catalogs must continue to reflect the connected user endpoint as-is.
- Qwen voice management now includes admin/global routes in the upstream contract; admin UI gating should use provider capability `voice_admin`.
- Qwen voice preview/test should fail fast with a readable warmup/model-loading message; admin/user preview flows must not sit in multi-minute pending state.
- If F5/Qwen voice CRUD upstreams are unreachable, backend voice-management routes should surface `503` instead of silently returning empty voice lists.
- Qwen voice preview/test timeout is backend-configurable via `QWEN_VOICE_PREVIEW_TIMEOUT_SECONDS` and defaults to `60s`.
- TTS/YouTube autoplay must not resume automatically after full page reload; explicit user action is required to start playback again.
- Audio priority controls were removed; TTS no longer pauses/resumes YouTube automatically.
- YouTube queue bans set queue items to `status='banned'` and prevent re-adding the same video via `/api/youtube/queue/ban/{queue_id}`.
- Google Cloud TTS voice pools are stored in `tts_user_settings.gcloud_voices`, sorted by quality (Gemini/Chirp/Neural2 first). If at least one Gemini voice is selected, runtime randomization uses only Gemini voices; otherwise it falls back to the broader premium pool.
- Google Cloud voice list and persisted selection now allow only Gemini and Chirp3-HD families; legacy voice families are filtered out in API and ignored in runtime selection.
- Google Cloud preview endpoint (`/api/tts/gcloud/preview`) returns `requested_model` and `fallback_used` to diagnose when Gemini requests degrade to fallback voices.
- Gemini speaker ids without locale prefix (for example `Kore`, `Aoede`) are treated as Gemini requests in backend voice resolution, not as default Standard voices.
- Default Gemini model mapping uses `gemini-2.5-flash-tts` unless a preview request explicitly overrides `model_name`.
- Google Cloud voice style prompt is now backend-controlled via `tts_user_settings.gcloud_mood` (`neutral`/`sad`/`happy`); runtime and preview both use this mood mapping instead of free-form user prompts.
- Frontend polling intervals are adaptive (reduced outside relevant pages); background polling is disabled where supported.
- Drops config/rewards responses are cached for 60s server-side with invalidation on updates; dashboard quick actions refresh every 120s.
- Stream title/category changes broadcast `stream_info_updated` over WebSocket; Twitch stream info cache is kept in sync.
- Dashboard stream-info polling runs every 120s; stream-info cache applies to both Twitch and VK.
- Frontend TTS playback is gated by `tts_enabled` and `tts_enabled_platforms` (stored in localStorage) for UI sync.
- Frontend localStorage query cache (`rq_cache_`) is auto-pruned by age and count on app startup.
- VK Live chat badges are passed as image URLs; VK smiles are sent as emotes with positions.
- Google Cloud TTS engine is available as `gcloud`; preferred auth is ADC (`gcloud auth application-default login` + quota project), API key fallback remains supported.
- Rotating backend log file level is configurable via `LOG_FILE_LEVEL` (default `WARNING`), while console verbosity follows `LOG_LEVEL`.
- VK Live bot connects to all active VK channels on startup.
- Twitch/VK bot accounts use OAuth bot tokens stored in DB (refreshable); runtime does not use legacy env fallback tokens.
- Bot token status APIs now return `seconds_left`/`hours_left` in addition to `days_left`; `needs_refresh` is driven by near-expiry (15 minutes), not whole-day rounding.
- VK Live HTTP polling first falls back from dev API to prod API on `401`, then tries token refresh with cooldown to avoid rapid refresh loops.
- If `bot_tokens` is empty, runtime can auto-bootstrap from existing active admin OAuth user tokens (`BOT_TOKEN_AUTO_BOOTSTRAP_*` settings).
- Bot OAuth endpoints (`/auth/twitch/bot/login`, `/auth/vk/bot/login`) accept admin session or short-lived admin link token (`bot_oauth_token`), issued via `/api/admin/bot/twitch/login-link` and `/api/admin/bot/vk/login-link`.
- Admin provisioning is role-based (`users.role='admin'`); bootstrap admin endpoint is removed.
- Admin authority is sourced from `users.role`; `users.is_admin` is kept only for legacy compatibility.
- MemeAlerts: `!memegrant <nickname> <amount>` uses MemeAlerts API lookup and grant endpoints; dashboard can show grant/purchase history when connected.
- MemeAlerts: `!givema <nickname> <amount>` is an alias of `!memegrant` on Twitch and VK.
- MemeAlerts grant nickname resolution uses `user/find` with fallback to `user/find/streamer`; when upstream rejects with `401/403`, backend returns explicit hint that the user may not yet be present in channel supporters.
- MemeAlerts history source: `Выдачи` come from local DB table `memealerts_grant_history`, and `Покупки` come from MemeAlerts `POST /supporters` with `streamerId`.
- MemeAlerts points-reward auto grants use `tts_user_settings.youtube_settings.memealerts_settings.points_reward`; Twitch match is by `reward_id`, VK match is by reward title from ChatBot message.
- MemeAlerts donation auto-conversion uses `tts_user_settings.youtube_settings.memealerts_settings.donation_auto` and requires connected DonationAlerts token before enabling.
- Admin UI consistency: prefer semantic tokens (`text-foreground`, `text-muted-foreground`, `bg-card`, `border-border`) over hardcoded gray/white classes; keep heading hierarchy, spacing, and button heights (`h-8`/`h-9`) consistent between admin pages.
- Admin panel routes must stay aligned between tabs and direct URLs: `/dashboard/dolbaebadmintts`, `/dashboard/dolbaebadmintts/bots`, `/dashboard/dolbaebadmintts/voices`, `/dashboard/dolbaebadmintts/users`, `/dashboard/dolbaebadmintts/channels`, `/dashboard/dolbaebadmintts/logs`, `/dashboard/dolbaebadmintts/monitoring`.
