# Секреты прямой публикации

Видео отправляет само Python-приложение через официальные API. Все ключи аккаунтов обязательны в
пространстве конкретного проекта:

- `PUBLISH_GRANDMA_KITCHEN_<KEY>` — только «Бабушкина кухня»;
- `PUBLISH_DISCIPLINE_<KEY>` — только «Дисциплина».

Глобальные `YOUTUBE_*`, `INSTAGRAM_*`, `TIKTOK_*` и `VK_*` намеренно не читаются: это защита от
публикации не в тот аккаунт.

## YouTube

- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_CHANNEL_ID_RU` и при необходимости `YOUTUBE_CHANNEL_ID_EN`

Нужен OAuth-клиент Google с YouTube Data API v3 и scope `youtube.upload`. Перед загрузкой код получает
канал текущего OAuth-пользователя и сравнивает его с настроенным channel ID.

## Instagram

- `INSTAGRAM_ACCESS_TOKEN`
- `INSTAGRAM_APP_ID`
- `INSTAGRAM_APP_SECRET`
- `INSTAGRAM_BUSINESS_ACCOUNT_ID_RU` и при необходимости `..._EN`

Нужен professional-аккаунт и токен Meta с правами публикации. Для загрузки Reels Meta должна получить
MP4 по публичному HTTPS URL, поэтому также обязательны общие параметры:

- `CONTENT_FABRIC_PUBLIC_BASE_URL`
- `PUBLICATION_MEDIA_SIGNING_KEY`

## TikTok

- `TIKTOK_ACCESS_TOKEN`
- `TIKTOK_OPEN_ID`

Приложению TikTok нужен Content Posting API и scope `video.publish`. По умолчанию используется
`SELF_ONLY`, а ролик маркируется как AI-generated. Эти общие параметры можно изменить через
`TIKTOK_PRIVACY_LEVEL` и `TIKTOK_IS_AIGC`.

## VK

- `VK_ACCESS_TOKEN`
- `VK_CLIENT_ID`
- `VK_REFRESH_TOKEN`
- `VK_DEVICE_ID`
- `VK_REDIRECT_URI`
- `VK_USER_SCREEN_NAME`
- `VK_GROUP_ID_RU`

`video.save` принимает пользовательский токен администратора, а `VK_GROUP_ID_RU` задаёт сообщество,
куда сохраняется и публикуется видео. Перед каждой загрузкой приложение сверяет короткое имя владельца
токена и проверяет, что он администрирует именно указанное сообщество. Токену нужны права `video`,
`groups`, `wall` и серверный доступ. Для новых приложений VK ID выдаёт access token на один час вместе
с refresh token; перед включением фоновой публикации оба должны быть настроены и проверены.

Для VK ID OAuth 2.1 откройте в списке публикаций админки ссылку **Connect VK publishing API**.
Создайте отдельное приложение в кабинете подключения VK ID и выберите платформу **Web**. ID
VK Mini App, защищённый ключ и сервисный ключ для этого потока не подходят и не используются.
Добавьте показанный callback URL в доверенные адреса Web-приложения VK ID, затем подтвердите права
`video`, `groups`, `wall`. Админка сохранит access/refresh token и device ID локально; публикационный
воркер обновляет короткоживущий access token и атомарно сохраняет ротированный refresh token.

## Безопасное сохранение

Не передавайте секреты в аргументах команд и не коммитьте `.env`. Используйте скрытый ввод:

```bash
.venv/bin/python scripts/set_publish_secret.py grandma_kitchen YOUTUBE_REFRESH_TOKEN
.venv/bin/python scripts/set_publish_secret.py discipline YOUTUBE_REFRESH_TOKEN
.venv/bin/python scripts/set_publish_secret.py shared PUBLICATION_MEDIA_SIGNING_KEY --generate
```

Если токен уже скопирован в буфер обмена macOS, его можно сохранить без отображения в истории:

```bash
pbpaste | .venv/bin/python scripts/set_publish_secret.py grandma_kitchen INSTAGRAM_ACCESS_TOKEN --stdin
```

Скрипт атомарно обновляет `.env`, выставляет режим `0600` и не печатает значение. Публичные channel,
account и group IDs всё равно хранятся раздельно по проектам.

Короткоживущий токен Meta можно обменять и сразу заменить в `.env`, не печатая ни один секрет:

```bash
.venv/bin/python scripts/refresh_meta_token.py grandma_kitchen
```
