from __future__ import annotations

import uuid

from django import forms
from django.db import models


class LenientJSONFormField(forms.JSONField):
    """Accept values already decoded by the database driver."""

    empty_values = (None, "")

    def to_python(self, value):
        if isinstance(value, (dict, list, int, float, bool)):
            return value
        return super().to_python(value)


class LenientJSONField(models.JSONField):
    """Django admin JSON field tolerant of psycopg/sqlite decoded values."""

    def from_db_value(self, value, expression, connection):
        if value is None or isinstance(value, (dict, list, int, float, bool)):
            return value
        return super().from_db_value(value, expression, connection)

    def formfield(self, **kwargs):
        kwargs.setdefault("form_class", LenientJSONFormField)
        return super().formfield(**kwargs)


class AdvertisingQueueSchedule(models.Model):
    id = models.IntegerField(primary_key=True, default=1)
    enabled = models.BooleanField("Включена", default=True)
    next_run_at = models.DateTimeField("Следующий запуск")

    class Meta:
        managed = False
        db_table = "advertising_queue_schedule"
        verbose_name = "Расписание рекламы"
        verbose_name_plural = "Расписание рекламы (каждые 2 дня, 09:00 МСК)"

    def __str__(self):
        return "Реклама для бабушки · каждые 2 дня"


class AdvertisingProduct(models.Model):
    format = models.CharField("Тип", max_length=16, choices=[("clip", "Клип"), ("clip_advice", "Клип — совет"), ("post", "Пост")], default="clip")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    title = models.TextField("Название")
    images = LenientJSONField(default=list, blank=True)
    comment = models.TextField("Комментарий", blank=True, default="")
    position = models.PositiveIntegerField("Порядок", default=0)
    status = models.CharField("Статус", max_length=32, default="pending", choices=[
        ("pending", "В очереди"), ("preparing", "Готовятся сценарии"),
        ("ready", "Ожидает свободного диалога"), ("offered", "На согласовании / в работе"),
        ("completed", "Обработан"), ("failed", "Ошибка подготовки")])
    prepared = LenientJSONField(default=dict, blank=True)
    error = models.TextField("Ошибка", default="", blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    refill_notified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "advertising_products"
        ordering = ("position", "created_at", "id")
        verbose_name = "Рекламный товар"
        verbose_name_plural = "Рекламные товары"

    def __str__(self):
        return self.title


class BotUser(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    telegram_id = models.BigIntegerField(blank=True, null=True, unique=True)
    normalized_username = models.CharField(max_length=128, blank=True, null=True, unique=True)
    display_name = models.TextField(blank=True, default="")
    role = models.CharField(max_length=16, choices=(("admin", "admin"), ("guest", "guest")))
    is_active = models.BooleanField(default=True)
    quiet_mode_enabled = models.BooleanField(default=False)
    quiet_mode_auto_enabled = models.BooleanField(default=False)
    quiet_mode_auto_cycle_date = models.DateField(blank=True, null=True)
    quiet_mode_auto_generation_count = models.IntegerField(default=0)
    quiet_mode_auto_generation_target = models.IntegerField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "bot_users"
        ordering = ("role", "normalized_username", "telegram_id")
        verbose_name = "Bot user / пользователь бота"
        verbose_name_plural = "Bot users / пользователи бота"

    def __str__(self) -> str:
        identity = self.normalized_username or self.telegram_id or self.id
        return f"{self.display_name or identity} ({self.role})"


class ChatSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    chat_id = models.IntegerField(unique=True)
    state = models.CharField(max_length=128, blank=True, null=True)
    session_json = LenientJSONField(default=dict, blank=True)
    generation_run_id = models.UUIDField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "chat_sessions"
        verbose_name = "Chat session / состояние диалога"
        verbose_name_plural = "Chat sessions / состояния диалогов"

    def __str__(self) -> str:
        return f"{self.chat_id}: {self.state or '-'}"


class PipelineStepCheckpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    chat_id = models.IntegerField()
    generation_run_id = models.UUIDField(blank=True, null=True)
    step_key = models.CharField(max_length=128)
    scene_index = models.IntegerField(blank=True, null=True)
    substep = models.CharField(max_length=128, blank=True, null=True)
    language = models.CharField(max_length=8, blank=True, null=True)
    session_snapshot_json = LenientJSONField(default=dict, blank=True)
    payload_json = LenientJSONField(default=dict, blank=True)
    restored_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "pipeline_step_checkpoints"
        ordering = ("-created_at",)
        verbose_name = "Pipeline checkpoint / точка восстановления"
        verbose_name_plural = "Pipeline checkpoints / точки восстановления"

    def __str__(self) -> str:
        scene = "" if self.scene_index is None else f" scene={self.scene_index}"
        substep = "" if not self.substep else f" {self.substep}"
        return f"{self.chat_id}: {self.step_key}{scene}{substep}"


class GenerationRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    project = models.ForeignKey("Project", db_column="project_id", blank=True, null=True, on_delete=models.DO_NOTHING)
    workflow_type = models.CharField(max_length=16, blank=True, null=True)
    status = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    is_draft = models.BooleanField(default=False)
    current_scene_index = models.IntegerField(blank=True, null=True)
    current_media_substep = models.CharField(max_length=128, blank=True, null=True)
    current_media_language_index = models.IntegerField(blank=True, null=True)
    human_title = models.TextField(blank=True, null=True)
    title_color = models.CharField(max_length=32, default="#c72638")
    language = models.CharField(max_length=8)
    series = models.ForeignKey("ContentSeries", db_column="series_id", blank=True, null=True, on_delete=models.DO_NOTHING)
    series_video_index = models.IntegerField(blank=True, null=True)
    series_total_videos = models.IntegerField(blank=True, null=True)
    scheduled_item = models.ForeignKey(
        "ScheduledGenerationItem",
        db_column="scheduled_item_id",
        blank=True,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="runs_by_scheduled_item",
    )
    script = models.TextField()
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "generation_runs"
        ordering = ("-created_at",)
        verbose_name = "Generation run / запуск генерации"
        verbose_name_plural = "Generation runs / запуски генерации"

    def __str__(self) -> str:
        return self.human_title or self.script[:120] or str(self.id)


class ScheduledGenerationItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    title = models.TextField()
    recipe = models.TextField()
    comment = models.TextField(blank=True, null=True)
    overlay_subtitle = models.TextField(blank=True, null=True)
    title_color = models.CharField(max_length=32, default="#c72638")
    run_at = models.DateTimeField()
    is_draft = models.BooleanField(default=True)
    status = models.CharField(max_length=64, default="pending")
    generation_run = models.ForeignKey(
        GenerationRun,
        db_column="generation_run_id",
        blank=True,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="scheduled_generation_items_by_result",
    )
    series = models.ForeignKey(
        "ContentSeries",
        db_column="series_id",
        blank=True,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="scheduled_generation_items",
    )
    source_row_key = models.CharField(max_length=128, blank=True, null=True, unique=True)
    source_file_path = models.TextField(blank=True, null=True)
    error_text = models.TextField(blank=True, null=True)
    payload_json = LenientJSONField(default=dict, blank=True)
    claimed_at = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "scheduled_generation_items"
        ordering = ("run_at", "created_at")
        verbose_name = "Scheduled generation item / ночная очередь"
        verbose_name_plural = "Scheduled generation items / ночная очередь"

    def __str__(self) -> str:
        return f"{self.run_at:%Y-%m-%d %H:%M} {self.title}"


class GenerationDraft(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    chat_id = models.IntegerField(blank=True, null=True)
    run = models.ForeignKey(GenerationRun, db_column="run_id", blank=True, null=True, on_delete=models.DO_NOTHING)
    title = models.TextField()
    stage = models.CharField(max_length=128, blank=True, null=True)
    scene_index = models.IntegerField(blank=True, null=True)
    substep = models.CharField(max_length=128, blank=True, null=True)
    language = models.CharField(max_length=8, blank=True, null=True)
    session_snapshot_json = LenientJSONField(default=dict, blank=True)
    payload_json = LenientJSONField(default=dict, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "generation_drafts"
        ordering = ("-created_at",)
        verbose_name = "Generation draft / черновик"
        verbose_name_plural = "Generation drafts / черновики"

    def __str__(self) -> str:
        return self.title or str(self.id)


class ScheduledGenerationEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    scheduled_item = models.ForeignKey(
        ScheduledGenerationItem,
        db_column="scheduled_item_id",
        on_delete=models.DO_NOTHING,
        related_name="events",
    )
    generation_run = models.ForeignKey(
        GenerationRun,
        db_column="generation_run_id",
        blank=True,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="scheduled_generation_events",
    )
    event_type = models.CharField(max_length=64)
    stage = models.CharField(max_length=64, blank=True, null=True)
    status = models.CharField(max_length=64, blank=True, null=True)
    message = models.TextField(blank=True)
    payload_json = LenientJSONField(default=dict, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "scheduled_generation_events"
        ordering = ("-created_at",)
        verbose_name = "Scheduled generation event / событие ночной очереди"
        verbose_name_plural = "Scheduled generation events / события ночной очереди"

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.event_type}"


class ContentSeries(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    project_id = models.UUIDField(blank=True, null=True)
    slug = models.CharField(max_length=128)
    title_ru = models.TextField()
    title_en = models.TextField()
    narrator_voice_id = models.CharField("Голос серии", max_length=128, null=True, blank=True)
    current_video_index = models.IntegerField(default=1)
    total_videos = models.IntegerField(default=10)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, null=True)
    meta_json = LenientJSONField(default=dict, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "content_series"
        ordering = ("title_ru",)
        verbose_name = "Content series / серия"
        verbose_name_plural = "Content series / серии"

    def __str__(self) -> str:
        return f"{self.title_ru} / {self.title_en} ({self.current_video_index} из {self.total_videos})"


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    owner = models.ForeignKey(
        BotUser,
        db_column="owner_user_id",
        blank=True,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="projects",
    )
    slug = models.CharField(max_length=128, unique=True)
    title = models.TextField()
    description = models.TextField(blank=True, null=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    final_scene_prompt = models.TextField(blank=True, null=True)
    title_preset = models.TextField(blank=True, null=True)
    hashtags_fixed_ru = LenientJSONField(default=list, blank=True)
    hashtags_fixed_en = LenientJSONField(default=list, blank=True)
    default_video_provider = models.CharField(max_length=64, blank=True, null=True)
    allowed_video_models = LenientJSONField(blank=True, null=True)
    background_music_asset_uri = models.TextField(blank=True, null=True)
    background_music_volume = models.FloatField(blank=True, null=True)
    elevenlabs_voice_id = models.CharField(max_length=128, blank=True, null=True)
    default_generation_mode = models.CharField(max_length=32, blank=True, null=True)
    style_source_project_slug = models.CharField(max_length=128, blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "projects"
        ordering = ("slug",)
        verbose_name = "Project / проект"
        verbose_name_plural = "Projects / проекты"

    def __str__(self) -> str:
        return self.title or self.slug


class ProjectPublishAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    project = models.ForeignKey(Project, db_column="project_id", on_delete=models.DO_NOTHING)
    platform = models.CharField(max_length=64)
    language = models.CharField(max_length=8)
    account_key = models.CharField(max_length=128, default="default")
    external_account_id = models.TextField(blank=True, null=True)
    publish_policy = models.CharField(max_length=64, default="auto_after_copy_approval")
    is_active = models.BooleanField(default=True)
    meta_json = LenientJSONField(default=dict, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "project_publish_accounts"
        ordering = ("project", "platform", "language", "account_key")
        verbose_name = "Publish account / аккаунт публикации"
        verbose_name_plural = "Publish accounts / аккаунты публикации"

    def __str__(self) -> str:
        state = "active" if self.is_active else "inactive"
        return f"{self.project_id} {self.platform}:{self.language}/{self.account_key} ({state})"


class PublicationJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    project = models.ForeignKey(Project, db_column="project_id", on_delete=models.DO_NOTHING)
    bundle_key = models.CharField(max_length=128)
    origin = models.CharField(max_length=64, default="production_final")
    platform = models.CharField(max_length=64)
    language = models.CharField(max_length=8)
    account_key = models.CharField(max_length=128, default="default")
    external_account_id = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=64, default="draft")
    video_path = models.TextField()
    video_label = models.CharField(max_length=128, blank=True, null=True)
    title = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    hashtags_json = LenientJSONField(default=list, blank=True)
    recipe = models.TextField(blank=True, null=True)
    publish_policy = models.CharField(max_length=64, default="auto_after_copy_approval")
    payload_json = LenientJSONField(default=dict, blank=True)
    scheduled_at = models.DateTimeField(blank=True, null=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    published_at = models.DateTimeField(blank=True, null=True)
    external_post_id = models.TextField(blank=True, null=True)
    publish_error = models.TextField(blank=True, null=True)
    attempt_count = models.IntegerField(default=0)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "publication_jobs"
        ordering = ("-created_at",)
        verbose_name = "Задание заливки"
        verbose_name_plural = "Заливки"

    def __str__(self) -> str:
        return f"{self.platform}:{self.language} {self.status}"


class VideoOverview(GenerationRun):
    class Meta:
        proxy = True
        verbose_name = "Video / обзор видео"
        verbose_name_plural = "Videos / обзор видео"


class Scene(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    run = models.ForeignKey(GenerationRun, db_column="run_id", on_delete=models.DO_NOTHING)
    scene_order = models.IntegerField()
    text = models.TextField()
    duration_sec = models.FloatField()
    start_frame_prompt = models.TextField(blank=True, null=True)
    start_frame_prompt_effective = models.TextField(blank=True, null=True)
    end_frame_prompt = models.TextField(blank=True, null=True)
    video_prompt_by_lang = LenientJSONField(default=dict, blank=True)
    speech_by_lang = LenientJSONField(default=dict, blank=True)
    syntax_prompt_by_lang = LenientJSONField(default=dict, blank=True)
    prompt_meta_json = LenientJSONField(default=dict, blank=True)

    class Meta:
        managed = False
        db_table = "scenes"
        ordering = ("run_id", "scene_order")
        verbose_name = "Scene / сцена"
        verbose_name_plural = "Scenes / сцены"

    def __str__(self) -> str:
        return f"Scene {self.scene_order}: {self.text[:80]}"


class SceneTextRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    run = models.ForeignKey(GenerationRun, db_column="run_id", on_delete=models.DO_NOTHING)
    scene = models.ForeignKey(Scene, db_column="scene_id", blank=True, null=True, on_delete=models.DO_NOTHING)
    scene_order = models.IntegerField(blank=True, null=True)
    field_name = models.CharField(max_length=64)
    language = models.CharField(max_length=8, blank=True, null=True)
    old_text = models.TextField(blank=True)
    new_text = models.TextField(blank=True)
    source = models.CharField(max_length=64, default="admin")
    actor = models.CharField(max_length=256, blank=True, null=True)
    meta_json = LenientJSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "scene_text_revisions"
        ordering = ("-created_at",)
        verbose_name = "Scene text revision / история текста сцены"
        verbose_name_plural = "Scene text revisions / история текстов сцен"

    def __str__(self) -> str:
        lang = f":{self.language}" if self.language else ""
        return f"Scene {self.scene_order or '-'} {self.field_name}{lang} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class GenerationArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    run = models.ForeignKey(GenerationRun, db_column="run_id", on_delete=models.DO_NOTHING)
    scene = models.ForeignKey(Scene, db_column="scene_id", blank=True, null=True, on_delete=models.DO_NOTHING)
    artifact_type = models.CharField(max_length=64)
    label = models.TextField()
    uri = models.TextField()
    language = models.CharField(max_length=8, blank=True, null=True)
    mime_type = models.CharField(max_length=128, blank=True, null=True)
    payload_json = LenientJSONField(default=dict, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "generation_artifacts"
        ordering = ("-created_at",)
        verbose_name = "Generation artifact / файл или ссылка"
        verbose_name_plural = "Generation artifacts / файлы и ссылки"

    def __str__(self) -> str:
        return self.label


class SceneImageCandidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    candidate_id = models.CharField(max_length=64, unique=True)
    scene = models.ForeignKey(Scene, db_column="scene_id", on_delete=models.DO_NOTHING)
    kind = models.CharField(max_length=16)
    image_blob = models.BinaryField(blank=True, null=True)
    media_uri = models.TextField(blank=True, null=True)
    mime_type = models.CharField(max_length=128, blank=True, null=True)
    byte_size = models.IntegerField(blank=True, null=True)
    liked = models.BooleanField(default=False)
    approved_for_video = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "scene_image_candidates"
        verbose_name = "Scene image candidate / кадр сцены"
        verbose_name_plural = "Scene image candidates / кадры сцен"

    def __str__(self) -> str:
        return f"{self.kind}: {self.candidate_id}"


class SceneVideoCandidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    candidate_id = models.CharField(max_length=64, unique=True)
    scene = models.ForeignKey(Scene, db_column="scene_id", on_delete=models.DO_NOTHING)
    language = models.CharField(max_length=8)
    video_blob = models.BinaryField(blank=True, null=True)
    media_uri = models.TextField(blank=True, null=True)
    mime_type = models.CharField(max_length=128, blank=True, null=True)
    byte_size = models.IntegerField(blank=True, null=True)
    liked = models.BooleanField(default=False)
    approved_for_production = models.BooleanField(default=False)
    speed_coef = models.FloatField(default=1.0)
    source_image_candidate_id = models.CharField(max_length=64, blank=True, null=True)
    video_prompt_by_lang = LenientJSONField(default=dict, blank=True)
    speech_by_lang = LenientJSONField(default=dict, blank=True)
    speech_cues_by_lang = LenientJSONField(default=dict, blank=True)
    speech_plan_by_lang = LenientJSONField(default=dict, blank=True)
    video_speech_qa_by_lang = LenientJSONField(default=dict, blank=True)
    generation_params_json = LenientJSONField(default=dict, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "scene_video_candidates"
        verbose_name = "Scene video candidate / видео сцены"
        verbose_name_plural = "Scene video candidates / видео сцен"

    def __str__(self) -> str:
        return f"{self.language}: {self.candidate_id}"


class DisciplineQueueSchedule(models.Model):
    id = models.IntegerField(primary_key=True, default=1)
    enabled = models.BooleanField("Включена", default=True)
    next_run_at = models.DateTimeField("Следующая распаковка", help_text="Раз в два дня. Время отображается в часовом поясе админки.")
    class Meta:
        managed = False
        db_table = "discipline_queue_schedule"
        verbose_name = "Расписание Дисциплины"
        verbose_name_plural = "Расписание Дисциплины"
    def __str__(self):
        return "Утренняя распаковка каждые два дня"


class DisciplineIdea(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    slug = models.SlugField("Ключ", max_length=100, unique=True)
    title = models.TextField("Название")
    position = models.PositiveIntegerField("Порядок", default=0)
    mode = models.CharField("Режим", max_length=64)
    series = models.TextField("Серия", default="", blank=True)
    content_series = models.ForeignKey(ContentSeries, verbose_name="Настройки серии и голоса", null=True, blank=True, on_delete=models.SET_NULL, db_column="content_series_id", related_name="discipline_ideas")
    episode = models.PositiveIntegerField("Выпуск", default=0)
    script = models.TextField("Текст озвучки")
    direction = models.TextField("Сценарий видеоряда")
    review_plan = models.TextField("Приёмы, источники и план проверки", default="", blank=True)
    problem = models.TextField("Проблема зрителя")
    hook = models.TextField("Первые две секунды")
    payoff = models.TextField("Развязка и польза")
    sources = LenientJSONField("Источники и референсы", default=list, blank=True)
    status = models.CharField("Статус", max_length=32, default="pending", choices=[
        ("pending", "В очереди"), ("ready", "Ожидает доставки"), ("offered", "Идея на согласовании"),
        ("in_progress", "Совместная генерация"), ("paused", "Генерация прервана"), ("deferred", "На следующую распаковку"),
        ("completed", "Готово"), ("skipped", "Отклонено")])
    offered_at = models.DateTimeField("Предложена", null=True, blank=True)
    completed_at = models.DateTimeField("Завершена", null=True, blank=True)
    run_id = models.CharField(max_length=64, null=True, blank=True)
    delivery_index = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        managed = False
        db_table = "discipline_ideas"
        ordering = ("position", "created_at", "id")
        verbose_name = "Сценарий Дисциплины"
        verbose_name_plural = "Дисциплина · очередь сценариев"
    def __str__(self):
        return self.title
