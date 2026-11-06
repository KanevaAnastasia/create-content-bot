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
