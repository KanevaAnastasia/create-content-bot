from __future__ import annotations

import uuid

from django import forms
from django.db import models


class LenientJSONFormField(forms.JSONField):
    """Accept values already decoded by the database driver."""

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


class ChatSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    chat_id = models.IntegerField(unique=True)
    state = models.CharField(max_length=128, blank=True, null=True)
    session_json = LenientJSONField(default=dict)
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
    session_snapshot_json = LenientJSONField(default=dict)
    payload_json = LenientJSONField(default=dict)
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
    status = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    current_scene_index = models.IntegerField(blank=True, null=True)
    current_media_substep = models.CharField(max_length=128, blank=True, null=True)
    current_media_language_index = models.IntegerField(blank=True, null=True)
    human_title = models.TextField(blank=True, null=True)
    language = models.CharField(max_length=8)
    series = models.ForeignKey("ContentSeries", db_column="series_id", blank=True, null=True, on_delete=models.DO_NOTHING)
    series_video_index = models.IntegerField(blank=True, null=True)
    series_total_videos = models.IntegerField(blank=True, null=True)
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


class ContentSeries(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    project_id = models.UUIDField(blank=True, null=True)
    slug = models.CharField(max_length=128)
    title_ru = models.TextField()
    title_en = models.TextField()
    current_video_index = models.IntegerField(default=1)
    total_videos = models.IntegerField(default=10)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, null=True)
    meta_json = LenientJSONField(default=dict)
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
    slug = models.CharField(max_length=128, unique=True)
    title = models.TextField()
    description = models.TextField(blank=True, null=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    final_scene_prompt = models.TextField(blank=True, null=True)
    title_preset = models.TextField(blank=True, null=True)
    hashtags_fixed_ru = LenientJSONField(default=list)
    hashtags_fixed_en = LenientJSONField(default=list)
    default_video_provider = models.CharField(max_length=64, blank=True, null=True)
    allowed_video_models = LenientJSONField(blank=True, null=True)
    background_music_asset_uri = models.TextField(blank=True, null=True)
    background_music_volume = models.FloatField(blank=True, null=True)
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
    meta_json = LenientJSONField(default=dict)
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
    hashtags_json = LenientJSONField(default=list)
    recipe = models.TextField(blank=True, null=True)
    publish_policy = models.CharField(max_length=64, default="auto_after_copy_approval")
    payload_json = LenientJSONField(default=dict)
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
        verbose_name = "Publication job / публикация"
        verbose_name_plural = "Publication jobs / публикации"

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
    video_prompt_by_lang = LenientJSONField(default=dict)
    speech_by_lang = LenientJSONField(default=dict)
    syntax_prompt_by_lang = LenientJSONField(default=dict)
    prompt_meta_json = LenientJSONField(default=dict)

    class Meta:
        managed = False
        db_table = "scenes"
        ordering = ("run_id", "scene_order")
        verbose_name = "Scene / сцена"
        verbose_name_plural = "Scenes / сцены"

    def __str__(self) -> str:
        return f"Scene {self.scene_order}: {self.text[:80]}"


class GenerationArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    run = models.ForeignKey(GenerationRun, db_column="run_id", on_delete=models.DO_NOTHING)
    scene = models.ForeignKey(Scene, db_column="scene_id", blank=True, null=True, on_delete=models.DO_NOTHING)
    artifact_type = models.CharField(max_length=64)
    label = models.TextField()
    uri = models.TextField()
    language = models.CharField(max_length=8, blank=True, null=True)
    mime_type = models.CharField(max_length=128, blank=True, null=True)
    payload_json = LenientJSONField(default=dict)
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
    image_blob = models.BinaryField()
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
    video_blob = models.BinaryField()
    liked = models.BooleanField(default=False)
    approved_for_production = models.BooleanField(default=False)
    speed_coef = models.FloatField(default=1.2)
    source_image_candidate_id = models.CharField(max_length=64, blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "scene_video_candidates"
        verbose_name = "Scene video candidate / видео сцены"
        verbose_name_plural = "Scene video candidates / видео сцен"

    def __str__(self) -> str:
        return f"{self.language}: {self.candidate_id}"
