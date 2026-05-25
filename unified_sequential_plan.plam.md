---
name: Unified plan by sprints
overview: "Единый roadmap мультипроектности и post-pipeline с последовательными спринтами, измеримыми целями и готовыми к запуску todo-пунктами."
todos:
  - id: sprint-1-baseline-audit
    content: "Спринт 1: baseline критичных флоу, фиксация in-memory точек и DoD."
    status: pending
  - id: sprint-2-text-engine-refactor
    content: "Спринт 2: декларативный реестр шагов и единый text step-engine без смены UX."
    status: pending
  - id: sprint-3-postgres-foundation
    content: "Спринт 3: SQLAlchemy/Alembic/PostgreSQL фундамент, db-session слой и health-check."
    status: pending
  - id: sprint-4-multiproject-schema-seed
    content: "Спринт 4: schema v1 (projects, entities, text steps/revisions) и seed default project."
    status: pending
  - id: sprint-5-active-project-db-steps
    content: "Спринт 5: active_project_id, команды выбора проекта и DB-driven text pipeline."
    status: pending
  - id: sprint-6-runs-scenes-artifacts
    content: "Спринт 6: persistence для runs/scenes/artifacts и project CRUD-команды."
    status: pending
  - id: sprint-7-db-jobs-worker
    content: "Спринт 7: generation_jobs/Job, атомарный pick и встроенный worker loop."
    status: pending
  - id: sprint-8-postprocessing-dual-locale
    content: "Спринт 8: post-processing цепочка, FFmpeg merge и финальные RU/EN outputs."
    status: pending
  - id: sprint-9-cutover-stabilization
    content: "Спринт 9: dual-read/dual-write rollout и cutover на PostgreSQL как единственный source."
    status: pending
  - id: epic-optional-extensions
    content: "Опциональные эпики: verify Telegram fallback, fal.ai router, isolated /translate_video."
    status: pending
isProject: false
---

# Unified Plan by Sprints

## Текущая структура

Источник объединения:
- `job-worker-db-loop_b8deae90.plan.md`
- `мультипроектность_и_postgresql_67ce380b.plan.md`
- `postgresql_multiproject_migration_f24404cf.plan.md`
- `universal_pipeline_refactor_12e05e06.plan.md`
- `veo3_telegram_fallback_3bd908ea.plan.md`

Текущий контур системы:
- Telegram state-machine и in-memory состояние (`SESSIONS`) являются центром orchestration.
- Текстовый pipeline частично централизован, но шаги и переходы размазаны по handlers.
- Данные проектов/ассетов частично в коде, а не в едином persistent source.
- Медиа-генерация ориентирована на Vertex-first, post-processing не полностью формализован как job pipeline.

## Архитектура: текущая -> целевая

### Текущая архитектура (as-is)
- Источник данных смешанный: кодовые константы + runtime in-memory state.
- Логика шагов тесно связана с Telegram-handlers.
- Ограниченная трассируемость запусков и артефактов по всему циклу.
- Ограниченная расширяемость под multiproject без точечной доработки кода.

### Целевая архитектура (to-be)
- Единый источник правды: PostgreSQL (проекты, шаги, ревизии, run-сущности, jobs, artifacts).
- Декларативный текстовый pipeline с единым step-engine и историей revisions.
- Полная трассировка run: `generation_runs` -> `scenes`/`scene_revisions` -> `generation_jobs` -> `run_artifacts`.
- Явная multiproject модель с `active_project_id` и project-scoped поведением.
- Core media путь остаётся Vertex-first; дополнительные провайдеры/команды изолированы как опциональные эпики.

## Спринт 1. Baseline и фиксация текущего поведения

Цель спринта: получить стабильную отправную точку, чтобы любые изменения дальше были измеримы и обратимы.

План работ:
- Зафиксировать критичные пользовательские флоу: `/start`, `/generate`, `/image`, `/video`, `/cancel`.
- Зафиксировать baseline тестов и минимальный smoke-набор.
- Описать текущие in-memory точки (`SESSIONS`, cache) и контракт env для БД.

Что изменилось в конце спринта:
- Есть baseline тестов и список критичных сценариев.
- Зафиксирован перечень мест, которые мигрируют с in-memory на PostgreSQL.
- Определён минимальный DoD для следующих спринтов.

## Спринт 2. Декларативный текстовый контур без смены источника данных

Цель спринта: убрать дубли и хрупкость в текстовых шагах, не меняя текущую функциональность.

План работ:
- Ввести `PipelineDefinition/StepSpec` и реестр шагов (зеркало текущего графа).
- Ввести `pipeline_text_engine` с единым циклом run/regenerate для "ОК/фидбек".
- Перевести handlers на диспетчеризацию по step id, сохранив текущий UX.

Что изменилось в конце спринта:
- Текстовый pipeline управляется единым движком шагов.
- Сокращена дублирующая логика в handlers.
- Поведение для пользователя не изменилось, но структура стала расширяемой.

## Спринт 3. PostgreSQL фундамент и миграции

Цель спринта: поднять надёжный инфраструктурный слой БД как основу multiproject.

План работ:
- Подключить SQLAlchemy 2.x, Alembic и Postgres driver.
- Добавить `db/session` слой и health-check при старте.
- Создать baseline миграции и документировать `upgrade/downgrade`.

Что изменилось в конце спринта:
- Приложение стабильно стартует с PostgreSQL.
- Миграции воспроизводимы в чистом окружении.
- Есть единый слой транзакций и подключения к БД.

## Спринт 4. Multiproject schema v1 и seed default project

Цель спринта: перенести основу проектных данных из кода в БД.

План работ:
- Создать `projects`, глобальные `characters`/`locations`, связи `project_characters`/`project_locations`.
- Создать `text_steps`, `text_step_revisions`, `current_revision_id`.
- Выполнить seed default project из текущих ассетов и шагов.

Что изменилось в конце спринта:
- Дефолтный проект создаётся автоматически в БД.
- Базовые сущности проекта читаются из PostgreSQL.
- Подготовлена база для project-scoped pipeline.

## Спринт 5. Active project и DB-driven text pipeline

Цель спринта: сделать `/generate` полностью project-aware и управляемым из БД.

План работ:
- Добавить `active_project_id` в сессию и команды выбора проекта.
- Переключить шаги на чтение из `text_steps` (по проекту и порядку).
- Писать историю в `text_step_revisions` с `context_snapshot`.
- При подтверждении "ОК" обновлять `current_revision_id`.

Что изменилось в конце спринта:
- Каждый запуск текстового pipeline работает в контексте выбранного проекта.
- Полная история итераций шагов сохраняется в БД.
- Утверждённые версии шагов фиксируются явно.

## Спринт 6. Run/Scene/Artifact persistence и проектные CRUD-команды

Цель спринта: получить сквозную наблюдаемость generation-run и управляемость сущностями проекта.

План работ:
- Добавить `generation_runs`, `scenes`, `scene_revisions`, `scene_characters`, `run_artifacts`.
- Сохранять утверждённый сценарий и сцены в БД перед медиа-этапами.
- Добавить `/character_add` и `/location_add` с проектной привязкой.

Что изменилось в конце спринта:
- Для каждого run доступны сцены, ревизии и артефакты.
- Проектные персонажи/локации управляются командами из бота.
- In-memory перестаёт быть основным source of truth для run-данных.

## Спринт 7. DB jobs и встроенный worker loop

Цель спринта: стандартизировать выполнение долгих задач через БД-очередь.

План работ:
- Добавить `generation_jobs` (или `Job`) со статусами `pending/running/done/failed`.
- Реализовать атомарный pick (`FOR UPDATE SKIP LOCKED`) и daemon worker loop.
- Подключить запуск воркера при старте приложения и ввести job-логирование.

Что изменилось в конце спринта:
- Долгие этапы выполняются через управляемую очередь jobs.
- Параллельные воркеры не конфликтуют при захвате одной задачи.
- Ошибки и ретраи отражаются в статусах и логах.

## Спринт 8. Post-processing и dual-locale финальные выходы

Цель спринта: завершить production pipeline до готового RU/EN результата.

План работ:
- Ввести job-цепочку post-processing: merge, upscale, subtitles, voiceover, final render.
- Реализовать MVP склейку на локальном FFmpeg.
- Собирать финальные RU/EN артефакты и social output по правилам проекта.

Что изменилось в конце спринта:
- Стабильно формируются финальные выходы для двух локалей.
- Прогресс run прозрачно виден по stage/status/events.
- Пайплайн закрывает путь от текста до публикационного пакета.

## Спринт 9. Cutover и стабилизация production

Цель спринта: завершить миграцию и убрать зависимость от legacy путей.

План работ:
- Включить временный dual-read/dual-write с логированием fallback.
- После стабилизации отключить fallback на legacy источники.
- Оставить legacy fallback только как dev-режим (опционально).

Что изменилось в конце спринта:
- Production полностью работает на PostgreSQL-контуре.
- Legacy источники исключены из основного рабочего пути.
- Риски расхождения между кодом и данными минимизированы.

## Опциональные эпики (после core roadmap)

Цель: расширения, которые не должны блокировать основной multiproject rollout.

Состав:
- Verify/maintain уже внедрённого `veo3` Telegram fallback.
- Добавление `fal.ai` как второго video-provider через отдельный роутер.
- Глобальная `/translate_video` как отдельный изолированный поток.

Что изменилось в конце эпика:
- Новые возможности подключены без влияния на стабильность core pipeline.
- Core архитектурные инварианты (Vertex-first основной путь, project-scoped state) сохраняются.

## Общие критерии готовности по программе

- Все проектные данные, шаги, run-состояния и артефакты хранятся в PostgreSQL.
- Текстовый и медиа pipeline работают в контексте `active_project_id`.
- Есть полная трассируемость по `project_id/chat_id/run_id/job_id`.
- Документация и CI отражают миграции, запуск и контроль качества.
