from __future__ import annotations

import base64

from django.contrib import admin
from django.http import Http404, HttpResponse
from django.urls import path, reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from .forms import ChatSessionAdminForm, SceneImageCandidateAdminForm, SceneVideoCandidateAdminForm
from .models import (
    ChatSession,
    GenerationArtifact,
    GenerationRun,
    PipelineStepCheckpoint,
    Scene,
    SceneImageCandidate,
    SceneVideoCandidate,
    VideoOverview,
)


def _short(value: str | None, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _admin_change_url(obj) -> str:
    opts = obj._meta
    return reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])


def _admin_change_link(obj, label: str | None = None) -> str:
    return format_html('<a href="{}">{}</a>', _admin_change_url(obj), label or str(obj))


def _candidate_media_url(table_name: str, candidate_id: str) -> str:
    if table_name == "scene_image_candidates":
        return reverse("admin:pipeline_admin_sceneimagecandidate_media", args=[candidate_id])
    if table_name == "scene_video_candidates":
        return reverse("admin:pipeline_admin_scenevideocandidate_media", args=[candidate_id])
    return ""


def _candidate_change_link(table_name: str, candidate_id: str) -> str:
    if table_name == "scene_image_candidates":
        row = SceneImageCandidate.objects.filter(candidate_id=candidate_id).first()
    elif table_name == "scene_video_candidates":
        row = SceneVideoCandidate.objects.filter(candidate_id=candidate_id).first()
    else:
        row = None
    if row is None:
        return ""
    return _admin_change_link(row, "edit")


def _artifact_link(uri: str, label: str | None = None) -> str:
    raw = str(uri or "")
    text = label or raw
    if raw.startswith("db://"):
        try:
            table_name, candidate_id = raw.removeprefix("db://").split("/", 1)
        except ValueError:
            return raw
        media_url = _candidate_media_url(table_name, candidate_id)
        if not media_url:
            return raw
        if table_name == "scene_image_candidates":
            changelist = reverse("admin:pipeline_admin_sceneimagecandidate_changelist")
            media_label = "open image"
        else:
            changelist = reverse("admin:pipeline_admin_scenevideocandidate_changelist")
            media_label = "open video"
        edit_link = _candidate_change_link(table_name, candidate_id)
        return format_html(
            '<a href="{}?q={}">{}</a> {} <a href="{}" target="_blank" rel="noopener">{}</a>',
            changelist,
            candidate_id,
            text,
            edit_link,
            media_url,
            media_label,
        )
    if raw.startswith(("http://", "https://", "file://")):
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', raw, text)
    return raw


def _image_preview(obj: SceneImageCandidate | None, *, large: bool = True) -> str:
    if obj is None:
        return ""
    image = base64.b64encode(bytes(obj.image_blob)).decode("ascii")
    media_url = reverse("admin:pipeline_admin_sceneimagecandidate_media", args=[obj.candidate_id])
    width = 420 if large else 180
    height = 320 if large else 120
    return format_html(
        '<figure style="display:inline-block;margin:0 12px 12px 0;vertical-align:top;">'
        '<a href="{}" target="_blank" rel="noopener">'
        '<img src="data:image/png;base64,{}" style="max-width:{}px;max-height:{}px;border-radius:4px;border:1px solid var(--border-color);">'
        "</a><figcaption><code>{}</code> {} {} {}</figcaption></figure>",
        media_url,
        image,
        width,
        height,
        obj.candidate_id,
        obj.kind,
        "approved" if obj.approved_for_video else "",
        _admin_change_link(obj, "edit"),
    )


def _video_preview(obj: SceneVideoCandidate | None) -> str:
    if obj is None:
        return ""
    media_url = reverse("admin:pipeline_admin_scenevideocandidate_media", args=[obj.candidate_id])
    return format_html(
        '<figure style="display:block;margin:0 0 16px;">'
        '<video src="{}" controls preload="metadata" style="max-width:520px;width:100%;border-radius:4px;border:1px solid var(--border-color);"></video>'
        '<figcaption><code>{}</code> lang={} speed={} {} {}</figcaption></figure>',
        media_url,
        obj.candidate_id,
        obj.language,
        obj.speed_coef,
        "approved" if obj.approved_for_production else "",
        _admin_change_link(obj, "edit"),
    )


class SceneInline(admin.TabularInline):
    model = Scene
    extra = 0
    can_delete = False
    fields = ("scene_order", "duration_sec", "short_text", "scene_link")
    readonly_fields = fields
    show_change_link = True

    @admin.display(description="Scene text")
    def short_text(self, obj: Scene) -> str:
        return _short(obj.text)

    @admin.display(description="Open")
    def scene_link(self, obj: Scene) -> str:
        if not obj.pk:
            return ""
        return _admin_change_link(obj, "open scene")


class GenerationArtifactInline(admin.TabularInline):
    model = GenerationArtifact
    extra = 0
    can_delete = False
    fields = ("label", "artifact_type", "scene", "language", "mime_type", "uri_link", "created_at")
    readonly_fields = fields
    show_change_link = True

    @admin.display(description="URI")
    def uri_link(self, obj: GenerationArtifact) -> str:
        return _artifact_link(obj.uri, "open")


@admin.register(GenerationRun)
class GenerationRunAdmin(admin.ModelAdmin):
    list_display = ("display_title", "status", "language", "created_at", "artifact_count")
    search_fields = ("human_title", "script", "id")
    list_filter = ("status", "language")
    readonly_fields = ("id", "created_at", "script")
    fields = ("id", "human_title", "status", "language", "script", "created_at")
    inlines = (SceneInline, GenerationArtifactInline)

    @admin.display(description="Title", ordering="human_title")
    def display_title(self, obj: GenerationRun) -> str:
        return obj.human_title or _short(obj.script) or str(obj.id)

    @admin.display(description="Artifacts")
    def artifact_count(self, obj: GenerationRun) -> int:
        return GenerationArtifact.objects.filter(run_id=obj.id).count()


@admin.register(VideoOverview)
class VideoOverviewAdmin(admin.ModelAdmin):
    list_display = ("display_title", "status", "language", "created_at", "scene_count", "artifact_count")
    search_fields = ("human_title", "script", "id")
    list_filter = ("status", "language")
    readonly_fields = ("id", "created_at", "script", "video_map")
    fields = ("id", "human_title", "status", "language", "created_at", "script", "video_map")

    @admin.display(description="Video")
    def display_title(self, obj: VideoOverview) -> str:
        return obj.human_title or _short(obj.script, 100) or str(obj.id)

    @admin.display(description="Scenes")
    def scene_count(self, obj: VideoOverview) -> int:
        return Scene.objects.filter(run_id=obj.id).count()

    @admin.display(description="Artifacts")
    def artifact_count(self, obj: VideoOverview) -> int:
        return GenerationArtifact.objects.filter(run_id=obj.id).count()

    @admin.display(description="Scenes and artifacts")
    def video_map(self, obj: VideoOverview) -> str:
        scenes = list(Scene.objects.filter(run_id=obj.id).order_by("scene_order"))
        artifacts = list(GenerationArtifact.objects.filter(run_id=obj.id).order_by("scene_id", "created_at"))
        by_scene: dict[str | None, list[GenerationArtifact]] = {}
        for artifact in artifacts:
            by_scene.setdefault(str(artifact.scene_id) if artifact.scene_id else None, []).append(artifact)

        parts = ['<div class="cf-video-map">']
        for scene in scenes:
            parts.append(
                '<section style="margin:0 0 18px;padding:12px;border:1px solid var(--border-color);border-radius:6px;">'
            )
            parts.append(
                f"<h3 style='margin:0 0 8px;'>Scene {scene.scene_order} "
                f"<small>{_admin_change_link(scene, 'open scene')}</small></h3>"
            )
            parts.append(f"<p style='margin:0 0 10px;'>{escape(_short(scene.text, 220))}</p>")
            scene_artifacts = by_scene.get(str(scene.id), [])
            if scene_artifacts:
                parts.append("<ul style='margin:0;padding-left:18px;'>")
                for artifact in scene_artifacts:
                    parts.append(
                        "<li>"
                        f"{escape(artifact.label)} "
                        f"<code>{escape(artifact.artifact_type)}</code> "
                        f"{_artifact_link(artifact.uri, 'open')}"
                        "</li>"
                    )
                parts.append("</ul>")
            else:
                parts.append("<p style='margin:0;color:var(--body-quiet-color);'>No scene artifacts yet.</p>")
            parts.append("</section>")

        final_artifacts = by_scene.get(None, [])
        if final_artifacts:
            parts.append("<h3>Final video files</h3><ul>")
            for artifact in final_artifacts:
                parts.append(
                    "<li>"
                    f"{escape(artifact.label)} "
                    f"<code>{escape(artifact.artifact_type)}</code> "
                    f"{_artifact_link(artifact.uri, 'open')}"
                    "</li>"
                )
            parts.append("</ul>")
        parts.append("</div>")
        return mark_safe("".join(str(part) for part in parts))


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    form = ChatSessionAdminForm
    list_display = ("chat_id", "state", "generation_run_id", "updated_at")
    search_fields = ("chat_id", "state", "generation_run_id")
    readonly_fields = ("created_at", "updated_at", "checkpoint_timeline")
    fieldsets = (
        ("Session", {"fields": ("chat_id", "state", "generation_run_id", "created_at", "updated_at")}),
        ("Pipeline text", {"fields": ("selected_concept", "viral_concept_brief", "recipe_full_text")}),
        ("Plans", {"fields": ("scenes_plan_json", "production_plan_json")}),
        (
            "Scene editor",
            {
                "description": "Edits the scene at the 0-based scene_index inside production.json_plan.",
                "fields": (
                    "scene_index",
                    "start_frame_prompt",
                    "end_frame_requested",
                    "end_frame_prompt",
                    "video_prompt_by_lang_json",
                    "speech_by_lang_json",
                    "syntax_retry_context_json",
                    "video_approvals_by_lang_json",
                ),
            },
        ),
        ("Raw session JSON", {"classes": ("collapse",), "fields": ("session_json",)}),
        ("Checkpoint timeline", {"fields": ("checkpoint_timeline",)}),
    )

    @admin.display(description="Checkpoint timeline")
    def checkpoint_timeline(self, obj: ChatSession) -> str:
        rows = PipelineStepCheckpoint.objects.filter(chat_id=obj.chat_id).order_by("-created_at")[:25]
        if not rows:
            return "No checkpoints yet."
        items = []
        for row in rows:
            restored = " restored" if row.restored_at else ""
            scene = "" if row.scene_index is None else f" scene={row.scene_index}"
            substep = "" if not row.substep else f" {row.substep}"
            lang = "" if not row.language else f" lang={row.language}"
            items.append(f"<li>{row.created_at:%Y-%m-%d %H:%M:%S} - {row.step_key}{scene}{substep}{lang}{restored}</li>")
        return format_html("<ul>{}</ul>", mark_safe("".join(items)))


@admin.register(PipelineStepCheckpoint)
class PipelineStepCheckpointAdmin(admin.ModelAdmin):
    list_display = ("chat_id", "step_key", "scene_index", "substep", "language", "created_at", "restored_at")
    list_filter = ("step_key", "substep", "language", "restored_at")
    search_fields = ("chat_id", "generation_run_id", "step_key")
    readonly_fields = ("created_at",)
    fields = (
        "chat_id",
        "generation_run_id",
        "step_key",
        "scene_index",
        "substep",
        "language",
        "session_snapshot_json",
        "payload_json",
        "restored_at",
        "created_at",
    )


@admin.register(Scene)
class SceneAdmin(admin.ModelAdmin):
    list_display = ("scene_order", "run_link", "duration_sec", "short_text")
    list_filter = ("run__language", "run__status")
    search_fields = ("text", "run__human_title", "run__script", "run__id")
    readonly_fields = ("id", "run_link", "artifacts", "images", "videos")
    fields = ("id", "run", "run_link", "scene_order", "duration_sec", "text", "artifacts", "images", "videos")
    raw_id_fields = ("run",)

    @admin.display(description="Run")
    def run_link(self, obj: Scene) -> str:
        return _admin_change_link(obj.run, "open video")

    @admin.display(description="Text")
    def short_text(self, obj: Scene) -> str:
        return _short(obj.text)

    @admin.display(description="Artifacts")
    def artifacts(self, obj: Scene) -> str:
        rows = GenerationArtifact.objects.filter(scene_id=obj.id).order_by("created_at")
        if not rows:
            return "No artifacts yet."
        items = [
            format_html("<li>{} <code>{}</code> {}</li>", row.label, row.artifact_type, _artifact_link(row.uri, "open"))
            for row in rows
        ]
        return format_html("<ul>{}</ul>", mark_safe("".join(str(item) for item in items)))

    @admin.display(description="Images")
    def images(self, obj: Scene) -> str:
        rows = SceneImageCandidate.objects.filter(scene_id=obj.id).order_by("created_at")
        if not rows:
            return "No images yet."
        return mark_safe("".join(str(_image_preview(row, large=False)) for row in rows))

    @admin.display(description="Videos")
    def videos(self, obj: Scene) -> str:
        rows = SceneVideoCandidate.objects.filter(scene_id=obj.id).order_by("created_at")
        if not rows:
            return "No videos yet."
        return mark_safe("".join(str(_video_preview(row)) for row in rows))


@admin.register(GenerationArtifact)
class GenerationArtifactAdmin(admin.ModelAdmin):
    list_display = ("label", "artifact_type", "language", "run", "scene", "uri_link", "created_at")
    list_filter = ("artifact_type", "language", "mime_type")
    search_fields = ("label", "uri", "run__human_title", "run__script")
    readonly_fields = ("created_at", "uri_link", "media_preview")
    raw_id_fields = ("run", "scene")
    fields = (
        "run",
        "scene",
        "artifact_type",
        "label",
        "uri",
        "uri_link",
        "media_preview",
        "language",
        "mime_type",
        "payload_json",
        "created_at",
    )

    @admin.display(description="URI")
    def uri_link(self, obj: GenerationArtifact) -> str:
        return _artifact_link(obj.uri)

    @admin.display(description="Preview")
    def media_preview(self, obj: GenerationArtifact) -> str:
        raw = str(obj.uri or "")
        if raw.startswith("db://scene_image_candidates/"):
            candidate_id = raw.rsplit("/", 1)[-1]
            row = SceneImageCandidate.objects.filter(candidate_id=candidate_id).first()
            return _image_preview(row) if row else "Image candidate not found."
        if raw.startswith("db://scene_video_candidates/"):
            candidate_id = raw.rsplit("/", 1)[-1]
            row = SceneVideoCandidate.objects.filter(candidate_id=candidate_id).first()
            return _video_preview(row) if row else "Video candidate not found."
        if str(obj.mime_type or "").startswith("image/") and raw.startswith(("http://", "https://", "file://")):
            return format_html('<img src="{}" style="max-width:420px;max-height:320px;border-radius:4px;">', raw)
        if str(obj.mime_type or "").startswith("video/") and raw.startswith(("http://", "https://", "file://")):
            return format_html('<video src="{}" controls preload="metadata" style="max-width:520px;width:100%;"></video>', raw)
        return "No inline preview for this artifact."


@admin.register(SceneImageCandidate)
class SceneImageCandidateAdmin(admin.ModelAdmin):
    form = SceneImageCandidateAdminForm
    list_display = ("candidate_id", "scene_id", "kind", "liked", "approved_for_video", "open_image", "created_at")
    list_filter = ("kind", "liked", "approved_for_video")
    search_fields = ("candidate_id", "scene_id")
    fields = (
        "candidate_id",
        "scene_id",
        "kind",
        "liked",
        "approved_for_video",
        "preview",
        "replacement_image",
        "created_at",
    )
    readonly_fields = ("candidate_id", "scene_id", "kind", "preview", "created_at")

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "media/<path:candidate_id>/",
                self.admin_site.admin_view(self.media_view),
                name="pipeline_admin_sceneimagecandidate_media",
            )
        ]
        return custom + urls

    def media_view(self, request, candidate_id: str):
        row = SceneImageCandidate.objects.filter(candidate_id=candidate_id).first()
        if row is None:
            raise Http404("Image candidate not found.")
        return HttpResponse(bytes(row.image_blob), content_type="image/png")

    @admin.display(description="Preview")
    def preview(self, obj: SceneImageCandidate) -> str:
        return _image_preview(obj)

    @admin.display(description="Image")
    def open_image(self, obj: SceneImageCandidate) -> str:
        media_url = reverse("admin:pipeline_admin_sceneimagecandidate_media", args=[obj.candidate_id])
        return format_html('<a href="{}" target="_blank" rel="noopener">open image</a>', media_url)


@admin.register(SceneVideoCandidate)
class SceneVideoCandidateAdmin(admin.ModelAdmin):
    form = SceneVideoCandidateAdminForm
    list_display = (
        "candidate_id",
        "scene_id",
        "language",
        "liked",
        "approved_for_production",
        "speed_coef",
        "watch",
        "created_at",
    )
    list_filter = ("language", "liked", "approved_for_production")
    search_fields = ("candidate_id", "scene_id", "source_image_candidate_id")
    fields = (
        "candidate_id",
        "scene_id",
        "language",
        "liked",
        "approved_for_production",
        "speed_coef",
        "source_image_candidate_id",
        "preview",
        "replacement_video",
        "created_at",
    )
    readonly_fields = ("candidate_id", "scene_id", "language", "source_image_candidate_id", "preview", "created_at")

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "media/<path:candidate_id>/",
                self.admin_site.admin_view(self.media_view),
                name="pipeline_admin_scenevideocandidate_media",
            )
        ]
        return custom + urls

    def media_view(self, request, candidate_id: str):
        row = SceneVideoCandidate.objects.filter(candidate_id=candidate_id).first()
        if row is None:
            raise Http404("Video candidate not found.")
        return HttpResponse(bytes(row.video_blob), content_type="video/mp4")

    @admin.display(description="Preview")
    def preview(self, obj: SceneVideoCandidate) -> str:
        return _video_preview(obj)

    @admin.display(description="Video")
    def watch(self, obj: SceneVideoCandidate) -> str:
        media_url = reverse("admin:pipeline_admin_scenevideocandidate_media", args=[obj.candidate_id])
        return format_html('<a href="{}" target="_blank" rel="noopener">open video</a>', media_url)
