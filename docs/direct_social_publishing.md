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
- Instagram: resumable-загрузка локального MP4 в Reels container, ожидание `FINISHED`, затем `media_publish`. ID контейнера, аккаунт, отпечаток видео/описания и подтверждённый media ID сохраняются в `payload_json.instagram_upload` на каждом этапе. Повтор задания использует прежний контейнер; новый создаётся только после подтверждённого `ERROR`/`EXPIRED` без неопределённого результата публикации. Ошибка Meta `2207085` повторяется не более трёх раз за запуск на том же готовом контейнере. Потерянный ответ публикации требует сверки аккаунта (`needs_manual_action`), а не создания дубля. Описания длиннее 2200 символов отклоняются, а не обрезаются молча.
  Перед загрузкой проверяются действительность токена, `instagram_content_publish` и целевой аккаунт. Для Page token поле `/me.instagram_business_account.id` должно совпасть с `INSTAGRAM_BUSINESS_ACCOUNT_ID_<LANG>`; доступ к чтению чужого Instagram сам по себе недостаточен. Для user token проверяются ограничения `granular_scopes`, если они заданы. Токен «Бабушки» нельзя подставлять в «Дисциплину» только потому, что запрос профиля проходит.
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

## Все локальные сервисы

Админка, Telegram-бот, основной Celery worker, отдельный worker статистики и Celery beat установлены
как пользовательские macOS LaunchAgents с `RunAtLoad` и `KeepAlive`. Redis управляется Homebrew
LaunchAgent. Поэтому после входа
пользователя в macOS весь контур поднимается автоматически, в том числе после перезагрузки компьютера.

Одна команда устанавливает недостающие LaunchAgents, синхронизирует их с версиями из репозитория и
поднимает остановленные процессы:

```bash
scripts/botctl.sh services-up
```

Для принудительного перезапуска всего контура и общей диагностики используйте:

```bash
scripts/botctl.sh services-restart
scripts/botctl.sh services-status
```

`services-status` проверяет состояние всех шести процессов, доступность Redis, размеры очередей Celery
и HTTP-доступность админки. Минутные служебные задания имеют срок годности меньше интервала запуска,
поэтому при остановленном worker они больше не образуют многотысячную очередь.

Статистика обслуживается отдельным worker очереди `marketer`: долгие загрузки видео больше не могут
занять все потоки и задержать ежедневный отчёт на 09:00 МСК.
