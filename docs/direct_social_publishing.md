# Прямая публикация видео

## Админка

Откройте `Publication jobs` и нажмите `Prepare video publication`, либо перейдите по кнопке публикации
из конкретной генерации.

1. Выберите генерацию, которая даст исходный контекст для описаний.
2. Обязательно выберите проект: `grandma_kitchen` или `discipline`.
3. Для RU и EN выберите любую историческую финальную сборку либо `Upload my own MP4`.
4. Отметьте площадки. Текст каждой площадки расположен прямо под её чекбоксом и доступен для правки.
5. При необходимости задайте время. Без времени готовые задания сразу уходят в Celery worker.

RuTube из проекта исключён. Поддерживаются YouTube RU/EN, Instagram RU/EN, TikTok RU и VK RU.

## Исполнение

- YouTube: отдельный OAuth refresh для RU/EN, проверка ожидаемого channel ID, resumable upload.
- Instagram: Reels container по подписанному HTTPS URL, ожидание обработки, `media_publish`.
- TikTok: автоматический OAuth refresh, creator info, Direct Post init и chunked `FILE_UPLOAD`.
- VK: `video.save` и загрузка MP4 на выданный URL.

Celery Beat раз в минуту подбирает запланированные задания. Состояние, внешний ID и ошибка сохраняются
в `publication_jobs`. Повторную отправку можно запустить admin action `Publish selected jobs now through
official APIs`.

## Настройка

Заполните проектные ключи по [autoposting_secrets.md](/opt/content_fabric/docs/autoposting_secrets.md),
затем обновите привязки аккаунтов:

```bash
.venv/bin/python scripts/setup_autoposting.py
scripts/botctl.sh celery-worker
scripts/botctl.sh celery-beat
```

Статус `Direct API` в `Project publish accounts` должен стать `ready`. Если конфигурации не хватает,
задача остаётся `needs_manual_action` и показывает точные отсутствующие ключи.
