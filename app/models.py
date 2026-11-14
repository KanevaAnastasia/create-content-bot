"""
ORM-модели проекта.

Сейчас БД постепенно становится source of truth для проектов, шагов пайплайна,
запусков генерации, сцен и результатов видео.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import BigInteger, Boolean, Date, Enum, Float, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import DateTime, Uuid

from app.db import Base


class AdvertisingQueueSchedule(Base):
    """Single durable cadence for the owner's advertising queue."""

    __tablename__ = "advertising_queue_schedule"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdvertisingProduct(Base):
    __tablename__ = "advertising_products"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    images: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    prepared: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    claimed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refill_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class BotUser(Base):
    """Telegram user allowed to access the bot."""

    __tablename__ = "bot_users"
    __table_args__ = (
        Index("ix_bot_users_telegram_id", "telegram_id"),
        Index("ix_bot_users_normalized_username", "normalized_username"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    normalized_username: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    role: Mapped[str] = mapped_column(Enum("admin", "guest", name="bot_user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    quiet_mode_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quiet_mode_auto_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quiet_mode_auto_cycle_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    quiet_mode_auto_generation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quiet_mode_auto_generation_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    projects: Mapped[list["Project"]] = relationship(back_populates="owner")


class Project(Base):
    """Проект контента: набор ассетов, шагов, правил и будущих output-настроек."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("bot_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    final_scene_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_preset: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtags_fixed_ru: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    hashtags_fixed_en: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    default_video_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allowed_video_models: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    background_music_asset_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    background_music_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    elevenlabs_voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_generation_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    style_source_project_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    owner: Mapped["BotUser | None"] = relationship(back_populates="projects")
    characters: Mapped[list["ProjectCharacter"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    locations: Mapped[list["ProjectLocation"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    visual_props: Mapped[list["VisualProp"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    text_steps: Mapped[list["TextStep"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    publish_accounts: Mapped[list["ProjectPublishAccount"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )
    publication_jobs: Mapped[list["PublicationJob"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )
    content_series: Mapped[list["ContentSeries"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )
    generation_runs: Mapped[list["GenerationRun"]] = relationship(back_populates="project")


class ContentSeries(Base):
    """Named short-form content series with localized titles and episode counter."""

    __tablename__ = "content_series"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_content_series_project_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title_ru: Mapped[str] = mapped_column(Text, nullable=False)
    title_en: Mapped[str] = mapped_column(Text, nullable=False)
    current_video_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_videos: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    project: Mapped["Project | None"] = relationship(back_populates="content_series")
    generation_runs: Mapped[list["GenerationRun"]] = relationship(back_populates="series")


class Character(Base):
    """Глобальный каталог персонажей, переиспользуемых разными проектами."""

    __tablename__ = "characters"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_asset_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    projects: Mapped[list["ProjectCharacter"]] = relationship(back_populates="character", cascade="all, delete-orphan")


class Location(Base):
    """Глобальный каталог локаций, доступных проектам через M:N связь."""

    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_code: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_asset_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    projects: Mapped[list["ProjectLocation"]] = relationship(back_populates="location", cascade="all, delete-orphan")


class ProjectCharacter(Base):
    """Связь проекта с персонажем и проектные override-настройки."""

    __tablename__ = "project_characters"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    character_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("characters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    project: Mapped["Project"] = relationship(back_populates="characters")
    character: Mapped["Character"] = relationship(back_populates="projects")


class ProjectLocation(Base):
    """Связь проекта с локацией и проектные override-настройки ассета."""

    __tablename__ = "project_locations"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("locations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    asset_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    project: Mapped["Project"] = relationship(back_populates="locations")
    location: Mapped["Location"] = relationship(back_populates="projects")


class VisualProp(Base):
    """Проектный каталог визуальных констант предметов для prompt continuity."""

    __tablename__ = "visual_props"
    __table_args__ = (UniqueConstraint("project_id", "canonical_key", name="uq_visual_props_project_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_key: Mapped[str] = mapped_column(String(128), nullable=False)
    name_ru: Mapped[str] = mapped_column(Text, nullable=False)
    description_ru: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    project: Mapped["Project"] = relationship(back_populates="visual_props")


class TextStep(Base):
    """Настраиваемый текстовый шаг пайплайна внутри проекта."""

    __tablename__ = "text_steps"
    __table_args__ = (
        UniqueConstraint("project_id", "step_key", name="uq_text_steps_project_step_key"),
        UniqueConstraint("project_id", "step_order", name="uq_text_steps_project_step_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_key: Mapped[str] = mapped_column(String(128), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    base_system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("text_step_revisions.id", use_alter=True, name="fk_text_steps_current_revision_id"),
        nullable=True,
    )
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    project: Mapped["Project"] = relationship(back_populates="text_steps")
    revisions: Mapped[list["TextStepRevision"]] = relationship(
        back_populates="text_step",
        cascade="all, delete-orphan",
        foreign_keys="TextStepRevision.text_step_id",
    )
    current_revision: Mapped["TextStepRevision | None"] = relationship(
        foreign_keys=[current_revision_id],
        post_update=True,
    )


class TextStepRevision(Base):
    """История LLM-итераций текстового шага с входным контекстом и ответом модели."""

    __tablename__ = "text_step_revisions"
    __table_args__ = (UniqueConstraint("text_step_id", "revision_no", name="uq_text_step_revisions_step_revision_no"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    text_step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("text_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    base_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    user_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_response: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    text_step: Mapped["TextStep"] = relationship(
        back_populates="revisions",
        foreign_keys=[text_step_id],
    )


class GenerationRun(Base):
    """Запуск пайплайна генерации (один пользовательский run)."""

    __tablename__ = "generation_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workflow_type: Mapped[str | None] = mapped_column(
        Enum("image", "video", "cartoon", name="generation_workflow_type"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_scene_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_media_substep: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_media_language_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    human_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_color: Mapped[str] = mapped_column(String(32), nullable=False, default="#c72638")
    language: Mapped[str] = mapped_column(
        Enum("ru", "en", name="generation_language"),
        nullable=False,
    )
    series_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("content_series.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    series_video_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    series_total_videos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("scheduled_generation_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    script: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    project: Mapped["Project | None"] = relationship(back_populates="generation_runs")
    series: Mapped["ContentSeries | None"] = relationship(back_populates="generation_runs")
    scheduled_item: Mapped["ScheduledGenerationItem | None"] = relationship(
        back_populates="generation_runs",
        foreign_keys=[scheduled_item_id],
    )
    scenes: Mapped[list["Scene"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    video_results: Mapped[list["VideoResult"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list["GenerationArtifact"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class ScheduledGenerationItem(Base):
    """Night queue item imported from admin Excel or created manually."""

    __tablename__ = "scheduled_generation_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    recipe: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    overlay_subtitle: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_color: Mapped[str] = mapped_column(String(32), nullable=False, default="#c72638")
    run_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending", index=True)
    generation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    series_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("content_series.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_row_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    source_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    claimed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    generation_run: Mapped["GenerationRun | None"] = relationship(foreign_keys=[generation_run_id])
    series: Mapped["ContentSeries | None"] = relationship(foreign_keys=[series_id])
    generation_runs: Mapped[list["GenerationRun"]] = relationship(
        back_populates="scheduled_item",
        foreign_keys="GenerationRun.scheduled_item_id",
    )
    events: Mapped[list["ScheduledGenerationEvent"]] = relationship(
        back_populates="scheduled_item",
        cascade="all, delete-orphan",
    )


class ScheduledGenerationEvent(Base):
    """Auditable timeline event for scheduled/unattended generation."""

    __tablename__ = "scheduled_generation_events"
    __table_args__ = (
        Index("ix_scheduled_generation_events_scheduled_item_created", "scheduled_item_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scheduled_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("scheduled_generation_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    scheduled_item: Mapped["ScheduledGenerationItem"] = relationship(back_populates="events")
    generation_run: Mapped["GenerationRun | None"] = relationship()


class GenerationDraft(Base):
    """Saved draft marker that can reopen a generation run for fixes."""

    __tablename__ = "generation_drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scene_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    substep: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    session_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    run: Mapped["GenerationRun | None"] = relationship()


class Scene(Base):
    """Сцена, принадлежащая конкретному запуску генерации."""

    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("run_id", "scene_order", name="uq_scenes_run_scene_order"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_order: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False)
    start_frame_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_frame_prompt_effective: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_frame_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_prompt_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    speech_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    syntax_prompt_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    prompt_meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped["GenerationRun"] = relationship(back_populates="scenes")
    video_results: Mapped[list["VideoResult"]] = relationship(back_populates="scene", cascade="all, delete-orphan")
    image_candidates: Mapped[list["SceneImageCandidate"]] = relationship(
        back_populates="scene",
        cascade="all, delete-orphan",
    )
    media_message_links: Mapped[list["SceneMediaMessageLink"]] = relationship(
        back_populates="scene",
        cascade="all, delete-orphan",
    )
    video_candidates: Mapped[list["SceneVideoCandidate"]] = relationship(
        back_populates="scene",
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list["GenerationArtifact"]] = relationship(
        back_populates="scene",
        passive_deletes=True,
    )
    text_revisions: Mapped[list["SceneTextRevision"]] = relationship(
        back_populates="scene",
        passive_deletes=True,
    )


class SceneTextRevision(Base):
    """Auditable scene text/speech edits made outside opaque session snapshots."""

    __tablename__ = "scene_text_revisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scene_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scene_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    old_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    new_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="admin")
    actor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    run: Mapped["GenerationRun"] = relationship()
    scene: Mapped["Scene | None"] = relationship(back_populates="text_revisions")


class GenerationArtifact(Base):
    """Human-readable artifact index for one generation run."""

    __tablename__ = "generation_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scene_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    run: Mapped["GenerationRun"] = relationship(back_populates="artifacts")
    scene: Mapped["Scene | None"] = relationship(back_populates="artifacts")


class VideoResult(Base):
    """Результат генерации/доставки видео для сцены."""

    __tablename__ = "video_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Enum("veo", "telegram", name="video_provider"), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("pending", "success", "failed", name="video_result_status"),
        nullable=False,
    )
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_breakdown_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    run: Mapped["GenerationRun"] = relationship(back_populates="video_results")
    scene: Mapped["Scene"] = relationship(back_populates="video_results")


class ChatSession(Base):
    """Persistent snapshot of the active Telegram session."""

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PipelineStepCheckpoint(Base):
    """Restorable snapshot for navigating to a previous pipeline step."""

    __tablename__ = "pipeline_step_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    generation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("generation_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    step_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    scene_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    substep: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    session_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    restored_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SceneImageCandidate(Base):
    """Persistent start/end frame candidate for one scene."""

    __tablename__ = "scene_image_candidates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    scene_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    image_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    media_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    liked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_for_video: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    scene: Mapped["Scene"] = relationship(back_populates="image_candidates")


class SceneVideoCandidate(Base):
    """Persistent rendered video candidate for one scene/language."""

    __tablename__ = "scene_video_candidates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    scene_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    video_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    media_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    liked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_for_production: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    speed_coef: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_image_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_prompt_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    speech_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    speech_cues_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    speech_plan_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    video_speech_qa_by_lang: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    generation_params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    scene: Mapped["Scene"] = relationship(back_populates="video_candidates")


class SceneMediaMessageLink(Base):
    """Maps Telegram message ids back to persistent scene candidates."""

    __tablename__ = "scene_media_message_links"
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_message_id", name="uq_scene_media_message_links_chat_message"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    scene: Mapped["Scene"] = relationship(back_populates="media_message_links")


class PublishedVideoAnalytics(Base):
    """Analytics for published short-form videos, used as feedback for future briefs."""

    __tablename__ = "published_video_analytics"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipe: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_frame_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    average_view_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_percentage_viewed: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention_3s: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention_5s: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention_10s: Mapped[float | None] = mapped_column(Float, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    follows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProjectPublishAccount(Base):
    """Project-level mapping of locale/platform to target social account or channel."""

    __tablename__ = "project_publish_accounts"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "platform",
            "language",
            "account_key",
            name="uq_project_publish_accounts_project_platform_language_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    account_key: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="auto_after_copy_approval")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    project: Mapped["Project"] = relationship(back_populates="publish_accounts")


class PublicationJob(Base):
    """Single publish-ready video package for one platform/language/account."""

    __tablename__ = "publication_jobs"
    __table_args__ = (
        UniqueConstraint(
            "bundle_key",
            "platform",
            "language",
            "account_key",
            name="uq_publication_jobs_bundle_platform_language_account",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    bundle_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(64), nullable=False, default="production_final")
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    account_key: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="draft", index=True)
    video_path: Mapped[str] = mapped_column(Text, nullable=False)
    video_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hashtags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    recipe: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="auto_after_copy_approval")
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    scheduled_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_post_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    project: Mapped["Project"] = relationship(back_populates="publication_jobs")
