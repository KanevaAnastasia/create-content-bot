# n8n Autoposting Setup

Эта инструкция рассчитана на текущий проект и новый publish-layer в БД.

Что уже делает код:
- после апрува social descriptions в Telegram создает `publication_jobs` в БД;
- сохраняет publish-ready mp4 в `artifacts/publications/<bundle_key>/`;
- раскладывает RU видео на `youtube/rutube/instagram/vk/tiktok`;
- раскладывает EN видео на `instagram/youtube`.

Что делаете вы:
1. вставляете секреты в `.env` проекта и `ops/n8n/.env`;
2. запускаете setup-скрипт;
3. поднимаете `n8n`;
4. в `n8n` создаете один workflow по инструкции ниже.

## 1. Секреты проекта

Заполните основной `.env`:
- `DATABASE_URL`
- `PUBLICATION_ARTIFACTS_DIR=/private/tmp/content_fabric_publications`
- `YOUTUBE_CHANNEL_ID_RU`
- `YOUTUBE_CHANNEL_ID_EN`
- `INSTAGRAM_BUSINESS_ACCOUNT_ID_RU`
- `INSTAGRAM_BUSINESS_ACCOUNT_ID_EN`
- `TIKTOK_OPEN_ID`
- `VK_GROUP_ID_RU`
- `RUTUBE_CHANNEL_ID_RU`

OAuth и app secrets храните по инструкции в [docs/autoposting_secrets.md](/opt/content_fabric/docs/autoposting_secrets.md).

## 2. Подготовка схемы

Запустите:

```bash
cd /opt/content_fabric
poetry run python scripts/setup_autoposting.py
```

Скрипт:
- создаст недостающие таблицы через SQLAlchemy metadata;
- подтянет account mapping из env в `project_publish_accounts`.

## 3. Установка и запуск n8n

Скопируйте env:

```bash
cd /opt/content_fabric/ops/n8n
cp .env.example .env
```

Заполните минимум:
- `N8N_ENCRYPTION_KEY`
- `N8N_BASIC_AUTH_PASSWORD`
- `CONTENT_FABRIC_PUBLICATION_DIR=/private/tmp/content_fabric_publications`
- при необходимости `N8N_EDITOR_BASE_URL` и `WEBHOOK_URL`

Запуск:

```bash
cd /opt/content_fabric/ops/n8n
docker compose up -d
```

Открыть UI:
- [http://localhost:5678](http://localhost:5678)

## 4. Что появится в БД

После того как в Telegram вы нажмете `ОК` на финальных описаниях:
- создадутся строки в `publication_jobs`;
- `status=approved` получат площадки, где есть account id из env;
- `status=needs_manual_action` получат площадки без account mapping или без готовой auto-ветки;
- `video_path` будет указывать на локальный mp4 для публикации.

## 5. Рекомендуемый workflow в n8n

Создайте workflow со следующими узлами:

1. `Schedule Trigger`
- каждые 5 минут

2. `Postgres`
- query:

```sql
SELECT
  id,
  bundle_key,
  platform,
  language,
  account_key,
  external_account_id,
  status,
  video_path,
  video_label,
  title,
  description,
  payload_json
FROM publication_jobs
WHERE status IN ('approved', 'needs_manual_action')
ORDER BY created_at ASC
LIMIT 20;
```

3. `Split In Batches`
- batch size `1`

4. `Switch`
- field: `{{$json.platform}}`
- cases:
  - `youtube`
  - `instagram`
  - `tiktok`
  - `vk`
  - `rutube`

## 6. Ветки по платформам

### YouTube

Рекомендуемый режим:
- автоматическая ветка через YouTube Data API

Перед публикацией:
1. `Read Binary File`
   - file path: `{{$json.video_path}}`
2. `HTTP Request` или YouTube node
   - используйте OAuth credentials YouTube
   - title: `{{$json.title}}`
   - description: `{{$json.description}}`

После успеха:
3. `Postgres`

```sql
UPDATE publication_jobs
SET
  status = 'published',
  published_at = NOW(),
  external_post_id = {{$json.videoId ? "'" + $json.videoId + "'" : "NULL"}},
  publish_error = NULL,
  attempt_count = attempt_count + 1
WHERE id = '{{$node["Postgres"].json["id"]}}';
```

Если ошибка:
4. `Postgres`

```sql
UPDATE publication_jobs
SET
  status = 'failed',
  publish_error = {{$json.error ? "'" + $json.error.message + "'" : "'unknown error'"}},
  attempt_count = attempt_count + 1
WHERE id = '{{$node["Postgres"].json["id"]}}';
```

### Instagram / TikTok / VK / RuTube

Для этих платформ в этом репозитории лучше держать гибридный режим:
- если у вас уже есть подтвержденная рабочая official API ветка в `n8n`, публикуйте автоматически;
- если нет, делайте operator-ready ветку.

Operator-ready ветка:
1. `Read Binary File` из `{{$json.video_path}}`
2. `Telegram` или `Email` node
3. отправить:
   - platform
   - language
   - title
   - description
   - путь к видео или сам binary file
4. `Postgres`:

```sql
UPDATE publication_jobs
SET
  status = 'scheduled',
  publish_error = 'Sent to manual/operator branch'
WHERE id = '{{$node["Postgres"].json["id"]}}';
```

Это дает рабочий production flow уже сейчас, без серых обходов и без потери контента.

## 7. Минимально рабочий режим "вставила секреты и заработало"

Самый реалистичный вариант сегодня:
- `YouTube RU/EN` вести в авто-ветку;
- `Instagram RU/EN`, `TikTok RU`, `VK RU`, `RuTube RU` вести в operator-ready ветку;
- когда у вас будут подтвержденные app approvals и рабочие OAuth flows под конкретные аккаунты, просто замените manual branch на auto branch внутри того же workflow.

## 8. Проверка

После первого апрува описаний в Telegram проверьте:

```sql
SELECT platform, language, status, video_path
FROM publication_jobs
ORDER BY created_at DESC;
```

Ожидаемо:
- `youtube ru`, `youtube en` появятся сразу;
- RU-платформы будут ссылаться на один и тот же RU mp4;
- EN-платформы будут ссылаться на один и тот же EN mp4.
