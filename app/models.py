"""
ORM-модели проекта.

Сейчас БД постепенно становится source of truth для проектов, шагов пайплайна,
запусков генерации, сцен и результатов видео.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import DateTime, Uuid

from app.db import Base


class Project(Base):
    """Проект контента: набор ассетов, шагов, правил и будущих output-настроек."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
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
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    characters: Mapped[list["ProjectCharacter"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    locations: Mapped[list["ProjectLocation"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    visual_props: Mapped[list["VisualProp"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    text_steps: Mapped[list["TextStep"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Character(Base):
    """Глобальный каталог персонажей, переиспользуемых разными проектами."""

    __tablename__ = "characters"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(
        Enum("ru", "en", name="generation_language"),
        nullable=False,
    )
    script: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    scenes: Mapped[list["Scene"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    video_results: Mapped[list["VideoResult"]] = relationship(back_populates="run", cascade="all, delete-orphan")


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

    run: Mapped["GenerationRun"] = relationship(back_populates="scenes")
    video_results: Mapped[list["VideoResult"]] = relationship(back_populates="scene", cascade="all, delete-orphan")


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
