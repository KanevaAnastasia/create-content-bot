# Секреты прямой публикации

Видео отправляет само Python-приложение через официальные API. Все ключи аккаунтов обязательны в
пространстве конкретного проекта:

- `PUBLISH_GRANDMA_KITCHEN_<KEY>` — только «Бабушкина кухня»;
- `PUBLISH_DISCIPLINE_<KEY>` — только «Дисциплина».

Глобальные `YOUTUBE_*`, `INSTAGRAM_*`, `TIKTOK_*` и `VK_*` намеренно не читаются: это защита от
публикации не в тот аккаунт.

## YouTube

- `YOUTUBE_CLIENT_ID_RU`, `YOUTUBE_CLIENT_SECRET_RU`, `YOUTUBE_REFRESH_TOKEN_RU`
- `YOUTUBE_CLIENT_ID_EN`, `YOUTUBE_CLIENT_SECRET_EN`, `YOUTUBE_REFRESH_TOKEN_EN` for a separate English channel
- `YOUTUBE_CHANNEL_ID_RU` и при необходимости `YOUTUBE_CHANNEL_ID_EN`

Нужен OAuth-клиент Google с YouTube Data API v3 и scope `youtube.upload`. Перед загрузкой код получает
канал текущего OAuth-пользователя и сравнивает его с настроенным channel ID. Подключение выполняется
кнопкой **Connect YouTube publishing API** в списке публикаций. RU и EN хранят отдельные refresh token,
поэтому каналы могут принадлежать разным Google-пользователям. Старые ключи без языкового суффикса
поддерживаются как резервная совместимость.

## Instagram

- `INSTAGRAM_ACCESS_TOKEN`
- `INSTAGRAM_APP_ID`
- `INSTAGRAM_APP_SECRET`
- `INSTAGRAM_BUSINESS_ACCOUNT_ID_RU` и при необходимости `..._EN`

Нужен professional-аккаунт и токен Meta с правами публикации. Видео Reels загружается напрямую с
локального диска через официальный resumable upload протокол Meta (`rupload.facebook.com`), поэтому
публичный HTTPS URL для MP4 не требуется.

Команда `/stat` дополнительно читает ролики всех подключённых RU/EN-аккаунтов активных проектов
и отбирает видео с точным упоминанием `@syntx_ai` в описании. Начало периода — последнее
наступившее 10-е число по Москве; кнопка «Другая дата» открывает календарь. В итог входят
публикации с выбранного дня включительно до момента запроса, просмотры — текущая метрика
`views` за всё время жизни этих роликов, а не прирост просмотров за период.

Для чтения просмотров токену и приложению Meta требуется доступ `instagram_manage_insights`
в дополнение к правам чтения аккаунта. Прав публикации недостаточно: при отказе API бот
покажет ссылки и число роликов, но отметит просмотры как недоступные, без подстановки нулей.
После выдачи разрешения нужно сохранить обновлённый токен в том же проектном подключении.
Описание API: https://developers.facebook.com/docs/instagram-platform/insights/.

## TikTok

- `TIKTOK_ACCESS_TOKEN`
- `TIKTOK_REFRESH_TOKEN`
- `TIKTOK_CLIENT_KEY`
- `TIKTOK_CLIENT_SECRET`
- `TIKTOK_REDIRECT_URI`
- `TIKTOK_OPEN_ID`

Приложению TikTok нужен Content Posting API и scope `video.publish`. По умолчанию используется
`SELF_ONLY`, а ролик маркируется как AI-generated. Эти общие параметры можно изменить через
`TIKTOK_PRIVACY_LEVEL` и `TIKTOK_IS_AIGC`. Кнопка **Connect TikTok publishing API** использует
официальный Desktop OAuth + PKCE и локальный callback `http://localhost:8000/...`; access token
обновляется и ротированный refresh token сохраняется автоматически. До прохождения аудита TikTok
прямые публикации доступны только с приватностью `SELF_ONLY`.

## VK

- `VK_ACCESS_TOKEN`
- `VK_CLIENT_ID`
- `VK_REFRESH_TOKEN`
- `VK_DEVICE_ID`
- `VK_REDIRECT_URI`
- `VK_USER_ID` (предпочтительно)
- `VK_USER_SCREEN_NAME` (резервная проверка для старой конфигурации)
- `VK_GROUP_ID_RU`

`video.save` принимает пользовательский токен, а `VK_GROUP_ID_RU` задаёт сообщество,
куда сохраняется и публикуется видео. Перед каждой загрузкой приложение сверяет неизменяемый числовой
ID владельца токена и точный ID настроенного сообщества; фактические права на загрузку проверяет VK. Токену нужны права `video`,
`groups`, `wall` и серверный доступ. Для новых приложений VK ID выдаёт access token на один час вместе
с refresh token; перед включением фоновой публикации оба должны быть настроены и проверены.

Для VK ID OAuth 2.1 откройте в списке публикаций админки ссылку **Connect VK publishing API**.
Создайте отдельное приложение в кабинете подключения VK ID и выберите платформу **Web**. ID
VK Mini App, защищённый ключ и сервисный ключ для этого потока не подходят и не используются.
Добавьте показанный callback URL в доверенные адреса Web-приложения VK ID, затем подтвердите права
`video`, `groups`, `wall`. Админка сохранит access/refresh token и device ID локально; публикационный
воркер обновляет короткоживущий access token и атомарно сохраняет ротированный refresh token.

Для локальной авторизации VK ID поддерживает только базовый домен `localhost` и redirect URL
`http://localhost/...` без порта. В проекте правило PF из `ops/pf/` безопасно перенаправляет только
локальный `127.0.0.1:80` на админку `127.0.0.1:8000`. Для публичного окружения используйте HTTPS.

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
