# Content Fabric Bot

Telegram-бот для поэтапной генерации кулинарных сценариев и визуального пайплайна:

- идея/рецепт → **текстовая фаза** (концепт → цельный рецепт → сценарий): у каждого шага свой промпт, один цикл «LLM → показ → ОК или правка (previous + feedback снова в модель)»;
- затем сценарий → **один LLM-вызов production** (сразу `start_frame_prompt`, `end_frame_prompt`, `video_prompt` по сценам) и **один апрув** всего блока → **медиа по сценам** в `STATE_WAITING_SCENE_MEDIA`: промпт старта → картинка старта → при необходимости конец → картинка конца → апрув `video_prompt` → видео со **стартовым** и **конечным** кадром, если конец сгенерирован (подробно — §6);
- обязательные апрувы и перегенерация через нейросеть на любом этапе;
- устойчивость к лимитам Gemini (API key mode, очередь, ретраи, fallback моделей);
- генерация изображений через Vertex AI с fallback-моделями.
- генерация видео по сценам после апрува изображений (с обработкой policy/image ошибок).

---

## 1) Что умеет бот

- **Многошаговый диалог** с сохранением состояния по `chat_id`.
- **Сценарий** строится **одним запросом к LLM** по **полному тексту рецепта** после апрува (4–6 сцен, требования задаются в промпте, без циклов авто-исправления в Python).
- **Каталог текстовых шагов** `TEXT_PIPELINE_STEPS` в `constants.py` (готов к замене списком из БД для нескольких проектов); контракт потока и целевой формы сцены — `app/pipeline_flow.py`.
- **Один апрув** полного production-блока (кадры + video prompts) перед пошаговой медиа-генерацией.
- Режим **«История»**: Gemini готовит текст закадровой озвучки до минуты, после отдельного апрува ElevenLabs v4 создаёт цельную дорожку с проектным `ELEVENLABS_VOICE_ID`. Видеоряд сначала делится на законченные смысловые фразы, а каждый такой эпизод разворачивается в нужное число немых 3-секундных Kling-клипов — фразы больше не режутся для показа на механические куски по три секунды. Каждая аудиоверсия сохраняется; дополнительный платный вариант v4 создаётся только по отдельной кнопке и затем выбирается командами `Аудио 1`, `Аудио 2` и т. д. До генерации кадров бот создаёт и по очереди предлагает утвердить референсы повторяющихся исторических героев и их возрастных версий; утверждённые файлы прикладываются ко всем соответствующим сценам. Исторические сцены и character sheets выполняются как сухая акварель с тонким тушевым контуром, фактурой бумаги, приглушённой ретро-палитрой и лёгкой сепией; современные кадры остаются цветными. Ретро-сцены сохраняют эпоху, молодую версию бабушки и повторяющийся образ кота; выбранная дорожка подставляется в финальную сборку вместо звука клипов.
- Режим **«Визуализация»**: принимает аудиофайл, голосовое сообщение или видео; из видео автоматически извлекается аудиодорожка. Gemini делает дословную расшифровку с временными сегментами, после чего пользователь выбирает стилистику, а проектные персонажи и правила активного проекта применяются к немому визуальному ряду. В финальной сборке сохраняется исходная загруженная дорожка.
- Режим **«Реклама»**: принимает рецепт, бытовую инструкцию или историю, затем обязательно запрашивает фото товара. После анализа упаковки предлагает 5 разных рекламных кампаний: можно выбрать одну, запросить другие/усиленные варианты или прислать свой сюжет. Затем доступен формат **«Клип»** (3–6 вертикальных сцен Veo 3 Fast по 8 секунд, один и тот же товар обязателен в каждом кадре) или **«Пост»** (3 изображения 4:5 с очень крупным товаром, бабушкой и котом плюс готовый рекламный текст).
- **Поштучная генерация изображений** по сценам (с апрувом каждой сцены).
- **End-frame с reference**: конечный кадр генерируется с использованием стартового кадра как референса.
- Команда **`/image`** для разовой генерации изображения по промпту.
- Команда **`/fix_stress`** для принудительной правки ударений/пауз в репликах Veo3.
- Перед генерацией видео по «ОК»: если реплика `text` длиннее speech budget (`duration_sec × 14` для RU, но до 125 символов для 8-секундной RU-сцены) **без токенов `[пауза]`**, бот отправляет в Gemini описание лимита и **автоматически** сокращает `text` и согласует `CHARACTERS SPEAKS` в `video_prompt` (см. раздел 6); если модель не уложилась — сообщение про ручное сокращение или увеличение `duration_sec`.

---

## 2) Архитектура проекта

### Корень

- `main.py` — точка входа, загрузка `.env`, запуск polling.
- `config.py` — структура `ASSETS` (генерируется скриптом раскладки ассетов).
- `organize_assets.py` — утилита раскладки изображений по папкам и обновления `config.py`.
- `list_models.py` — список доступных моделей Gemini по ключам.
- `.env` — рабочие секреты (локально, не коммитить).
- `.env.example` — шаблон переменных окружения.

### Пакет `app/`

- `bot_app.py` — регистрация команд и message handler.
- `handlers.py` — state machine и Telegram UX.
- `pipeline.py` — бизнес-этапы генерации (кадры, production, видео-промпты).
- `pipeline_llm_steps.py` — промпты текстовых шагов (концепт, рецепт, сценарий) и общий вызов `gemini_text`.
- `pipeline_flow.py` — описание универсального цикла текстовой фазы и `TypedDict` целевой сцены для медиа-шага.
- `constants.py` — env-имена, состояния state machine, `TEXT_PIPELINE_STEPS`, `STRICT_LOCATIONS`.
- `scene_utils.py` — нормализация сцен и вспомогательные проверки (без тяжёлого пост-редактирования сценария в коде).
- `scene_speech_budget.py` — лимит длины реплики для Veo (14 символов речи в секунду для RU, до 125 символов для 8-секундной RU-сцены; токены `[пауза]` в счёт не входят).
- `scene_speech_shorten.py` — один запрос к Gemini: сократить `text` и вернуть полный `video_prompt` с дословным `CHARACTERS SPEAKS`.
- `vertex_images.py` — генерация изображений через SDK/REST fallback.
- `llm_utils.py` — вызовы Gemini (Vertex AI или API key), ретраи, fallback моделей.
- `llm_queue.py` — глобальная очередь LLM-вызовов.
- `formatting.py` — человекочитаемое форматирование ответов.
- `cache.py` — in-memory кэш этапов.
- `state.py` — in-memory сессии.
- `assets.py` — нормализация `ASSETS` структуры.
- `env_utils.py` — загрузка `.env`.
- `db.py` — подключение к PostgreSQL через SQLAlchemy (sync), `engine/session`, health-check.
- `models.py` — ORM-модели проектов, текстовых шагов, запусков генерации, сцен и результатов видео.
- `project_seed.py` — seed текущего дефолтного проекта из `ASSETS` и `TEXT_PIPELINE_STEPS`.

---

## 3) Установка и запуск

### Установка Poetry

Если `poetry` уже установлен и доступен в `PATH`, этот шаг можно пропустить.

```bash
python3 -m pip install --user poetry
```

Если после установки команда `poetry` не найдена, используйте эквивалент через модуль:

```bash
python3 -m poetry --version
```

### Базовый запуск

```bash
python3 -m poetry lock
python3 -m poetry install
cp .env.example .env
python3 -m poetry run python main.py
```

То же самое короткими командами (когда `poetry` в `PATH`):

```bash
poetry lock
poetry install
cp .env.example .env
poetry run python main.py
```

### Запуск с тестами (опционально)

```bash
python3 -m poetry install --with dev
python3 -m poetry run pytest
python3 -m poetry run python main.py
```

---

## 4) Настройка `.env`

Скопируйте шаблон:

```bash
cp .env.example .env
```

Минимум для запуска бота (Vertex AI):

```env
TELEGRAM_BOT_TOKEN="..."
GEMINI_MODEL="gemini-2.5-flash"
GOOGLE_CLOUD_PROJECT="your-project-id"
GOOGLE_CLOUD_LOCATION="us-central1"
VERTEX_API_KEY="..."
```

Рекомендуемый доступ к Gemini (только Vertex AI):

```env
VERTEX_API_KEY="..."
GOOGLE_CLOUD_PROJECT="your-project-id"
GOOGLE_CLOUD_LOCATION="us-central1"
```

Рекомендуется для устойчивости:

```env
GEMINI_MODEL_FALLBACKS="gemini-2.5-flash-lite,gemini-2.5-pro"
```

Для image generation:

```env
VERTEX_API_KEY="..."
GOOGLE_CLOUD_PROJECT="your-project-id"
GOOGLE_CLOUD_LOCATION="us-central1"
VERTEX_IMAGE_MODEL="gemini-3-pro-image"
VERTEX_IMAGE_MODEL_FALLBACKS="gemini-3.1-flash-image,gemini-2.5-flash-image"
VERTEX_IMAGE_GENERATE_TIMEOUT_SEC=480
```

`VERTEX_IMAGE_MODEL` — основная, самая сильная модель. `VERTEX_IMAGE_MODEL_FALLBACKS`
не перебираются автоматически: если основная модель не сработала, бот спрашивает,
повторить генерацию на сильной модели или явно понизить модель.

Профиль проекта для prompt-политик (по умолчанию кулинарный проект с бабушкой и котом):

```env
CONTENT_FABRIC_PROJECT_SCOPE="grandma_kitchen"
```

Для PostgreSQL (SQLAlchemy sync):

```env
DATABASE_URL="postgresql+psycopg2://user:password@localhost:5432/content_fabric"
```

Для доступа к Django-админке из интернета включите публичный профиль и укажите HTTPS-домен:

```env
DJANGO_PUBLIC_ADMIN=1
DJANGO_DEBUG=0
DJANGO_SECRET_KEY="long-random-secret"
DJANGO_ALLOWED_HOSTS="admin.example.com"
DJANGO_CSRF_TRUSTED_ORIGINS="https://admin.example.com"
CONTENT_FABRIC_PUBLIC_BASE_URL="https://admin.example.com"
DJANGO_ADMIN_PATH="private-admin-path"
DJANGO_STATIC_ROOT="/opt/content_fabric/runtime/django_static"
DJANGO_SECURE_SSL_REDIRECT=1
```

`DJANGO_ADMIN_PATH` меняет URL админки с `/admin/` на `/<private-admin-path>/`.
HSTS subdomains/preload включайте только если весь домен и все поддомены всегда работают по HTTPS.
Для публичного доступа используйте HTTPS reverse proxy или tunnel, который прокидывает запросы
на локальный Django-процесс. Минимальный локальный запуск для прокси:

```bash
.venv/bin/python admin_panel/manage.py check --deploy
.venv/bin/python admin_panel/manage.py collectstatic --noinput
.venv/bin/python admin_panel/manage.py createsuperuser
.venv/bin/python admin_panel/manage.py runserver 127.0.0.1:8000
```

В прокси направьте `https://admin.example.com` на `http://127.0.0.1:8000` и отдавайте
`/static/` из `DJANGO_STATIC_ROOT`. Не открывайте порт `8000` напрямую в интернет.

Публикация видео работает напрямую через официальные API YouTube, Instagram, TikTok и VK, без `n8n`.
В админке можно выбрать любую историческую финальную версию или загрузить свой MP4, явно выбрать проект,
отредактировать текст каждой площадки и отправить отмеченные цели. Настройка описана в
[docs/direct_social_publishing.md](/opt/content_fabric/docs/direct_social_publishing.md), перечень ключей — в
[docs/autoposting_secrets.md](/opt/content_fabric/docs/autoposting_secrets.md).

Для video generation используется прямой вызов Python-функции `tools.gen_video.generate_video_via_vertex`
(без внешней shell-команды и без `VIDEO_GEN_CMD_TEMPLATE`).

Для `veo3_fast` в коде выставляются дефолты: `quality=720`, `aspect_ratio=9:16`, `audio=true`.
В модуле `tools/gen_video.py` для алиаса `veo3_fast` используется стабильная модель
`veo-3.1-fast-generate-001` (можно переопределить через `VERTEX_VIDEO_MODEL_VEO3_FAST`).
Генерация видео реализована через Vertex `predictLongRunning` API (LRO) без внешней shell-команды.

Опциональная Syntax-генерация перед Vertex (Telethon):

```env
SYNTAX_FALLBACK_ENABLED=1
SYNTAX_FALLBACK_MODE=manual
SYNTAX_FALLBACK_TIMEOUT_SEC=900
SYNTAX_FALLBACK_POLL_SEC=10
SYNTAX_PROMPT_MAX_CHARS=2000
SYNTAX_BOT_USERNAME="@syntxaibot"
TELEGRAM_API_ID="123456"
TELEGRAM_API_HASH="your_api_hash"
# один из вариантов хранения сессии:
TELETHON_SESSION_PATH="/abs/path/to/telethon.session"
# или несколько сессий для параллельного Syntax-first:
TELETHON_SESSION_PATHS="/abs/path/to/one.session,/abs/path/to/two.session"
# или:
TELETHON_STRING_SESSION="your_string_session"
# или несколько string-сессий:
TELETHON_STRING_SESSIONS="string_session_one,string_session_two"
```

В этом режиме бот сначала отправляет кадр(ы) и **простой текстовый** video prompt в `@syntxaibot`.
Если prompt длиннее лимита Syntax (максимум 2000 символов), он сначала сокращается через Gemini;
механической обрезки строки перед отправкой нет. Затем бот
периодически читает входящие (интервал `SYNTAX_FALLBACK_POLL_SEC`, по умолчанию 10 с) и ожидает
mp4-документ/видео, прямую ссылку на `.mp4` или страницу `getvideo.syntxai.net` в пределах таймаута.
Если Syntax не вернул видео или сообщает, что Veo находится на обновлении, бот сразу переходит к Vertex.
Для режима 📼 Kling через Syntax webapp нужен browser runtime Playwright поверх Python-пакета:

```bash
.venv/bin/python -m playwright install chromium
```

Проверка цепочки Vertex still → Syntax без Telegram-бота:

```bash
.venv/bin/python tools/syntax_vertex_e2e.py \
  --image-prompt "Описание кадра 9:16" \
  --video-prompt "Короткое описание движения и камеры" \
  --poll-sec 30 \
  --out ./artifacts/syntax_run.mp4
```

Первичная авторизация Telethon (один раз на аккаунт):

```bash
.venv/bin/python tools/telethon_login.py --phone +79990001122 --session-path token/syntax_telethon.session
```

Если нужен `TELETHON_STRING_SESSION` вместо файла:

```bash
.venv/bin/python tools/telethon_login.py --phone +79990001122 --print-string-session
```

Важно:
- `api_id/api_hash` берутся из [my.telegram.org](https://my.telegram.org) (не из BotFather).
- Session-файл/строку храните как секрет и не коммитьте в репозиторий.
- Для параллельного `Syntax-first` дайте несколько Telethon sessions через `TELETHON_SESSION_PATHS` или `TELETHON_STRING_SESSIONS`.
- Если включён `SYNTAX_FALLBACK_ENABLED`, видео сначала пробуется через Syntax; при неуспехе или сообщении про обновление Veo бот переходит к Vertex.

> Gemini text-запросы идут в key-mode; Gemini image-запросы выполняются только через Vertex API transport.

---

## 5) Команды бота

- `/start` — health-check.
- `/check_env` — статус env-настроек (с маскировкой секретов).
- `/generate <текст>` — запуск основного пайплайна. Можно передать ссылку на рецепт: бот попробует извлечь текст и отправит его на согласование.
- `/quick <текст>` — запуск пайплайна как в `/generate`, но с приоритетом `VERTEX_API_KEY` для Gemini.
- `/image <промпт>` — сгенерировать одно изображение.
- `/video <секунды> <промпт>` — сгенерировать одно видео по фото (прикрепите фото в сообщении или ответьте на фото этой командой).
- `/fix_stress` — принудительно привести Veo3-реплики к формату ударений/пауз.
- `/cancel` или `/reset` — сброс сессии.

---

## Логирование

- Консоль: все события уровня `INFO` и выше.
- Файлы логов (по умолчанию в `logs/`):
  - `logs/bot.log` — общий лог (`INFO+`);
  - `logs/errors.log` — только ошибки (`ERROR+`).
- Путь можно переопределить через `BOT_LOG_DIR`.

## 6) Пайплайн генерации

### Текстовая фаза (одинаковый UX, разные промпты)

Порядок задан в `constants.TEXT_PIPELINE_STEPS`: **концепт → рецепт → сценарий**.  
На каждом шаге: промпт в LLM → ответ пользователю → **«ОК»** (следующий шаг) или **правка** (в модель уходят предыдущий вариант и фидбек). Промпты собираются в `app/pipeline_llm_steps.py`.

1. **Концепты** — `/generate ...` или выбор после старта; варианты блюд.
2. **Рецепт** — один **связный текст** (ингредиенты + приготовление), без нумерованных «ШАГ 1…N» в выводе модели; апрув и правки через тот же цикл.
3. **Сценарий** — JSON **4–6 сцен** по полному тексту рецепта; правки с `previous_scenes` + feedback в промпте.

### После утверждённого сценария

Опорные состояния см. `POST_SCENARIO_PIPELINE_WAITING_STATES` в `constants.py`.

4. **Production (кадры + video сразу)** — после «ОК» по сценарию вызывается **`generate_full_production(..., approved_frame_prompts=None)`** (`pipeline.py`): один запрос к LLM, в **`session["production"]["json_plan"]`** по сценам сразу **`start_frame_prompt`**, **`end_frame_prompt`**, **`video_prompt`**. Для последующих правок только video в сессию кладётся срез кадров (`frame_prompts` из production) — см. **`_frame_prompts_slice_from_production`** в `handlers_state_scene.py`. Отдельного шага «только frame prompts на апрув» нет.

5. **Медиа по сценам** — не новый LLM-этап с нуля, а **интерактивный проход** по уже заполненному `json_plan` из п.4.

   **Где в коде:** после «ОК» по production — `state == waiting_scene_media` (`constants.STATE_WAITING_SCENE_MEDIA`), обработчик **`app/handlers/handlers_state_media.py`** → **`_handle_waiting_scene_media_state`**. Текущая сцена — `session["production"]["json_plan"][session["image_scene_index"]]`. Подшаг — **`session["scene_media_substep"]`** (`start_prompt` / `end_prompt` / `video_prompt`, см. `SCENE_MEDIA_SUBSTEP_*` в `handlers_media_core.py`).

Состояния **`waiting_frame_prompts_approval`** / **`refining_frame_prompts`** оставлены для совместимости со старыми сессиями; новый поток их не использует.

   **Цепочка подшагов:**
   1. **Старт** — показывается и правится **`start_frame_prompt`** (или перегенерация через LLM по «ФИДБЕК»); по «ОК» собирается промпт для картинки (стиль + референсы персонажей и `continuity_anchors`; location-ассеты не отправляются), вызывается Vertex, в сцену пишется **`start_frame_image_b64`**.
   2. **Конец** — только если для сцены нужен отдельный конечный кадр (`_scene_needs_end_frame_step`); иначе подшаг конца пропускается, сразу **video**. Для конца используется **`end_frame_prompt`** и часто стартовое изображение как референс; результат — **`end_frame_image_b64`**.
   3. **Видео** — показывается **`video_prompt`**; по «ОК» **`_scene_media_video_ok`** сначала проверяет длину реплики **`text`**: speech budget (`duration_sec × 14` для RU, но до 125 символов для 8-секундной RU-сцены) **без** подстрок `[пауза]` (`scene_speech_budget.py`). При превышении вызывается **`_maybe_shorten_scene_speech_for_video_budget`**: в Gemini уходит JSON с текущим `text`, `video_prompt` и текстом замечания про лимит; ответ обновляет сцену, затем снова проверка лимита и **`enforce_video_prompt_voice_requirements`**. После этого **`_generate_video_bytes_with_high_load_retry`** (`app/video_generation.py` / Vertex) получает актуальный `video_prompt` с **`start_image`** = декод `start_frame_image_b64` и **`end_image`** = декод `end_frame_image_b64`, если он непустой (если конца нет — в видео уходит только старт).

   **Назад на шаг:** «старт заново» / «конец заново», а также `ПРОМПТ:` / `ФИДБЕК:` — см. сообщения бота и функции **`_handle_scene_media_common_restarts`**, **`_scene_media_start_not_ok`**, **`_scene_media_video_not_ok`** в том же модуле.

### Ошибки видео

- `invalid image/reference image` — предложение переписать image prompt и перегенерация кадра;
- `policy/safety` — переписывание video prompt через LLM и повтор апрува.

Целевая структура полей сцены на медиа-этапе описана в `app/pipeline_flow.ScenePerStepMedia` (в т.ч. путь к явным спискам референсов в БД — на будущее).

---

## 7) Важные правила сценария (в промптах к модели)

Большинство ограничений задаётся в промпте сценария (`pipeline_llm_steps`, блок `requirements`), а не пост-обработкой в коде:

- 4–6 сцен по порядку рецепта; бабушка и кот; еда и объяснение шага в кадре.
- Смена ракурса или предметного фокуса между соседними сценами; без ракурса снизу.
- Консистентность через `continuity_anchors`: повторяемая посуда, кастрюля/сковорода, плита/рабочая поверхность, ингредиенты и состояние еды.
- Плита/сковорода: конфорка как предметная опора; нарезка: стиль нарезки; финал: подача блюда.
- Реплика `text`: первое лицо (я/мы), формат Veo3 при `veo3*` (`[пауза]`, ударения заглавной буквой в гласной и т.д.).
- Длина речи в `text`: не больше speech budget (`duration_sec × 14` для RU, до 125 символов для 8-секундной RU-сцены) **без** всех `[пауза]`; в промпте плана сцен это же правило (`pipeline_llm_steps.py`). На шаге «ОК» видео при нарушении лимита срабатывает автосокращение через Gemini (`scene_speech_shorten.py`), а не только ручная правка.

Для отладки человекочитаемых кодов ошибок по сценам остаются хелперы в `scene_utils` / `_humanize_scene_errors` в `pipeline.py` (без автоматического цикла исправления сценария).

---

## 8) Ретраи и отказоустойчивость

### Gemini

- Глобальная очередь вызовов (без параллельных гонок).
- `503/UNAVAILABLE` в хендлерах: до 20 повторов, шаг 15 секунд.
- `429`: backoff в `llm_utils.py`.
- `quota/resource_exhausted`: fallback-модели и корректное сообщение пользователю.
- Все запросы Gemini выполняются через Vertex SDK transport.

### Image generation (Vertex)

- Основной путь: Vertex SDK.
- Fallback: REST для imagen-моделей.
- Автоперебор image-моделей.
- Промпт-фолбэк: смягчение текста при ошибке генерации.

### Video generation

- Прямой вызов Vertex `predictLongRunning` API (`tools/gen_video.py`, функция `generate_video_via_vertex`).
- Для каждой сцены генерируется отдельный `mp4` после явного апрува.
- Ошибки policy/invalid-image обрабатываются интерактивно (перегенерация промптов и повтор).

---

## 9) Диагностика частых ошибок

### `telegram.error.BadRequest: Message is too long`

Исправлено чанкингом длинных сообщений в `handlers.py`.

### `403 PERMISSION_DENIED` в Vertex

Проверьте:

- включен `generativelanguage.googleapis.com` и/или `aiplatform.googleapis.com` в нужном проекте;
- ключ принадлежит правильному проекту;
- key restrictions не блокируют API;
- активен billing.

### `404 NOT_FOUND model ...`

Модель недоступна в текущем проекте/версии API.  
Проверьте, что `VERTEX_IMAGE_MODEL="gemini-3-pro-image"`, модель доступна в проекте и используется поддерживаемый регион. Если модель временно недоступна, бот предложит повторить сильную модель или вручную понизить до следующей из `VERTEX_IMAGE_MODEL_FALLBACKS`.

### `SSL CERTIFICATE_VERIFY_FAILED`

Установите `certifi` и проверьте системные сертификаты Python.

---

## 10) Как посмотреть доступные модели

```bash
python3 list_models.py
```

Для production-рекомендаций используйте Vertex AI (project/location в `console.cloud.google.com`).

## 11) PostgreSQL, миграции и seed

После настройки `DATABASE_URL` схема должна применяться через Alembic:

```bash
poetry run alembic upgrade head
poetry run python scripts/seed_default_project.py
```

Временный dev-вариант `create_tables()` ещё есть в `app.db`, но рабочий путь развития схемы — миграции.
При старте бот выполняет DB health-check, если `DATABASE_URL` задан.

---

## 12) Ассеты и структура

Ожидаемая структура:

- `assets/characters/<name>/base.png`
- `assets/locations/<name>/base.png`
- опционально `assets/locations/<name>/top_view.png`

Скрипт:

```bash
python3 organize_assets.py --source ./images --assets-dir ./assets
```

Он раскладывает файлы, создает структуру и обновляет `config.py`.

---

## 13) Замечания по безопасности

- Не коммитьте `.env` с реальными ключами.
- Используйте только `.env.example` в репозитории.
- В `/check_env` и логах ключи маскируются, но все равно избегайте публикации логов с чувствительными данными.

---

## 14) Куда расширять дальше

- Подстановка шагов пайплайна из БД вместо константы `TEXT_PIPELINE_STEPS` (сохраняя `step_id` и привязку к `STATE_*`).
- `/check_vertex` и `/check_vertex_image` тест-команды диагностики.
- Сохранение сгенерированных изображений на диск (`generated/...`).
- Явные списки референсов в данных сцены / `ScenePerStepMedia`; location-ассеты хранятся в проекте, но не отправляются в генерацию.
- Персистентное хранение сессий и кэша (Redis/DB) вместо in-memory.
