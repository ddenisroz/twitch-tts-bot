# Локальная интеграция TTS

Последнее обновление: 2026-03-16

Документ фиксирует активный контракт внешнего TTS-стека вокруг `bot_service`.

## Цель

Сохранить единый frontend boundary и разделить три режима работы TTS:

- `self-hosted endpoint` — пользователь поднимает TTS у себя и подключает URL
- `project-hosted worker` — отдельный воркер под инфраструктурой проекта
- `gateway-managed` — путь `bot_service -> tts-gateway -> project-hosted worker`

Voice/admin CRUD живёт на стороне upstream-провайдера.

## Термины

- `self-hosted endpoint` — пользовательский endpoint, который настраивается через экран Local TTS
- `project-hosted worker` — отдельный runtime-воркер проекта, это не пользовательский self-hosted режим
- `gateway-managed` — управляемый путь через `tts-gateway`

Флаги `use_local`, `f5_local`, `qwen_local` сохранены только как совместимые имена; источник истины для выбора cloud/self-hosted — `advanced_provider` + `f5_mode` / `qwen_mode`.

## Runtime-контракт

1. Auth mode для upstreams — strict API key
2. `bot_service` отправляет оба заголовка:
   - `Authorization: Bearer <key>`
   - `X-API-Key: <key>`
3. Для direct voice/admin вызовов используй provider-specific key (`F5_TTS_SERVICE_API_KEY`, `QWEN_TTS_SERVICE_API_KEY`), а не только `TTS_GATEWAY_API_KEY`
4. Managed `qwen` synthesis требует настроенный `TTS_GATEWAY_URL`
5. Gateway-managed `f5` и proxy-режим `qwen` должны отдавать gateway-hosted `audio_url`, а не прямой provider URL
6. Qwen voice/admin CRUD использует `QWEN_VOICE_SERVICE_URL`, а если он пуст, fallback идет на `QWEN_TTS_SERVICE_URL`
7. `local_tts_endpoints` в runtime означают именно пользовательские self-hosted endpoints

## Нужные backend env

```env
TTS_GATEWAY_URL=http://localhost:8010
TTS_GATEWAY_API_KEY=<gateway-key>

F5_TTS_SERVICE_URL=http://localhost:8011
F5_TTS_SERVICE_API_KEY=<f5-key>

QWEN_TTS_SERVICE_URL=http://localhost:8012
QWEN_TTS_SERVICE_API_KEY=<qwen-key-or-empty>
QWEN_VOICE_SERVICE_URL=
QWEN_VOICE_PREVIEW_TIMEOUT_SECONDS=60
QWEN_ALLOWED_MODELS=
QWEN_CLOUD_ALLOWED_MODELS=

LOCAL_TTS_ALLOWED_HOSTS=localhost,127.0.0.1,::1,host.docker.internal,f5_tts,tts_service,qwen_tts,qwen_service
LOCAL_TTS_ALLOWED_CIDRS=127.0.0.0/8,::1/128
```

## Upstream-репозитории

- `tts-gateway`: `https://github.com/ddenisroz/tts-gateway.git`
- `f5-tts-service`: `https://github.com/ddenisroz/f5-tts-service.git`
- `nano-qwen3tts-vllm`: `https://github.com/calldatfate/nano-qwen3tts-vllm.git`

## Порты по умолчанию

- `tts-gateway` — `8010`
- `f5-tts-service` — `8011`
- `nano-qwen3tts-vllm` — `8012`

## Ограничения по upstream

### `tts-gateway`

- требует Redis
- должен знать URL и API key для F5 и Qwen upstreams

### `f5-tts-service`

- требует свои env и БД
- для старта нужны `vendor/F5-TTS`, веса модели и зависимости prewarm

### `nano-qwen3tts-vllm`

- практически требует Linux или WSL2 runtime
- в текущем upstream нет native parity по auth, health и status
- self-hosted path в этом репозитории работает через слой совместимости
- рекомендуемый режим для прод/runtime: один контейнер = одна объявленная модель или семейство
- для single-model запуска используй `QWEN3_TTS_MODEL_PATH=<exact-model-id-or-path>`
- рекомендуемый единый ключ для обоих репозиториев: `QWEN_ALLOWED_MODELS=base` или CSV exact ids
- для ограниченного multi-model runtime используй `QWEN_TTS_ALLOWED_MODELS=base` или список exact ids через запятую, если нужен runtime-only override
- для backend-managed cloud catalog/filtering используй `QWEN_CLOUD_ALLOWED_MODELS`, если нужен backend-only override
- `QWEN_VOICE_STORAGE_DIR` должен быть вынесен в persistent volume и шариться между runtime-перезапусками

## Стабильные backend entrypoints

- `GET /api/tts/health?provider=f5|qwen|gcloud`
- `GET /api/voices/providers/capabilities`
- `GET /api/local-tts/config?provider=f5|qwen`
- `POST /api/local-tts/test-connection`
- `POST /api/local-tts/config`
- `POST /api/local-tts/toggle?provider=f5|qwen`
- `POST /api/tts/settings`
- `POST /api/tts/synthesize`

## Поведение voice/admin

- `provider=f5` — нормальный CRUD
- `provider=qwen` — user/global/admin CRUD работает через Qwen upstream; отдельный `QWEN_VOICE_SERVICE_URL` нужен только если voice API вынесен отдельно

## Базовый UI flow для self-hosted

1. Открой Local TTS settings
2. Выбери provider `f5` или `qwen`
3. Сохрани endpoint URL
4. Сохрани endpoint API key, если self-hosted worker запущен с auth. Для project-hosted Qwen на `localhost:8012` ключ обязателен.
5. Переключи нужный provider в `Self-hosted` на основной странице TTS settings
6. Во вкладке `Управление голосами` local UI показывает self-hosted пользовательские голоса для подключенного endpoint-а
7. Для self-hosted voices доступен dialog `Настроить`: `reference_text` для обоих provider-ов, а для `f5` ещё `cfg_strength` и `speed_preset`

## Рекомендуемый test asset

- для upload smoke используй [female_1.wav](/H:/Programming/raw_code/AI/Python/TTS_TTV_0.02/female_1.wav)

## Smoke-checklist

1. `GET /api/tts/health?provider=f5` возвращает healthy
2. `GET /api/tts/health?provider=qwen` возвращает healthy или контролируемый gateway-required статус
3. Synth через backend и gateway работает для `f5` и `qwen`
4. F5 voice CRUD работает через backend routes
5. Qwen voice/admin CRUD и model catalog работают через backend routes
6. Self-hosted Qwen connection checks используют compatibility probe только для synth path
7. `/api/tts/qwen/models?mode=cloud` показывает runtime-модели managed worker с backend-фильтрацией; рекомендуемый общий env — `QWEN_ALLOWED_MODELS`, backend-only override — `QWEN_CLOUD_ALLOWED_MODELS`
8. `/api/tts/qwen/models?mode=local` показывает runtime-модели пользовательского endpoint-а без backend-фильтрации
9. Если Qwen voice CRUD upstream недоступен, backend должен отдавать явный `503`, а не пустые списки голосов
10. Qwen voice preview/test ждёт warmup дольше обычного preview path; timeout управляется `QWEN_VOICE_PREVIEW_TIMEOUT_SECONDS` и по умолчанию равен `60s`
