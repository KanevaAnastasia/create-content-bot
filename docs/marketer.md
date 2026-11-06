# Маркетолог

Админка → Маркетолог (`/admin/marketer/`, учитывает `ADMIN_URL_PATH`).
GET требует staff + view_publicationjob; POST дополнительно change_publicationjob и CSRF.
Фоновые задачи Celery `pipeline_admin.marketer_report` не вызывают publish API.
Отдельная очередь `marketer`; после развёртывания нужен `scripts/botctl.sh celery-worker-reload`.

1. Выбрать проект и обновить статистику. Последние 30 published jobs, не весь архив аккаунта.
2. Исследовать нишу: явный запрос, язык-предпочтение, YouTube short (<4 мин), 90 дней, максимум 10 результатов.
3. По желанию составить AI-план через существующий Gemini/Vertex provider: отдельная кнопка,
   расходует квоту/бюджет, передаёт только allowlist метрик/заголовков/источников, без токенов.

Instagram: like_count/comments_count и отдельные insights views/reach/saved/shares.
YouTube Data API: viewCount/likeCount/commentCount. Нет удержания/CTR без YouTube Analytics.
VK/TikTok пока явно недоступны. API errors и missing не превращаются в нули.
Недоступный/удалённый ролик сохраняется как строка с предупреждением.

Сравнения: проект + платформа + язык + account_key + возрастная корзина;
минимум 3 других ролика. Это эвристика, не причинный анализ. Разные платформы не суммируются.
AI не смотрит видео; адаптации по названиям помечены как гипотезы, внешние строки недоверенные.

Срезы и состояние очереди: runtime/marketer/<project UUID>.json, атомарная запись, 0600.
Файлы не обслуживаются публично. Project-scoped flock, task nonce, 60-секундный cooldown,
20-минутное обнаружение зависшей задачи. Сохраняется один предыдущий срез.
При ошибке задачи прошлый отчёт остаётся; перезапуск — кнопкой. Данные не пишутся в publication jobs.
После обновления данных старый AI-план помечается устаревшим.

Документация: https://developers.google.com/youtube/v3/docs/videos/list
и https://developers.google.com/youtube/v3/docs/search/list ;
Meta Instagram API: https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api
# Grouped cards and history

## Audience and retention

Refresh collects account-level YouTube/Instagram follower snapshots for accounts
represented in the report. YouTube counts can be rounded or hidden. Deltas are
only computed for the same account ID and are not attributed to individual posts.
The separate behaviour action reads YouTube Analytics for up to the first 30
report publications over the last 90 days. It needs a separately granted Analytics
scope; this change does not alter OAuth scopes or reconnect accounts.
It collects attributed subscriptions, mean viewing duration/percentage and
audienceWatchRatio by elapsedVideoTimeRatio. Largest adjacent drops have video
timestamp links, not causal labels. Ratios can exceed 100% due to rewatches.
There is no second-by-second like/swipe telemetry or automatic video understanding.
Failures retain older behavioural data with an explicit warning and timestamp.
AI text uses an escaped Markdown subset (headings, bold, lists); arbitrary HTML
and executable links are never rendered.

Cards group only by project and publication bundle, never by title. Expand a card
to inspect individual language/platform metrics, links and advice. YouTube-backed
cards use its public thumbnail. Missing metrics remain distinct from measured zero.

The history button reads each configured RU/EN YouTube channel's uploads playlist:
at most 50 latest uploads per language, filtered to 90 days, including non-Shorts.
Imported records live only in the project report cache and cannot be published.
Existing jobs take precedence when platform/language/video ID match. Unknown
cross-platform matches remain separate. Failed imports preserve the prior data
and display a warning. Other platforms' history and manual merging are not yet
implemented. Existing Instagram insight permissions and VK/TikTok limitations
remain unchanged.
