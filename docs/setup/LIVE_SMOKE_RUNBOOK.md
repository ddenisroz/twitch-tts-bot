# Live smoke runbook

Последнее обновление: 2026-03-13

Это документ для первого end-to-end smoke без смешения инфраструктурных проблем, контрактных проблем и upstream gaps.

## Термины

- `self-hosted endpoint` — пользовательский TTS endpoint, сохранённый в `local_tts_endpoints`
- `project-hosted worker` — выделенный TTS runtime под инфраструктурой проекта
- `gateway-managed` — `bot_service -> tts-gateway -> project-hosted workers`

## Что входит в первый smoke

Обязательно:

1. `gateway-managed` synth для `f5`
2. `gateway-managed` synth для `qwen`
3. `self-hosted endpoint` для `f5`
4. `self-hosted endpoint` для `qwen` через compatibility path
5. `qwen` voice/admin CRUD через backend/upstream contract

## Preflight

Перед стартом прогони:

```powershell
.\scripts\dev\tts-smoke-preflight.ps1 -Scenario all
```

До smoke должно быть закрыто:

- `bot_service/.env` существует и заполнен
- `frontend/.env` существует и указывает на `bot_service`
- backend знает URL и API keys для TTS upstreams
- provider-specific direct keys (`F5_TTS_SERVICE_API_KEY`, `QWEN_TTS_SERVICE_API_KEY`) не подменяются одним только `TTS_GATEWAY_API_KEY`
- `LOCAL_TTS_ALLOWED_HOSTS` и `LOCAL_TTS_ALLOWED_CIDRS` настроены

## Что preflight не гарантирует

- что Redis реально доступен из `tts-gateway`
- что F5 assets и weights реально присутствуют
- что Qwen runtime реально поднят в Linux или WSL2
- что пользователь уже авторизован и может включить self-hosted режим

## Порядок запуска

1. PostgreSQL
2. Redis
3. `f5-tts-service`
4. `nano-qwen3tts-vllm`
5. `tts-gateway`
6. миграции `bot_service`
7. `bot_service`
8. `frontend`

## Базовые health checks

- `GET /health` у `bot_service`
- `GET /api/tts/health?provider=f5`
- `GET /api/tts/health?provider=qwen`
- `GET /api/voices/providers/capabilities`
- открыть `/tts-player`, потому что website-mode воспроизведение без него не стартует

## Сценарии

### S1. Gateway-managed F5

Ожидаемо:

- `f5Mode = cloud`
- `advancedProvider = f5`
- `useLocalTTS = false`
- synth проходит через gateway

### S2. Gateway-managed Qwen

Ожидаемо:

- `qwenMode = cloud`
- `advancedProvider = qwen`
- `useLocalTTS = false`
- synth проходит через gateway

### S3. Self-hosted F5

Ожидаемо:

- local endpoint успешно проходит `test-connection`
- конфиг сохраняется
- synth идёт через пользовательский self-hosted endpoint
- upload smoke использует [female_1.wav](/H:/Programming/raw_code/AI/Python/TTS_TTV_0.02/female_1.wav)

### S4. Self-hosted Qwen

Ожидаемо:

- `test-connection` проходит с compatibility warning
- synth идёт через `/api/prepare -> /api/stream/{id}` adapter
- это не считается ошибкой текущей фазы
- upload smoke использует [female_1.wav](/H:/Programming/raw_code/AI/Python/TTS_TTV_0.02/female_1.wav) и валидный `API_KEY` worker-а

### S5. Qwen voice/admin CRUD

Ожидаемо:

- capabilities помечают Qwen admin как available
- backend routes для global/admin voices отвечают успешно
- UI не показывает admin-действия, если `voice_admin=false`
- preview/test либо отрабатывает успешно, либо за разумное время возвращает понятный warmup/model-loading ответ; не должно быть многоминутного `pending`

## Критерий успеха

Первый smoke считается успешным, если:

1. `f5` и `qwen` synth работают через gateway-managed path
2. `f5` self-hosted endpoint работает
3. `qwen` self-hosted endpoint работает через compatibility path
4. `qwen` voice/admin CRUD работает через backend/upstream contract
5. frontend везде остаётся backend-only
