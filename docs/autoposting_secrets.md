# Autoposting Secrets

Ниже практичная схема, куда складывать секреты для автопостинга и откуда их получать.

## Куда что класть

В `.env` Python-приложения:
- `N8N_PUBLISH_WEBHOOK_URL`
- `N8N_PUBLISH_WEBHOOK_TOKEN`
- `CONTENT_FABRIC_PUBLIC_BASE_URL`
- при необходимости platform IDs без чувствительных токенов: `YOUTUBE_CHANNEL_ID_RU`, `YOUTUBE_CHANNEL_ID_EN`, `VK_GROUP_ID_RU`, `RUTUBE_CHANNEL_ID_RU`

В `n8n Credentials` или внешнем secret manager:
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`
- `INSTAGRAM_APP_ID`
- `INSTAGRAM_APP_SECRET`
- `INSTAGRAM_ACCESS_TOKEN`
- `TIKTOK_CLIENT_KEY`
- `TIKTOK_CLIENT_SECRET`
- `TIKTOK_ACCESS_TOKEN`
- `VK_CLIENT_ID`
- `VK_CLIENT_SECRET`
- `VK_ACCESS_TOKEN`
- любые cookie/session/manual credentials для площадок, где нет надежного публичного publish API

Почему так:
- `n8n` будет публиковать в площадки и именно ему нужны OAuth access/refresh tokens.
- Python-приложению лучше не хранить лишние social-токены, если оно только собирает payload и дергает publish-webhook.

## YouTube

Нужно:
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_CHANNEL_ID_RU`
- `YOUTUBE_CHANNEL_ID_EN`

Откуда брать:
- создать проект и OAuth credentials в Google Cloud / Google API Console;
- включить YouTube Data API v3;
- пройти OAuth consent и получить refresh token для нужного канала;
- channel id взять из YouTube Studio или через API.

Важно:
- для загрузки видео нужен OAuth 2.0; service account для YouTube upload не подходит.
- новые непроверенные API projects могут загружать видео только как private до аудита проекта.

## Instagram

Нужно:
- `INSTAGRAM_APP_ID`
- `INSTAGRAM_APP_SECRET`
- `INSTAGRAM_ACCESS_TOKEN`
- `INSTAGRAM_BUSINESS_ACCOUNT_ID_RU`
- `INSTAGRAM_BUSINESS_ACCOUNT_ID_EN`
- `INSTAGRAM_PAGE_ID_RU`
- `INSTAGRAM_PAGE_ID_EN`

Откуда брать:
- создать приложение в Meta for Developers;
- подключить Instagram Graph API;
- перевести аккаунты в professional/business/creator, если это требуется вашим publish-flow;
- связать Instagram-аккаунты с Facebook Pages;
- пройти OAuth и получить long-lived token;
- ids аккаунтов и страниц взять из Graph API/Meta tools.

Практическая оговорка:
- Instagram publish лучше сразу вести через `n8n` credentials и workflow approval.
- перед внедрением стоит отдельно проверить, что ваш тип аккаунта и нужный формат публикации действительно доступны через текущий Graph API flow.

## TikTok

Нужно:
- `TIKTOK_CLIENT_KEY`
- `TIKTOK_CLIENT_SECRET`
- `TIKTOK_ACCESS_TOKEN`
- `TIKTOK_OPEN_ID`

Откуда брать:
- зарегистрировать app в TikTok for Developers;
- добавить Content Posting API product;
- получить одобрение на нужные scopes;
- авторизовать целевой TikTok account и получить `access_token` + `open_id`.

Важно:
- direct post и upload завязаны на product approval и scopes.
- для части flow TikTok ограничивает visibility или требует аудит клиента.

## VK

Нужно:
- `VK_CLIENT_ID`
- `VK_CLIENT_SECRET`
- `VK_ACCESS_TOKEN`
- `VK_GROUP_ID_RU`

Откуда брать:
- создать приложение VK;
- пройти OAuth;
- получить access token с правами, достаточными для загрузки/публикации видео в нужное сообщество или аккаунт;
- `group_id` взять из настроек сообщества или через API.

Практически:
- токен и публикацию удобнее держать в `n8n`, а Python пусть передает только title/description/video reference.

## RuTube

Сейчас рекомендую считать RuTube полуавтоматическим контуром, даже если title/description уже генерируются автоматически.

Хранить:
- `RUTUBE_CHANNEL_ID_RU`
- любые токены/сессионные данные только в `n8n credentials` или secret manager

Откуда брать:
- channel id и параметры канала из Studio/кабинета RuTube;
- если появится подтвержденный стабильный upload API именно под ваш тип аккаунта, токен брать по его официальной схеме.

Практическая рекомендация:
- пока не опираться на “серые” браузерные обходы как на основной production path.
- сначала использовать автоматическую генерацию `RuTube` title/description и manual approval/upload.

## Что должно жить только в секретах

Никогда не коммитить:
- OAuth client secrets
- refresh/access tokens
- cookies
- session files
- webhook signing secrets

Можно хранить в обычном config:
- channel ids
- page ids
- group ids
- public callback URLs
