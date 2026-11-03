from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
import mimetypes
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote, unquote, urlparse

from django import forms as django_forms
from django.contrib import admin, messages
from django.db import close_old_connections, connection, transaction
from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.urls import NoReverseMatch, path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from app.artifact_store import persist_post_production_artifacts
from app.db import SessionLocal
from app.final_video_delivery import (
    all_final_artifacts_delivered,
    deliver_final_artifact_to_telegram,
    trigger_final_completion_followup,
)
from app.generation_modes import (
    GENERATION_MODE_LONG,
    GENERATION_MODE_NORMAL,
    GENERATION_MODE_SESSION_KEY,
    GENERATION_MODE_SILENCE,
    normalize_generation_mode,
)
from app.guest_final_build import approved_guest_cartoon_scenes
from app.media_file_store import local_path_from_media_uri, read_media_uri_bytes
from app.post_production import (
    STORY_WORD_SYNC_PROJECT_SLUGS,
    apply_story_word_synced_subtitles,
    build_post_production,
    build_subtitled_post_production_from_upscaled_master,
    build_titled_post_production_from_subtitled_master,
    build_upscaled_post_production_from_clean_master,
    latinize_cyrillic_title,
    production_languages_for_scenes,
    selected_scene_video_fingerprint_for_language,
    subtitle_fingerprint_for_language,
)
from app.publications import (
    DEFAULT_PUBLICATION_VIDEO_VARIANT,
    PUBLICATION_VIDEO_VARIANT_LABELS,
    build_publication_targets,
    publication_artifacts_dir,
    publication_jobs_summary,
    publication_target_label,
    queue_publication_bundle,
)
from app.publication_config import project_env
from app.publish_secret_store import set_project_secrets
from app.social_publishers import _verify_vk_identity, platform_readiness
from app.social_oauth import (
    build_tiktok_authorize_url,
    build_youtube_authorize_url,
    exchange_tiktok_code,
    exchange_youtube_code,
    new_tiktok_pkce_pair,
    tiktok_user,
    youtube_channels,
)
from app.vk_oauth import build_authorize_url, exchange_authorization_code, new_pkce_pair
from app.scheduled_generation import (
    DEFAULT_TITLE_COLOR as SCHEDULED_DEFAULT_TITLE_COLOR,
    build_scheduled_generation_template_bytes,
    export_scheduled_generation_items_to_xlsx,
    import_scheduled_generation_xlsx,
    queue_scheduled_generation_item_now,
)
from app.scene_media_store import (
    merge_missing_scene_text_from_approved_candidate,
    rebuild_production_session,
    rebuild_scene_db_ids_for_session,
)
from app.series_store import advance_series_after_successful_render

from .forms import (
    PENDING_OVERLAY_TITLE_STYLE_KEY,
    ChatSessionAdminForm,
    SceneAdminForm,
    SceneImageCandidateAdminForm,
    SceneVideoCandidateAdminForm,
    _normalize_title_color,
)
from .models import (
    BotUser,
    ChatSession,
    ContentSeries,
    GenerationDraft,
    GenerationArtifact,
    GenerationRun,
    PipelineStepCheckpoint,
    Project,
    ProjectPublishAccount,
    PublicationJob,
    Scene,
    SceneImageCandidate,
    SceneTextRevision,
    SceneVideoCandidate,
    ScheduledGenerationEvent,
    ScheduledGenerationItem,
    VideoOverview,
)


@admin.register(BotUser)
class BotUserAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "normalized_username",
        "telegram_id",
        "role",
        "is_active",
        "quiet_mode_enabled",
        "quiet_mode_auto_enabled",
        "quiet_mode_auto_generation_count",
        "quiet_mode_auto_generation_target",
        "updated_at",
    )
    list_filter = ("role", "is_active", "quiet_mode_enabled", "quiet_mode_auto_enabled")
    search_fields = ("display_name", "normalized_username", "telegram_id")
    readonly_fields = ("id", "created_at", "updated_at")
    fields = (
        "id",
        "telegram_id",
        "normalized_username",
        "display_name",
        "role",
        "is_active",
        "quiet_mode_enabled",
        "quiet_mode_auto_enabled",
        "quiet_mode_auto_cycle_date",
        "quiet_mode_auto_generation_count",
        "quiet_mode_auto_generation_target",
        "created_at",
        "updated_at",
    )


FINAL_VIDEO_VARIANT_LABELS = {
    "clean": "clean",
    "upscaled_4k": "4K upscale",
    "subtitled_music": "subtitled/music",
    "titled_subtitled_music": "title/subtitled/music",
}
FINAL_VIDEO_ARTIFACT_TYPES = tuple(f"final_{key}" for key in FINAL_VIDEO_VARIANT_LABELS)
FINAL_BUILD_STATUS_ARTIFACT_TYPE = "final_build_status"
FINAL_BUILD_ACTIVE_STATUSES = {"pending", "running"}
TITLE_STYLE_OVERRIDE_ARTIFACT_TYPE = "title_overlay_style_override"
TITLE_STYLE_OVERRIDE_URI_PREFIX = "admin://title-overlay-style/"
TITLE_OVERRIDE_ARTIFACT_TYPE = "title_overlay_override"
TITLE_OVERRIDE_URI_PREFIX = "admin://title-overlay/"
TITLE_SUBTITLE_OVERRIDE_ARTIFACT_TYPE = "title_subtitle_overlay_override"
TITLE_SUBTITLE_OVERRIDE_URI_PREFIX = "admin://title-subtitle-overlay/"
OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE = "overlay_title_by_lang"
OVERLAY_TITLE_BY_LANG_URI_PREFIX = "db://generation_runs/"
OVERLAY_TITLE_BY_LANG_URI_SUFFIX = "/overlay_title_by_lang"
PUBLICATION_COPY_DRAFT_ARTIFACT_TYPE = "publication_copy_draft"
PUBLICATION_COPY_DRAFT_URI_PREFIX = "admin://publication-copy-draft/"
FINAL_BUILD_STALE_AFTER = timedelta(minutes=30)
FINAL_BUILD_HEARTBEAT_INTERVAL_SEC = 15
FINAL_BUILD_WORKER_KICK_DEBOUNCE_SEC = 10
ARCHIVED_TEST_RUN_STATUS = "archived_test"
_FINAL_BUILD_WORKER_KICKED_AT_BY_RUN: dict[str, Any] = {}
SOCIAL_COPY_FIELD_LABELS = (
    ("youtube_ru_title", "YouTube Shorts RU title"),
    ("youtube_ru_description", "YouTube Shorts RU description"),
    ("instagram_ru", "Instagram RU description"),
    ("vk_ru", "VK RU description"),
    ("tiktok_ru", "TikTok RU description"),
    ("instagram_en", "Instagram EN description"),
    ("youtube_en_title", "YouTube Shorts EN title"),
    ("youtube_en_description", "YouTube Shorts EN description"),
)
SOCIAL_COPY_FIELDS_BY_TARGET = {
    "youtube:ru": ("youtube_ru_title", "youtube_ru_description"),
    "instagram:ru": ("instagram_ru",),
    "vk:ru": ("vk_ru",),
    "tiktok:ru": ("tiktok_ru",),
    "instagram:en": ("instagram_en",),
    "youtube:en": ("youtube_en_title", "youtube_en_description"),
}


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


def _artifact_media_url(obj: GenerationArtifact) -> str:
    pk = getattr(obj, "pk", None)
    if not pk:
        return ""
    try:
        return reverse("admin:pipeline_admin_generationartifact_media", args=[pk])
    except NoReverseMatch:
        return f"/admin/pipeline_admin/generationartifact/{pk}/media/"


def _artifact_yandex_disk_link(obj: GenerationArtifact) -> str:
    payload = getattr(obj, "payload_json", None)
    if not isinstance(payload, dict):
        return ""
    yandex_disk = payload.get("yandex_disk")
    if not isinstance(yandex_disk, dict):
        return ""
    url = str(yandex_disk.get("public_url") or yandex_disk.get("web_url") or "").strip()
    if not url:
        return ""
    status = str(yandex_disk.get("status") or "").strip()
    label = "Yandex Disk" if status != "failed" else "Yandex Disk upload failed"
    return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, label)


def _generation_artifact_link(obj: GenerationArtifact, label: str | None = None) -> str:
    raw = str(obj.uri or "")
    text = label or raw
    cloud_link = _artifact_yandex_disk_link(obj)
    suffix = format_html(" {}", cloud_link) if cloud_link else ""
    if raw.startswith(("file://", "db://scene_image_candidates/", "db://scene_video_candidates/")):
        media_url = _artifact_media_url(obj)
        if media_url:
            return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>{}', media_url, text, suffix)
    return format_html("{}{}", _artifact_link(raw, text), suffix)


def _artifact_local_path(obj: GenerationArtifact) -> Path | None:
    raw = str(obj.uri or "").strip()
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        return Path(unquote(parsed.path))
    return None


def _candidate_media_bytes(obj: SceneImageCandidate | SceneVideoCandidate | None, blob_attr: str) -> bytes:
    if obj is None:
        return b""
    from_uri = read_media_uri_bytes(getattr(obj, "media_uri", None))
    if from_uri is not None:
        return from_uri
    blob = getattr(obj, blob_attr, None)
    return bytes(blob or b"")


def _candidate_media_response(
    request,
    obj: SceneImageCandidate | SceneVideoCandidate | None,
    *,
    blob_attr: str,
    default_content_type: str,
) -> HttpResponse | FileResponse:
    if obj is None:
        raise Http404("Candidate not found.")
    path_obj = local_path_from_media_uri(getattr(obj, "media_uri", None))
    content_type = getattr(obj, "mime_type", None) or default_content_type
    if path_obj is not None and path_obj.is_file():
        return _local_file_response(path_obj, content_type=content_type, request=request)
    payload = _candidate_media_bytes(obj, blob_attr)
    if not payload:
        raise Http404("Candidate media not found.")
    return HttpResponse(payload, content_type=content_type)


def _artifact_finder_link(obj: GenerationArtifact | None) -> str:
    if obj is None:
        return ""
    path_obj = _artifact_local_path(obj)
    if path_obj is None:
        return ""
    folder = path_obj.parent
    folder_uri = folder.as_uri() if folder.is_absolute() else f"file://{quote(str(folder), safe='/')}"
    return format_html(
        '<a href="{}" target="_blank" rel="noopener" title="{}">open folder in Finder</a>',
        folder_uri,
        str(folder),
    )


def _parse_http_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    raw = str(range_header or "").strip()
    if not raw.startswith("bytes=") or file_size <= 0:
        return None
    spec = raw.removeprefix("bytes=").split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    start_raw, end_raw = spec.split("-", 1)
    try:
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0:
                return None
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else file_size - 1
    except ValueError:
        return None
    if start < 0 or start >= file_size or end < start:
        return None
    return start, min(end, file_size - 1)


def _iter_file_range(path_obj: Path, start: int, end: int, chunk_size: int = 1024 * 1024):
    with path_obj.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _local_file_response(path_obj: Path, *, content_type: str, request) -> HttpResponse:
    file_size = path_obj.stat().st_size
    range_pair = _parse_http_range(request.headers.get("Range", ""), file_size)
    if range_pair is None:
        response = FileResponse(path_obj.open("rb"), content_type=content_type, filename=path_obj.name)
        response["Accept-Ranges"] = "bytes"
        return response
    start, end = range_pair
    response = StreamingHttpResponse(
        _iter_file_range(path_obj, start, end),
        status=206,
        content_type=content_type,
    )
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = str(end - start + 1)
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return response


def _image_preview(obj: SceneImageCandidate | None, *, large: bool = True) -> str:
    if obj is None:
        return ""
    media_url = reverse("admin:pipeline_admin_sceneimagecandidate_media", args=[obj.candidate_id])
    width = 420 if large else 180
    height = 320 if large else 120
    return format_html(
        '<figure style="display:inline-block;margin:0 12px 12px 0;vertical-align:top;">'
        '<a href="{}" target="_blank" rel="noopener">'
        '<img src="{}" loading="lazy" style="max-width:{}px;max-height:{}px;border-radius:4px;border:1px solid var(--border-color);">'
        "</a><figcaption><code>{}</code> {} {} {}</figcaption></figure>",
        media_url,
        media_url,
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
        '<video src="{}" controls preload="none" style="max-width:520px;width:100%;border-radius:4px;border:1px solid var(--border-color);"></video>'
        '<figcaption><code>{}</code> lang={} speed={} {} {}</figcaption></figure>',
        media_url,
        obj.candidate_id,
        obj.language,
        obj.speed_coef,
        "approved" if obj.approved_for_production else "",
        _admin_change_link(obj, "edit"),
    )


def _status_badge(value: bool, true_label: str = "approved", false_label: str = "not approved") -> str:
    color = "#1f8f3a" if value else "#b42318"
    background = "#e7f6ec" if value else "#fdeaea"
    label = true_label if value else false_label
    return format_html(
        '<span style="display:inline-block;padding:2px 7px;border-radius:10px;background:{};color:{};font-weight:600;">{}</span>',
        background,
        color,
        label,
    )


INLINE_MEDIA_JSON_KEYS = {"image_b64", "video_b64"}


def _is_inline_media_json_key(key: Any) -> bool:
    text = str(key)
    return text in INLINE_MEDIA_JSON_KEYS or text.endswith("_image_b64") or text.endswith("_video_b64")


def _strip_inline_media_payloads(value: Any):
    if isinstance(value, dict):
        return {
            str(key): _strip_inline_media_payloads(item)
            for key, item in value.items()
            if not _is_inline_media_json_key(key)
        }
    if isinstance(value, list):
        return [_strip_inline_media_payloads(item) for item in value]
    return value


def _title_from_run(run: GenerationRun) -> str:
    return " ".join(str(run.human_title or "").split())


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _default_overlay_title(run: GenerationRun) -> dict[str, str]:
    title = _title_from_run(run)
    return {"ru": title} if title else {}


def _overlay_series_title_for_admin_render(
    run: GenerationRun,
    session: dict[str, Any],
    production: dict[str, Any],
) -> dict[str, str]:
    values: dict[str, str] = {}
    for source in (production, session):
        value = source.get("series_title_by_lang") if isinstance(source, dict) else None
        if isinstance(value, dict) and any(str(item or "").strip() for item in value.values()):
            values.update({str(key): str(item) for key, item in value.items() if str(item or "").strip()})
    series = getattr(run, "series", None)
    if series is None:
        return values
    title_ru = " ".join(str(getattr(series, "title_ru", "") or "").split())
    title_en = " ".join(str(getattr(series, "title_en", "") or "").split())
    values.update({key: value for key, value in {"ru": title_ru, "en": title_en}.items() if value})
    return values


def _series_counter_by_lang_from_series(series: Any) -> dict[str, str]:
    if series is None:
        return {}
    index = getattr(series, "current_video_index", None)
    total = getattr(series, "total_videos", None)
    if not index or not total:
        return {}
    meta = getattr(series, "meta_json", None)
    meta = meta if isinstance(meta, dict) else {}
    label_ru = " ".join(str(meta.get("counter_label_ru") or "день").split())
    label_en = " ".join(str(meta.get("counter_label_en") or "day").split())
    return {
        "ru": f"{label_ru} {int(index)} из {int(total)}",
        "en": f"{label_en} {int(index)} of {int(total)}",
    }


def _overlay_series_counter_for_admin_render(
    run: GenerationRun,
    session: dict[str, Any],
    production: dict[str, Any],
) -> dict[str, str]:
    series_counter = _series_counter_by_lang_from_series(getattr(run, "series", None))
    if series_counter:
        return series_counter
    for source in (production, session):
        value = source.get("series_counter_by_lang") if isinstance(source, dict) else None
        if isinstance(value, dict) and any(str(item or "").strip() for item in value.values()):
            return {str(key): str(item) for key, item in value.items() if str(item or "").strip()}
    series = getattr(run, "series", None)
    index = getattr(run, "series_video_index", None) or (
        getattr(series, "current_video_index", None) if series is not None else None
    )
    total = getattr(run, "series_total_videos", None) or (
        getattr(series, "total_videos", None) if series is not None else None
    )
    if not index or not total:
        return {}
    return {"ru": f"день {int(index)} из {int(total)}", "en": f"day {int(index)} of {int(total)}"}


def _russian_overlay_title(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    direct = value.get("ru")
    if not isinstance(direct, str):
        direct = next(
            (
                item
                for key, item in value.items()
                if str(key or "").strip().lower().replace("_", "-").startswith("ru")
            ),
            "",
        )
    return " ".join(str(direct or "").split())


def _clean_overlay_title_by_lang(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip().lower(): " ".join(str(item or "").split())
        for key, item in value.items()
        if str(key).strip() and " ".join(str(item or "").split())
    }


def _scene_db_ids_for_run(run_id) -> dict[int, str]:
    rows = Scene.objects.filter(run_id=run_id).order_by("scene_order")
    return {idx: str(row.id) for idx, row in enumerate(rows)}


def _production_plan_for_session(session: dict[str, Any]) -> list[Any]:
    production = session.get("production") if isinstance(session, dict) else None
    plan = production.get("json_plan") if isinstance(production, dict) else None
    return plan if isinstance(plan, list) else []


def _scene_payload_from_session(session: dict[str, Any], scene: Scene) -> dict[str, Any]:
    plan = _production_plan_for_session(session)
    if not plan:
        return {}
    scene_map_raw = session.get("scene_db_ids_by_index")
    scene_map = scene_map_raw if isinstance(scene_map_raw, dict) else {}
    scene_id = str(scene.id)
    for raw_idx, raw_scene_id in scene_map.items():
        if str(raw_scene_id) != scene_id:
            continue
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(plan) and isinstance(plan[idx], dict):
            return copy.deepcopy(plan[idx])
    idx = int(scene.scene_order or 1) - 1
    if 0 <= idx < len(plan) and isinstance(plan[idx], dict):
        return copy.deepcopy(plan[idx])
    return {}


def _latest_scene_payload_for_admin(scene: Scene) -> dict[str, Any]:
    if not scene.pk:
        return {}
    chat_session = ChatSession.objects.filter(generation_run_id=scene.run_id).order_by("-updated_at").first()
    if chat_session is None:
        return {}
    return _scene_payload_from_session(copy.deepcopy(chat_session.session_json or {}), scene)


def _coerce_scene_order_value(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _renumber_scenes_for_run(run_id: Any) -> None:
    rows = list(Scene.objects.filter(run_id=run_id).order_by("scene_order", "id"))
    for idx, scene in enumerate(rows, start=1):
        if int(scene.scene_order or 0) != idx:
            Scene.objects.filter(id=scene.id).update(scene_order=idx)


def _sync_chat_sessions_for_run_scenes(
    run_id: Any,
    *,
    override_payloads_by_scene_id: dict[str, dict[str, Any]] | None = None,
) -> int:
    scenes = list(Scene.objects.filter(run_id=run_id).order_by("scene_order", "id"))
    override_payloads_by_scene_id = override_payloads_by_scene_id or {}
    updated = 0
    for chat_session in ChatSession.objects.filter(generation_run_id=run_id).order_by("updated_at"):
        session = copy.deepcopy(chat_session.session_json or {})
        production = session.get("production")
        if not isinstance(production, dict):
            production = {}
        old_plan = production.get("json_plan")
        if not isinstance(old_plan, list):
            old_plan = []
        old_map_raw = session.get("scene_db_ids_by_index")
        old_map = old_map_raw if isinstance(old_map_raw, dict) else {}

        payloads_by_id: dict[str, dict[str, Any]] = {}
        payloads_by_order: dict[int, dict[str, Any]] = {}
        for raw_idx, raw_scene_id in old_map.items():
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(old_plan) and isinstance(old_plan[idx], dict):
                payloads_by_id[str(raw_scene_id)] = copy.deepcopy(old_plan[idx])
        for idx, item in enumerate(old_plan):
            if not isinstance(item, dict):
                continue
            order = _coerce_scene_order_value(item.get("scene"), idx + 1)
            payloads_by_order.setdefault(order, copy.deepcopy(item))

        new_plan: list[dict[str, Any]] = []
        new_map: dict[int, str] = {}
        for idx, scene in enumerate(scenes, start=1):
            scene_id = str(scene.id)
            payload = (
                copy.deepcopy(override_payloads_by_scene_id.get(scene_id))
                if scene_id in override_payloads_by_scene_id
                else copy.deepcopy(payloads_by_id.get(scene_id) or payloads_by_order.get(int(scene.scene_order or idx)) or {})
            )
            if not isinstance(payload, dict):
                payload = {}
            payload["scene"] = idx
            payload["text"] = scene.text
            payload["duration_sec"] = float(scene.duration_sec)
            for key in ("start_frame_prompt", "start_frame_prompt_effective", "end_frame_prompt"):
                value = getattr(scene, key, None)
                if value:
                    payload[key] = value
                else:
                    payload.pop(key, None)
            for key in ("video_prompt_by_lang", "speech_by_lang", "syntax_prompt_by_lang", "prompt_meta_json"):
                value = getattr(scene, key, None)
                if isinstance(value, dict) and value:
                    payload[key] = value
                elif key == "speech_by_lang" and isinstance(payload.get(key), dict) and payload[key]:
                    pass
                else:
                    payload.pop(key, None)
            new_plan.append(payload)
            new_map[idx - 1] = scene_id

        production["json_plan"] = new_plan
        session["production"] = production
        session["scene_db_ids_by_index"] = new_map
        if new_plan:
            session["image_scene_index"] = min(_coerce_scene_order_value(session.get("image_scene_index"), 0), len(new_plan) - 1)
        else:
            session["image_scene_index"] = 0
        chat_session.session_json = _strip_inline_media_payloads(session)
        chat_session.generation_run_id = run_id
        chat_session.updated_at = timezone.now()
        chat_session.save(update_fields=["session_json", "generation_run_id", "updated_at"])
        updated += 1
    return updated


def _production_scenes_from_db(run: GenerationRun) -> list[dict[str, Any]]:
    scenes = list(Scene.objects.filter(run_id=run.id).order_by("scene_order"))
    scene_ids = [scene.id for scene in scenes]
    videos_by_scene: dict[str, list[SceneVideoCandidate]] = {str(scene_id): [] for scene_id in scene_ids}
    for row in (
        SceneVideoCandidate.objects.filter(scene_id__in=scene_ids)
        .only(
            "candidate_id",
            "scene_id",
            "language",
            "created_at",
            "liked",
            "approved_for_production",
            "speed_coef",
            "source_image_candidate_id",
        )
        .order_by("created_at", "candidate_id")
    ):
        videos_by_scene.setdefault(str(row.scene_id), []).append(row)

    out: list[dict[str, Any]] = []
    for scene in scenes:
        by_lang: dict[str, list[dict[str, Any]]] = {}
        for row in videos_by_scene.get(str(scene.id), []):
            lang = str(row.language or "ru").strip().lower() or "ru"
            by_lang.setdefault(lang, []).append(
                {
                    "candidate_id": row.candidate_id,
                    "language": lang,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "liked": bool(row.liked),
                    "approved_for_production": bool(row.approved_for_production),
                    "speed_coef": float(row.speed_coef),
                    "source_image_candidate_id": row.source_image_candidate_id,
                }
            )
        out.append(
            {
                "scene": int(scene.scene_order),
                "text": scene.text,
                "duration_sec": float(scene.duration_sec),
                "start_frame_prompt": scene.start_frame_prompt or "",
                "start_frame_prompt_effective": scene.start_frame_prompt_effective or "",
                "end_frame_prompt": scene.end_frame_prompt or "",
                "video_prompt_by_lang": scene.video_prompt_by_lang or {},
                "speech_by_lang": scene.speech_by_lang or {},
                "syntax_prompt_by_lang": scene.syntax_prompt_by_lang or {},
                "prompt_meta_json": scene.prompt_meta_json or {},
                "video_candidates_by_lang": by_lang,
            }
        )
    return out


def _checkpoint_session_for_admin_render(run: GenerationRun) -> dict[str, Any]:
    checkpoints = PipelineStepCheckpoint.objects.filter(generation_run_id=run.id).order_by("-created_at")
    for checkpoint in checkpoints:
        snapshot = checkpoint.session_snapshot_json
        if isinstance(snapshot, dict) and snapshot:
            return copy.deepcopy(snapshot)
    return {}


def _run_session_for_admin_render(run: GenerationRun) -> tuple[ChatSession | None, dict[str, Any], dict[str, Any]]:
    chat_session = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at").first()
    if chat_session is not None:
        session = copy.deepcopy(chat_session.session_json or {})
    else:
        session = _checkpoint_session_for_admin_render(run)
    session["generation_run_id"] = str(run.id)
    if not isinstance(session.get("scene_db_ids_by_index"), dict) or not session["scene_db_ids_by_index"]:
        session["scene_db_ids_by_index"] = rebuild_scene_db_ids_for_session(
            session,
            str(run.id),
        ) or _scene_db_ids_for_run(run.id)

    production = session.get("production")
    if not isinstance(production, dict):
        production = {}
    if not isinstance(production.get("json_plan"), list) or not production["json_plan"]:
        production["json_plan"] = _production_scenes_from_db(run)
    session["production"] = production
    rebuild_production_session(session, str(run.id), include_media_blobs=True)
    production = session.get("production") if isinstance(session.get("production"), dict) else production
    if bool(session.get("guest_mode")) and str(run.workflow_type or "") == "cartoon":
        production["json_plan"] = approved_guest_cartoon_scenes(production.get("json_plan"))
    return chat_session, session, production


def _ensure_generation_draft_for_admin_run(run: GenerationRun) -> GenerationDraft | None:
    if not run.pk or not bool(run.is_draft):
        return None
    existing = GenerationDraft.objects.filter(run_id=run.id).order_by("-created_at").first()
    if existing is not None:
        return existing
    try:
        _chat_session, session, _production = _run_session_for_admin_render(run)
    except Exception:
        session = {"generation_run_id": str(run.id)}
    session["generation_run_id"] = str(run.id)
    session["is_draft"] = True
    title = _title_from_run(run) or " ".join(str(run.script or "").split())[:180] or "Content Fabric draft"
    return GenerationDraft.objects.create(
        chat_id=None,
        run=run,
        title=title,
        stage=str(session.get("state") or "").strip() or None,
        scene_index=_safe_int(session.get("image_scene_index")),
        substep=str(session.get("scene_media_substep") or "").strip() or None,
        language=str(session.get("scene_media_language") or run.language or "").strip() or None,
        session_snapshot_json={},
        payload_json={"saved_from": "admin_generation_run_is_draft"},
        created_at=timezone.now(),
    )


ADMIN_CANDIDATE_SUBTITLE_SNAPSHOT_KEYS = (
    "speech_by_lang",
    "speech_cues_by_lang",
    "speech_plan_by_lang",
    "video_prompt_by_lang",
)
ADMIN_SCENE_DERIVED_SUBTITLE_KEYS = (
    "speech_cues_by_lang",
    "speech_plan_by_lang",
)


def _admin_final_build_scenes_from_page(run: GenerationRun, scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(scenes)
    try:
        rows = list(Scene.objects.filter(run_id=run.id).order_by("scene_order", "id"))
    except Exception:
        rows = []
    rows_by_order = {int(row.scene_order or idx): row for idx, row in enumerate(rows, start=1)}
    for idx, scene in enumerate(out, start=1):
        if not isinstance(scene, dict):
            continue
        try:
            order = int(scene.get("scene") or idx)
        except (TypeError, ValueError):
            order = idx
        row = rows_by_order.get(order)
        if row is not None:
            scene["scene"] = int(row.scene_order or order)
            scene["text"] = row.text
            scene["duration_sec"] = float(row.duration_sec)
            scene["video_prompt_by_lang"] = row.video_prompt_by_lang or {}
            scene["speech_by_lang"] = row.speech_by_lang or {}
            scene["syntax_prompt_by_lang"] = row.syntax_prompt_by_lang or {}
            scene["prompt_meta_json"] = row.prompt_meta_json or {}
            for key in ("start_frame_prompt", "start_frame_prompt_effective", "end_frame_prompt"):
                value = getattr(row, key, None)
                if value:
                    scene[key] = value
                else:
                    scene.pop(key, None)
        for key in ADMIN_SCENE_DERIVED_SUBTITLE_KEYS:
            scene.pop(key, None)
        merge_missing_scene_text_from_approved_candidate(scene)
        candidates_by_lang = scene.get("video_candidates_by_lang")
        if not isinstance(candidates_by_lang, dict):
            continue
        for candidates in candidates_by_lang.values():
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                for key in ADMIN_CANDIDATE_SUBTITLE_SNAPSHOT_KEYS:
                    candidate.pop(key, None)
    return out


def _overlay_title_for_admin_render(
    run: GenerationRun,
    session: dict[str, Any],
    production: dict[str, Any],
) -> dict[str, Any]:
    title = _title_overlay_by_lang_from_post_sources(session, production)
    title.update(_stored_overlay_title_by_lang_for_run(run))
    for source in (production, session):
        value = source.get("overlay_title_by_lang") if isinstance(source, dict) else None
        if isinstance(value, dict) and any(str(item or "").strip() for item in value.values()):
            source_title = {str(key): str(item) for key, item in value.items() if str(item or "").strip()}
            if _looks_like_default_overlay_title(source_title, run) and title:
                ru_title = _russian_overlay_title(source_title)
                if ru_title:
                    title["ru"] = ru_title
                continue
            title.update(source_title)
    title.update(_active_title_override_for_run(run))
    return title or _default_overlay_title(run)


def _overlay_subtitle_for_admin_render(
    run: GenerationRun | None,
    session: dict[str, Any],
    production: dict[str, Any],
) -> dict[str, str]:
    subtitle = _subtitle_overlay_by_lang_from_post_sources(session, production)
    for source in (production, session):
        value = source.get("overlay_subtitle_by_lang") if isinstance(source, dict) else None
        if isinstance(value, dict) and any(str(item or "").strip() for item in value.values()):
            subtitle.update({str(key): str(item) for key, item in value.items() if str(item or "").strip()})
    subtitle.update(_active_title_subtitle_override_for_run(run))
    return subtitle


def _post_production_sources(session: dict[str, Any], production: dict[str, Any]):
    for source in (production, session, session.get("production") if isinstance(session, dict) else None):
        post = source.get("post_production") if isinstance(source, dict) else None
        if isinstance(post, dict):
            yield post


def _title_overlay_by_lang_from_post_sources(
    session: dict[str, Any],
    production: dict[str, Any],
) -> dict[str, str]:
    recovered: dict[str, str] = {}
    for post in _post_production_sources(session, production):
        by_language = post.get("title_overlay_by_language")
        if not isinstance(by_language, dict):
            continue
        for language, payload in by_language.items():
            if isinstance(payload, dict):
                raw = payload.get("title")
            else:
                raw = payload
            title = " ".join(str(raw or "").split())
            if title:
                recovered[str(language).strip().lower() or "ru"] = title
    return recovered


def _subtitle_overlay_by_lang_from_post_sources(
    session: dict[str, Any],
    production: dict[str, Any],
) -> dict[str, str]:
    recovered: dict[str, str] = {}
    for post in _post_production_sources(session, production):
        by_language = post.get("subtitle_overlay_by_language")
        if not isinstance(by_language, dict):
            continue
        for language, payload in by_language.items():
            if isinstance(payload, dict):
                raw = payload.get("subtitle")
            else:
                raw = payload
            subtitle = " ".join(str(raw or "").split())
            if subtitle:
                recovered[str(language).strip().lower() or "ru"] = subtitle
    return recovered


def _looks_like_default_overlay_title(value: dict[str, Any], run: GenerationRun) -> bool:
    default = _title_from_run(run)
    if not default or not value:
        return False
    latinized = latinize_cyrillic_title(default)
    allowed = {default}
    if latinized:
        allowed.add(latinized)
    titles = {" ".join(str(item or "").split()) for item in value.values() if str(item or "").strip()}
    return bool(titles) and titles.issubset(allowed)


def _overlay_title_by_lang_artifact_uri(run_id: Any) -> str:
    return f"{OVERLAY_TITLE_BY_LANG_URI_PREFIX}{run_id}{OVERLAY_TITLE_BY_LANG_URI_SUFFIX}"


def _stored_overlay_title_by_lang_for_run(run: GenerationRun | None) -> dict[str, str]:
    if run is None:
        return {}
    try:
        artifacts = GenerationArtifact.objects.filter(
            run_id=run.id,
            artifact_type=OVERLAY_TITLE_BY_LANG_ARTIFACT_TYPE,
            uri=_overlay_title_by_lang_artifact_uri(run.id),
            language__isnull=True,
        ).order_by("-created_at", "-id")
        for artifact in artifacts:
            payload = getattr(artifact, "payload_json", None)
            if not isinstance(payload, dict):
                continue
            title_by_lang = _clean_overlay_title_by_lang(payload.get("overlay_title_by_lang"))
            if title_by_lang and not _looks_like_default_overlay_title(title_by_lang, run):
                return title_by_lang
    except Exception:
        return {}
    return {}


def _project_slug_for_admin_render(run: GenerationRun, session: dict[str, Any]) -> str | None:
    raw = str(session.get("selected_project_key") or "").strip()
    if raw:
        return raw
    project = getattr(run, "project", None)
    fallback = str(getattr(project, "slug", "") or "").strip()
    if fallback:
        return fallback
    return None


def _story_narration_for_admin_render(
    run: GenerationRun,
) -> tuple[dict[str, str], dict[str, str], dict[str, float], dict[str, dict[str, Any]]]:
    """Recover Story narration even when its original chat session is gone."""
    paths: dict[str, str] = {}
    voiceover: dict[str, str] = {}
    durations: dict[str, float] = {}
    alignments: dict[str, dict[str, Any]] = {}
    try:
        rows = GenerationArtifact.objects.filter(
            run_id=run.id,
            artifact_type="story_narration_audio",
        ).order_by("-created_at", "-id")
        for row in rows:
            language = str(row.language or "ru").strip().lower() or "ru"
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            text = " ".join(str(payload.get("voiceover_text") or "").split())
            if text and language not in voiceover:
                voiceover[language] = text
            try:
                duration = float(payload.get("duration_sec") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0 and language not in durations:
                durations[language] = duration
            alignment = payload.get("alignment")
            if isinstance(alignment, dict) and alignment and language not in alignments:
                alignments[language] = copy.deepcopy(alignment)
            if language in paths:
                continue
            local_path = local_path_from_media_uri(str(row.uri or ""))
            if local_path is not None and local_path.is_file():
                paths[language] = str(local_path)
    except Exception:
        return {}, {}, {}, {}
    return paths, voiceover, durations, alignments


def _title_override_uri(run_id: Any) -> str:
    return f"{TITLE_OVERRIDE_URI_PREFIX}{run_id}"


def _latest_title_override_payloads_for_run(run: GenerationRun) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    try:
        artifacts = GenerationArtifact.objects.filter(
            run_id=run.id,
            artifact_type=TITLE_OVERRIDE_ARTIFACT_TYPE,
            uri=_title_override_uri(run.id),
        ).order_by("-created_at")
        for artifact in artifacts:
            payload = getattr(artifact, "payload_json", None)
            if isinstance(payload, dict):
                payloads.append(copy.deepcopy(payload))
    except Exception:
        return []
    return payloads


def _active_title_override_for_run(run: GenerationRun | None) -> dict[str, str]:
    if run is None:
        return {}
    for payload in _latest_title_override_payloads_for_run(run):
        if payload.get("status") == "cleared":
            return {}
        value = _clean_overlay_title_by_lang(payload.get("overlay_title_by_lang"))
        if value:
            return value
    return {}


def _write_title_override_artifact(
    run: GenerationRun,
    *,
    overlay_title_by_lang: dict[str, Any] | None,
) -> GenerationArtifact:
    title_by_lang = _clean_overlay_title_by_lang(overlay_title_by_lang)
    payload: dict[str, Any] = {"status": "active" if title_by_lang else "cleared"}
    if title_by_lang:
        payload["overlay_title_by_lang"] = title_by_lang
    return GenerationArtifact.objects.create(
        run=run,
        artifact_type=TITLE_OVERRIDE_ARTIFACT_TYPE,
        label="Title overlay override",
        uri=_title_override_uri(run.id),
        language=None,
        mime_type="application/json",
        payload_json=payload,
        created_at=timezone.now(),
    )


def _title_subtitle_override_uri(run_id: Any) -> str:
    return f"{TITLE_SUBTITLE_OVERRIDE_URI_PREFIX}{run_id}"


def _latest_title_subtitle_override_payloads_for_run(run: GenerationRun) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    try:
        artifacts = GenerationArtifact.objects.filter(
            run_id=run.id,
            artifact_type=TITLE_SUBTITLE_OVERRIDE_ARTIFACT_TYPE,
            uri=_title_subtitle_override_uri(run.id),
        ).order_by("-created_at")
        for artifact in artifacts:
            payload = getattr(artifact, "payload_json", None)
            if isinstance(payload, dict):
                payloads.append(copy.deepcopy(payload))
    except Exception:
        return []
    return payloads


def _active_title_subtitle_override_for_run(run: GenerationRun | None) -> dict[str, str]:
    if run is None:
        return {}
    for payload in _latest_title_subtitle_override_payloads_for_run(run):
        if payload.get("status") == "cleared":
            return {}
        value = _clean_overlay_title_by_lang(payload.get("overlay_subtitle_by_lang"))
        if value:
            return value
    return {}


def _write_title_subtitle_override_artifact(
    run: GenerationRun,
    *,
    overlay_subtitle_by_lang: dict[str, Any] | None,
) -> GenerationArtifact:
    subtitle_by_lang = _clean_overlay_title_by_lang(overlay_subtitle_by_lang)
    payload: dict[str, Any] = {"status": "active" if subtitle_by_lang else "cleared"}
    if subtitle_by_lang:
        payload["overlay_subtitle_by_lang"] = subtitle_by_lang
    return GenerationArtifact.objects.create(
        run=run,
        artifact_type=TITLE_SUBTITLE_OVERRIDE_ARTIFACT_TYPE,
        label="Title subtitle overlay override",
        uri=_title_subtitle_override_uri(run.id),
        language=None,
        mime_type="application/json",
        payload_json=payload,
        created_at=timezone.now(),
    )


def _title_style_override_uri(run_id: Any) -> str:
    return f"{TITLE_STYLE_OVERRIDE_URI_PREFIX}{run_id}"


def _latest_title_style_override_payloads_for_run(run: GenerationRun) -> list[dict[str, Any]]:
    try:
        artifacts = GenerationArtifact.objects.filter(
            run_id=run.id,
            artifact_type=TITLE_STYLE_OVERRIDE_ARTIFACT_TYPE,
            uri=_title_style_override_uri(run.id),
        ).order_by("-created_at")
    except Exception:
        return []
    payloads: list[dict[str, Any]] = []
    for artifact in artifacts:
        payload = getattr(artifact, "payload_json", None)
        if isinstance(payload, dict):
            payloads.append(copy.deepcopy(payload))
    return payloads


def _active_title_style_override_for_run(run: GenerationRun | None) -> dict[str, Any]:
    if run is None:
        return {}
    for payload in _latest_title_style_override_payloads_for_run(run):
        value = payload.get("overlay_title_style")
        if isinstance(value, dict):
            return copy.deepcopy(value)
    return {}


def _pending_title_style_override_for_run(run: GenerationRun | None) -> dict[str, Any]:
    if run is None:
        return {}
    payloads = _latest_title_style_override_payloads_for_run(run)
    if not payloads or payloads[0].get("status") != "pending":
        return {}
    value = payloads[0].get(PENDING_OVERLAY_TITLE_STYLE_KEY)
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _write_title_style_override_artifact(
    run: GenerationRun,
    *,
    status: str,
    overlay_title_style: dict[str, Any] | None = None,
    pending_overlay_title_style: dict[str, Any] | None = None,
) -> GenerationArtifact:
    payload: dict[str, Any] = {"status": status}
    if isinstance(overlay_title_style, dict) and overlay_title_style:
        payload["overlay_title_style"] = copy.deepcopy(overlay_title_style)
    if isinstance(pending_overlay_title_style, dict) and pending_overlay_title_style:
        payload[PENDING_OVERLAY_TITLE_STYLE_KEY] = copy.deepcopy(pending_overlay_title_style)
    return GenerationArtifact.objects.create(
        run=run,
        artifact_type=TITLE_STYLE_OVERRIDE_ARTIFACT_TYPE,
        label="Title overlay style override",
        uri=_title_style_override_uri(run.id),
        language=None,
        mime_type="application/json",
        payload_json=payload,
        created_at=timezone.now(),
    )


def _overlay_title_style_for_admin_render(
    session: dict[str, Any],
    production: dict[str, Any],
    run: GenerationRun | None = None,
) -> dict[str, Any]:
    style: dict[str, Any] = {}
    for post in _post_production_sources(session, production):
        value = post.get("title_overlay_style")
        if isinstance(value, dict):
            style.update(copy.deepcopy(value))
    for source in (session, production):
        value = source.get("overlay_title_style") if isinstance(source, dict) else None
        if isinstance(value, dict):
            style.update(copy.deepcopy(value))
    run_title_color = _normalize_title_color(getattr(run, "title_color", "") if run is not None else "")
    if run_title_color:
        style["title_color"] = run_title_color
    raw_top = style.get("top_margin_ratio")
    try:
        top = float(raw_top)
    except (TypeError, ValueError):
        top = None
    if top is None or abs(top - 0.15) < 0.000001:
        style["top_margin_ratio"] = 0.05
    return style


def _pending_overlay_title_style_for_admin_render(
    session: dict[str, Any],
    production: dict[str, Any],
    run: GenerationRun | None = None,
) -> dict[str, Any]:
    if _normalize_title_color(getattr(run, "title_color", "") if run is not None else ""):
        return {}
    style: dict[str, Any] = {}
    for source in (session, production):
        value = source.get(PENDING_OVERLAY_TITLE_STYLE_KEY) if isinstance(source, dict) else None
        if isinstance(value, dict):
            style.update(copy.deepcopy(value))
    return style


def _apply_pending_overlay_title_style_for_build(
    session: dict[str, Any],
    production: dict[str, Any],
    run: GenerationRun | None = None,
) -> dict[str, Any]:
    style = _overlay_title_style_for_admin_render(session, production, run=run)
    pending_style = _pending_overlay_title_style_for_admin_render(session, production, run=run)
    if not pending_style:
        return style
    style.update(copy.deepcopy(pending_style))
    production["overlay_title_style"] = style
    session["overlay_title_style"] = style
    production.pop(PENDING_OVERLAY_TITLE_STYLE_KEY, None)
    session.pop(PENDING_OVERLAY_TITLE_STYLE_KEY, None)
    return style


def _stage_pending_title_color_for_run(run: GenerationRun, title_color: str) -> dict[str, Any]:
    normalized = _normalize_title_color(title_color)
    if not normalized:
        raise ValueError("Use a 6-digit hex color, for example #c72638.")
    run.title_color = normalized
    save = getattr(run, "save", None)
    if callable(save):
        save(update_fields=["title_color"])
    chat_session = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at").first()
    if chat_session is None or not isinstance(chat_session.session_json, dict):
        return {"title_color": normalized}
    session = copy.deepcopy(chat_session.session_json or {})
    production = session.get("production")
    if not isinstance(production, dict):
        production = {}
    style = _overlay_title_style_for_admin_render(session, production, run=run)
    style["title_color"] = normalized
    production["overlay_title_style"] = style
    session["overlay_title_style"] = style
    production.pop(PENDING_OVERLAY_TITLE_STYLE_KEY, None)
    session.pop(PENDING_OVERLAY_TITLE_STYLE_KEY, None)
    session["production"] = production
    chat_session.session_json = _strip_inline_media_payloads(session)
    chat_session.updated_at = timezone.now()
    chat_session.save(update_fields=["session_json", "updated_at"])
    return {"title_color": normalized}


def _style_title_color(style: dict[str, Any]) -> str:
    raw = str(style.get("title_color") or "").strip() if isinstance(style, dict) else ""
    return raw if raw.startswith("#") else f"#{raw}" if raw else "#ffffff"


def _title_color_swatch(color: str, label: str) -> str:
    value = _style_title_color({"title_color": color})
    return format_html(
        '<span style="display:inline-flex;align-items:center;gap:6px;margin-right:12px;">'
        '<span style="width:14px;height:14px;border-radius:3px;border:1px solid var(--border-color);'
        'background:{};"></span><span>{}: <code>{}</code></span></span>',
        value,
        label,
        value,
    )


def _title_color_picker_html(run: GenerationRun) -> str:
    try:
        _chat_session, session, production = _run_session_for_admin_render(run)
    except Exception:
        session, production = {}, {}
    active_style = _overlay_title_style_for_admin_render(session, production, run=run)
    pending_style = _pending_overlay_title_style_for_admin_render(session, production, run=run)
    current_color = _style_title_color(pending_style or active_style)
    active_color = _style_title_color(active_style)
    url = reverse("admin:pipeline_admin_generationrun_title_color", args=[run.pk])
    input_id = f"cf-title-color-{run.pk}"
    status_id = f"cf-title-color-status-{run.pk}"
    return format_html(
        '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">'
        '<input id="{}" type="color" value="{}" data-url="{}" '
        'style="width:52px;height:34px;padding:2px;cursor:pointer;">'
        '<code>{}</code>'
        '<span id="{}" style="color:var(--body-quiet-color);">{}</span>'
        "</div>"
        "<script>"
        "(function(){{"
        "const input=document.getElementById('{}');"
        "const status=document.getElementById('{}');"
        "if(!input||input.dataset.bound==='1')return;"
        "input.dataset.bound='1';"
        "let timer=null;"
        "function csrf(){{"
        "const field=document.querySelector('input[name=csrfmiddlewaretoken]');"
        "if(field&&field.value)return field.value;"
        "const match=document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);"
        "return match?decodeURIComponent(match[1]):'';"
        "}}"
        "function save(){{"
        "const body=new URLSearchParams();"
        "body.set('title_color',input.value);"
        "status.textContent='Saving...';"
        "fetch(input.dataset.url,{{method:'POST',headers:{{'X-CSRFToken':csrf()}},body:body}})"
        ".then(function(response){{return response.json().then(function(payload){{return {{ok:response.ok,payload:payload}};}});}})"
        ".then(function(result){{"
        "if(!result.ok||!result.payload.ok){{throw new Error(result.payload.error||'Save failed');}}"
        "status.textContent='Saved; applies on next Build/rebuild.';"
        "input.nextElementSibling.textContent=result.payload.title_color;"
        "}})"
        ".catch(function(error){{status.textContent=error.message||'Save failed';}});"
        "}}"
        "input.addEventListener('input',function(){{clearTimeout(timer);timer=setTimeout(save,250);}});"
        "input.addEventListener('change',save);"
        "}})();"
        "</script>",
        input_id,
        current_color,
        url,
        current_color,
        status_id,
        "Pending color" if pending_style else f"Active color: {active_color}",
        input_id,
        status_id,
    )


def _title_color_status_html(
    session: dict[str, Any],
    production: dict[str, Any],
    run: GenerationRun | None = None,
) -> str:
    active_style = _overlay_title_style_for_admin_render(session, production, run=run)
    pending_style = _pending_overlay_title_style_for_admin_render(session, production, run=run)
    active_color = _style_title_color(active_style)
    pending_color = _style_title_color(pending_style) if pending_style else ""
    if pending_color and pending_color.lower() != active_color.lower():
        return format_html(
            '<p style="margin:0 0 12px;">{}{}'
            '<span style="color:var(--body-quiet-color);">Pending color applies on the next Build/rebuild.</span></p>',
            _title_color_swatch(active_color, "Active title color"),
            _title_color_swatch(pending_color, "Pending title color"),
        )
    return format_html(
        '<p style="margin:0 0 12px;">{}'
        '<span style="color:var(--body-quiet-color);">Change it in this GenerationRun form; Build/rebuild renders it into video.</span></p>',
        _title_color_swatch(active_color, "Title color"),
    )


def _merge_post_production(existing: Any, update: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    for key, value in update.items():
        if key in {"renders_by_language", "subtitle_entries_by_language", "subtitle_fingerprint_by_language",
                   "title_overlay_by_language", "end_title_overlay_by_language", "subtitle_overlay_by_language", "series_overlay_by_language",
                   "speech_budget_by_language", "original_audio_preserved_by_language"}:
            current = merged.get(key)
            if not isinstance(current, dict):
                current = {}
            if isinstance(value, dict):
                for language, payload in value.items():
                    if isinstance(payload, dict) and isinstance(current.get(language), dict):
                        current[language].update(payload)
                    else:
                        current[language] = copy.deepcopy(payload)
            merged[key] = current
            continue
        if value not in (None, {}, []):
            merged[key] = copy.deepcopy(value)
    return merged


def _build_final_videos_for_run(
    run: GenerationRun,
    *,
    target_language: str | None = None,
    target_variant: str | None = None,
    target_variants: list[str] | tuple[str, ...] | set[str] | None = None,
    progress_callback: Any | None = None,
    source_clean_video_path: str | Path | None = None,
    source_upscaled_video_path: str | Path | None = None,
    source_subtitled_video_path: str | Path | None = None,
) -> dict[str, Any]:
    chat_session, session, production = _run_session_for_admin_render(run)
    scenes = production.get("json_plan") if isinstance(production, dict) else None
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("No production scenes are available for this run.")
    scenes = _admin_final_build_scenes_from_page(run, scenes)

    project_slug = _project_slug_for_admin_render(run, session)
    (
        narration_audio_path_by_lang,
        stored_voiceover_by_lang,
        stored_duration_by_lang,
        stored_alignment_by_lang,
    ) = _story_narration_for_admin_render(run)
    voiceover_by_lang = {
        "ru": str(session.get("story_voiceover_text") or stored_voiceover_by_lang.get("ru") or ""),
        "en": str(session.get("story_voiceover_text_en") or stored_voiceover_by_lang.get("en") or ""),
    }
    duration_by_lang = {
        "ru": float(session.get("story_audio_duration_sec") or stored_duration_by_lang.get("ru") or 0.0),
        "en": float(session.get("story_audio_duration_sec_en") or stored_duration_by_lang.get("en") or 0.0),
    }
    apply_story_word_synced_subtitles(
        scenes,
        project_slug=project_slug,
        voiceover_by_lang=voiceover_by_lang,
        alignment_by_lang={
            "ru": (
                session.get("story_audio_alignment")
                if isinstance(session.get("story_audio_alignment"), dict)
                and session.get("story_audio_alignment")
                else stored_alignment_by_lang.get("ru", {})
            ),
            "en": (
                session.get("story_audio_alignment_en")
                if isinstance(session.get("story_audio_alignment_en"), dict)
                and session.get("story_audio_alignment_en")
                else stored_alignment_by_lang.get("en", {})
            ),
        },
        duration_by_lang=duration_by_lang,
    )

    overlay_title_by_lang = _overlay_title_for_admin_render(run, session, production)
    if not overlay_title_by_lang:
        raise RuntimeError("Set human_title or overlay_title_by_lang before rendering the titled final master.")
    requested_variants = list(target_variants) if target_variants else ([target_variant] if target_variant else None)
    applies_title_overlay = requested_variants is None or "titled_subtitled_music" in requested_variants
    if applies_title_overlay:
        target_languages = [target_language] if target_language else production_languages_for_scenes(scenes)
        if "en" in {str(item or "").strip().lower() for item in target_languages} and not str(
            overlay_title_by_lang.get("en") or ""
        ).strip():
            raise RuntimeError("Set an explicit Title EN before rendering the English titled final master.")
    overlay_title_style = (
        _apply_pending_overlay_title_style_for_build(session, production, run=run)
        if applies_title_overlay
        else _overlay_title_style_for_admin_render(session, production, run=run)
    )
    overlay_subtitle_by_lang = _overlay_subtitle_for_admin_render(run, session, production)
    overlay_series_title_by_lang = _overlay_series_title_for_admin_render(run, session, production)
    overlay_series_counter_by_lang = _overlay_series_counter_for_admin_render(run, session, production)

    intro_video_path_by_lang: dict[str, str] = {}
    discipline_intro_path = Path(str(session.get("discipline_intro_approved_video_path") or ""))
    if project_slug == "discipline" and discipline_intro_path.is_file():
        intro_video_path_by_lang["ru"] = str(discipline_intro_path)
    is_story_workflow = (
        str(getattr(run, "workflow_type", "") or "").strip().lower() == "story"
        or any(bool(str(text or "").strip()) for text in voiceover_by_lang.values())
        or (
            str(project_slug or "").strip().lower() in STORY_WORD_SYNC_PROJECT_SLUGS
            and bool(narration_audio_path_by_lang)
        )
    )
    if (
        source_subtitled_video_path
        and target_language
        and requested_variants == ["titled_subtitled_music"]
        and not _subtitled_artifact_matches_current_subtitles(
            run,
            scenes,
            target_language,
            source_subtitled_video_path,
        )
    ):
        source_subtitled_video_path = None
    if (
        source_clean_video_path
        and target_language
        and requested_variants == ["upscaled_4k"]
    ):
        post = build_upscaled_post_production_from_clean_master(
            clean_video_path=source_clean_video_path,
            language=target_language,
            project_slug=project_slug,
            progress_callback=progress_callback,
            scene_video_fingerprint=selected_scene_video_fingerprint_for_language(scenes, target_language),
        )
    elif (
        source_upscaled_video_path
        and target_language
        and requested_variants == ["subtitled_music"]
    ):
        post = build_subtitled_post_production_from_upscaled_master(
            scenes,
            upscaled_video_path=source_upscaled_video_path,
            language=target_language,
            project_slug=project_slug,
            progress_callback=progress_callback,
            use_candidate_subtitle_snapshot=False,
            include_background_music=not is_story_workflow,
        )
    elif (
        source_subtitled_video_path
        and target_language
        and requested_variants == ["titled_subtitled_music"]
    ):
        post = build_titled_post_production_from_subtitled_master(
            scenes,
            subtitled_video_path=source_subtitled_video_path,
            language=target_language,
            project_slug=project_slug,
            overlay_title_by_lang=overlay_title_by_lang,
            overlay_subtitle_by_lang=overlay_subtitle_by_lang,
            overlay_title_style=overlay_title_style,
            overlay_series_title_by_lang=overlay_series_title_by_lang,
            overlay_series_counter_by_lang=overlay_series_counter_by_lang,
            progress_callback=progress_callback,
            use_candidate_subtitle_snapshot=False,
        )
    else:
        post = build_post_production(
            scenes,
            project_slug=project_slug,
            overlay_title_by_lang=overlay_title_by_lang,
            overlay_subtitle_by_lang=overlay_subtitle_by_lang,
            overlay_title_style=overlay_title_style,
            overlay_series_title_by_lang=overlay_series_title_by_lang,
            overlay_series_counter_by_lang=overlay_series_counter_by_lang,
            target_languages=[target_language] if target_language else None,
            target_variants=requested_variants,
            progress_callback=progress_callback,
            use_candidate_subtitle_snapshot=False,
            narration_audio_path_by_lang=narration_audio_path_by_lang or None,
            intro_video_path_by_lang=intro_video_path_by_lang or None,
            include_background_music=not is_story_workflow,
        )
    production["overlay_title_by_lang"] = overlay_title_by_lang
    ru_title = _russian_overlay_title(overlay_title_by_lang)
    if ru_title:
        run.human_title = ru_title
        run.save(update_fields=["human_title"])
    if overlay_title_style:
        production["overlay_title_style"] = overlay_title_style
    if overlay_subtitle_by_lang:
        production["overlay_subtitle_by_lang"] = overlay_subtitle_by_lang
        session["overlay_subtitle_by_lang"] = overlay_subtitle_by_lang
    else:
        production.pop("overlay_subtitle_by_lang", None)
        session.pop("overlay_subtitle_by_lang", None)
    if overlay_series_title_by_lang:
        production["series_title_by_lang"] = overlay_series_title_by_lang
    if overlay_series_counter_by_lang:
        production["series_counter_by_lang"] = overlay_series_counter_by_lang
    production["post_production"] = _strip_inline_media_payloads(
        _merge_post_production(production.get("post_production"), post)
    )
    session["production"] = production
    if getattr(run, "series", None) is not None:
        _sync_series_context_from_run(session, run)
    session["overlay_title_by_lang"] = overlay_title_by_lang
    if overlay_title_style:
        session["overlay_title_style"] = overlay_title_style
    persist_post_production_artifacts(session, post)

    if chat_session is not None:
        chat_session.session_json = _strip_inline_media_payloads(session)
        chat_session.generation_run_id = run.id
        chat_session.updated_at = timezone.now()
        chat_session.save(update_fields=["session_json", "generation_run_id", "updated_at"])
    return post


def _final_build_status_uri(run_id: Any, language: str, variant: str) -> str:
    return f"admin://final-build/{run_id}/{language}/{variant}"


def _final_build_status_label(language: str, variant: str) -> str:
    return f"Final build status {FINAL_VIDEO_VARIANT_LABELS.get(variant, variant)} ({language})"


def _set_final_build_status(
    run: GenerationRun,
    *,
    language: str,
    variant: str,
    status: str,
    message: str = "",
    artifact_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> GenerationArtifact:
    now = timezone.now()
    uri = _final_build_status_uri(run.id, language, variant)
    row = (
        GenerationArtifact.objects.filter(
            run_id=run.id,
            artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE,
            uri=uri,
            language=language,
        )
        .order_by("-created_at")
        .first()
    )
    payload = dict(row.payload_json) if row is not None and isinstance(row.payload_json, dict) else {}
    payload.update(
        {
            "status": status,
            "language": language,
            "variant": variant,
            "message": str(message or "")[:4000],
            "updated_at": now.isoformat(),
        }
    )
    if status == "pending":
        payload["enqueued_at"] = now.isoformat()
        payload.pop("started_at", None)
        payload.pop("finished_at", None)
        payload.pop("heartbeat_at", None)
        payload.pop("worker_id", None)
        payload.pop("artifact_id", None)
        payload["progress"] = _final_build_progress_payload("queued", "Queued for final_build_worker", percent=5)
    elif status == "running":
        payload.setdefault("started_at", now.isoformat())
        payload["heartbeat_at"] = now.isoformat()
        payload.pop("finished_at", None)
        payload.pop("artifact_id", None)
        payload.setdefault(
            "progress",
            _final_build_progress_payload("running", "Building final video", percent=50),
        )
    elif status in {"success", "failed"}:
        payload["finished_at"] = now.isoformat()
        payload.pop("heartbeat_at", None)
        payload["progress"] = _final_build_progress_payload(status, status, percent=100)
    if artifact_id:
        payload["artifact_id"] = artifact_id
    if extra_payload:
        payload.update({key: value for key, value in extra_payload.items() if value is not None})
    if row is None:
        row = GenerationArtifact(
            run_id=run.id,
            scene_id=None,
            artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE,
            label=_final_build_status_label(language, variant),
            uri=uri,
            language=language,
            mime_type="application/json",
            payload_json=payload,
            created_at=now,
        )
        row.save(force_insert=True)
    else:
        row.label = _final_build_status_label(language, variant)
        row.mime_type = "application/json"
        row.payload_json = payload
        row.save(update_fields=["label", "mime_type", "payload_json"])
    return row


def _latest_final_build_statuses(run_id: Any) -> dict[tuple[str, str], dict[str, Any]]:
    rows = GenerationArtifact.objects.filter(
        run_id=run_id,
        artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE,
    ).order_by("-created_at")
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        language = str(payload.get("language") or row.language or "").strip().lower()
        variant = str(payload.get("variant") or "").strip()
        if not language or not variant or (language, variant) in out:
            continue
        out[(language, variant)] = dict(payload)
    return out


def _final_build_updated_at(status: dict[str, Any]) -> Any:
    raw = status.get("updated_at") if isinstance(status, dict) else None
    if not raw:
        return None
    parsed = parse_datetime(str(raw))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _final_build_time_value(status: dict[str, Any], key: str) -> Any:
    raw = status.get(key) if isinstance(status, dict) else None
    if not raw:
        return None
    parsed = parse_datetime(str(raw))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _final_build_activity_at(status: dict[str, Any]) -> Any:
    return _final_build_time_value(status, "heartbeat_at") or _final_build_time_value(status, "updated_at")


def _final_build_status_is_stale(status: dict[str, Any]) -> bool:
    state = str((status or {}).get("status") or "").strip().lower()
    if state not in FINAL_BUILD_ACTIVE_STATUSES:
        return False
    if state == "running" and not status.get("heartbeat_at") and not status.get("worker_id"):
        return True
    activity_at = _final_build_activity_at(status or {})
    if activity_at is None:
        return False
    return timezone.now() - activity_at > FINAL_BUILD_STALE_AFTER


def _final_build_progress_payload(step: str, label: str, **extra: Any) -> dict[str, Any]:
    payload = {"step": step, "label": label}
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


FINAL_BUILD_VARIANT_STAGE_PROGRESS: dict[str, dict[str, tuple[int, str]]] = {
    "clean": {
        "normalizing": (12, "Preparing scene clips"),
        "clean_building": (35, "Building clean master"),
        "clean_ready": (72, "Clean master ready"),
        "upscale_building": (82, "Clean master ready; 4K is running"),
        "upscale_ready": (84, "Clean master ready; 4K master ready"),
        "subtitles_building": (86, "Clean master ready; subtitles/music is running"),
        "subtitles_ready": (88, "Clean master ready; subtitles/music ready"),
        "title_building": (90, "Clean master ready; title overlay is running"),
        "title_ready": (92, "Clean master ready; title overlay ready"),
        "persisting": (96, "Saving clean artifact"),
    },
    "upscaled_4k": {
        "normalizing": (8, "Waiting for clean master"),
        "clean_building": (14, "Waiting for clean master"),
        "clean_ready": (28, "Clean master ready; waiting for 4K upscale"),
        "reuse_clean": (34, "Using existing clean master"),
        "upscale_building": (52, "Upscaling to 4K"),
        "upscale_ready": (78, "4K master ready"),
        "subtitles_building": (82, "4K master ready; subtitles/music is running"),
        "subtitles_ready": (86, "4K master ready; subtitles/music ready"),
        "title_building": (88, "4K master ready; title overlay is running"),
        "title_ready": (90, "4K master ready; title overlay ready"),
        "persisting": (96, "Saving 4K artifact"),
    },
    "subtitled_music": {
        "normalizing": (6, "Waiting for clean and 4K masters"),
        "clean_building": (10, "Waiting for clean master"),
        "clean_ready": (20, "Clean master ready; waiting for 4K"),
        "upscale_building": (32, "Waiting for 4K upscale"),
        "upscale_ready": (48, "4K master ready; preparing subtitles/music"),
        "reuse_upscaled": (52, "Using existing 4K master"),
        "subtitles_building": (68, "Rendering subtitles and music"),
        "subtitles_ready": (88, "Subtitled/music master ready"),
        "title_building": (90, "Subtitled/music ready; title overlay is running"),
        "title_ready": (92, "Subtitled/music ready; title overlay ready"),
        "persisting": (96, "Saving subtitled/music artifact"),
    },
    "titled_subtitled_music": {
        "normalizing": (4, "Waiting for clean, 4K, subtitles, and title overlay"),
        "clean_building": (8, "Waiting for clean master"),
        "clean_ready": (16, "Clean master ready; waiting for 4K"),
        "upscale_building": (26, "Waiting for 4K upscale"),
        "upscale_ready": (42, "4K master ready; waiting for subtitles/music"),
        "subtitles_building": (58, "Waiting for subtitled/music master"),
        "subtitles_ready": (74, "Subtitled/music ready; waiting for title overlay"),
        "reuse_subtitled": (80, "Using existing subtitled/music master"),
        "title_building": (88, "Burning title overlay"),
        "title_ready": (94, "Title overlay master ready"),
        "persisting": (97, "Saving title/subtitled/music artifact"),
    },
}


def _final_build_stage_progress(stage: str, variant: str) -> dict[str, Any]:
    normalized_stage = str(stage or "").strip()
    normalized_variant = str(variant or "").strip()
    percent, label = FINAL_BUILD_VARIANT_STAGE_PROGRESS.get(normalized_variant, {}).get(
        normalized_stage,
        (50, "Building final video"),
    )
    return _final_build_progress_payload(
        normalized_stage or "building",
        label,
        percent=percent,
    )


def _initial_final_build_progress(variant: str) -> dict[str, Any]:
    return _final_build_stage_progress("normalizing", variant)


def _final_build_required_variants(variants: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    requested = {str(item).strip() for item in variants if str(item).strip()}
    indexes = [index for index, variant in enumerate(FINAL_VIDEO_VARIANT_LABELS) if variant in requested]
    if not indexes:
        return []
    return list(FINAL_VIDEO_VARIANT_LABELS)[: max(indexes) + 1]


def _kick_final_build_worker(run_id: Any) -> bool:
    run_key = str(run_id)
    now = timezone.now()
    last_kick = _FINAL_BUILD_WORKER_KICKED_AT_BY_RUN.get(run_key)
    if last_kick is not None and now - last_kick < timedelta(seconds=FINAL_BUILD_WORKER_KICK_DEBOUNCE_SEC):
        return False
    _FINAL_BUILD_WORKER_KICKED_AT_BY_RUN[run_key] = now

    project_root = Path(__file__).resolve().parents[2]
    admin_dir = Path(__file__).resolve().parents[1]
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "final_build_worker.log"
    worker_id = f"admin-kick-{os.getpid()}"
    cmd = [
        sys.executable,
        str(admin_dir / "manage.py"),
        "final_build_worker",
        "--once",
        f"--run-id={run_key}",
        f"--worker-id={worker_id}",
    ]
    with log_path.open("ab", buffering=0) as log_handle:
        subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return True


def _latest_final_artifact(run_id: Any, language: str, variant: str) -> GenerationArtifact | None:
    return (
        GenerationArtifact.objects.filter(
            run_id=run_id,
            artifact_type=f"final_{variant}",
            language=language,
        )
        .order_by("-created_at")
        .first()
    )


def _render_video_path_from_post(post: dict[str, Any], language: str, variant: str) -> Path | None:
    renders = post.get("renders_by_language") if isinstance(post, dict) else None
    if not isinstance(renders, dict):
        return None
    variants = renders.get(language)
    if not isinstance(variants, dict):
        return None
    payload = variants.get(variant)
    if not isinstance(payload, dict):
        return None
    raw_path = str(payload.get("video_path") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    return path if path.exists() else None


def _subtitled_artifact_matches_current_subtitles(
    run: GenerationRun,
    scenes: list[dict[str, Any]],
    language: str,
    source_path: str | Path,
) -> bool:
    artifact = _latest_final_artifact(run.id, language, "subtitled_music")
    if artifact is None:
        return False
    artifact_path = _artifact_local_path(artifact)
    if artifact_path is not None and Path(source_path).resolve() != artifact_path.resolve():
        return False
    payload = artifact.payload_json if isinstance(artifact.payload_json, dict) else {}
    if not _final_artifact_matches_current_scene_videos(artifact, scenes, language):
        return False
    stored = str(payload.get("subtitle_fingerprint") or "").strip()
    if not stored:
        return False
    return stored == subtitle_fingerprint_for_language(
        scenes,
        language,
        use_candidate_subtitle_snapshot=False,
    )


def _final_artifact_matches_current_scene_videos(
    artifact: GenerationArtifact | None,
    scenes: list[dict[str, Any]],
    language: str,
) -> bool:
    if artifact is None:
        return False
    payload = artifact.payload_json if isinstance(artifact.payload_json, dict) else {}
    stored = str(payload.get("scene_video_fingerprint") or "").strip()
    if not stored:
        return False
    current = selected_scene_video_fingerprint_for_language(scenes, language)
    return bool(current) and stored == current


def _heartbeat_final_build_job(run: GenerationRun, language: str, variant: str, worker_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(FINAL_BUILD_HEARTBEAT_INTERVAL_SEC):
        close_old_connections()
        try:
            current = _latest_final_build_statuses(run.id).get((language, variant), {})
            if str(current.get("status") or "").strip().lower() in {"success", "failed"}:
                continue
            _set_final_build_status(
                run,
                language=language,
                variant=variant,
                status="running",
                extra_payload={
                    "worker_id": worker_id,
                },
            )
        finally:
            close_old_connections()


def _heartbeat_final_build_batch(
    run: GenerationRun,
    language: str,
    variants: list[str],
    worker_id: str,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(FINAL_BUILD_HEARTBEAT_INTERVAL_SEC):
        close_old_connections()
        try:
            current_statuses = _latest_final_build_statuses(run.id)
            for variant in variants:
                current = current_statuses.get((language, variant), {})
                if str(current.get("status") or "").strip().lower() in {"success", "failed"}:
                    continue
                _set_final_build_status(
                    run,
                    language=language,
                    variant=variant,
                    status="running",
                    extra_payload={
                        "worker_id": worker_id,
                    },
                )
        finally:
            close_old_connections()


def _run_final_build_batch(
    run_id: str,
    language: str,
    variants: list[str] | tuple[str, ...] | set[str],
    *,
    worker_id: str = "manual",
) -> None:
    close_old_connections()
    normalized_variants = [
        variant
        for variant in FINAL_VIDEO_VARIANT_LABELS
        if variant in {str(item).strip() for item in variants if str(item).strip()}
    ]
    if not normalized_variants:
        return
    stop_event = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    plan_variants: list[str] = []
    try:
        run = GenerationRun.objects.get(id=run_id)
        GenerationRun.objects.filter(id=run.id).update(status="final_render_running", is_active=True)
        requested_variants = set(normalized_variants)
        required_variants = _final_build_required_variants(normalized_variants)
        try:
            _chat_session, _session, reuse_production = _run_session_for_admin_render(run)
            reuse_scenes = reuse_production.get("json_plan") if isinstance(reuse_production, dict) else None
            reuse_scenes = (
                _admin_final_build_scenes_from_page(run, reuse_scenes)
                if isinstance(reuse_scenes, list)
                else []
            )
        except Exception:
            reuse_scenes = []

        def latest_variant_path(variant: str) -> Path | None:
            artifact = _latest_final_artifact(run.id, language, variant)
            if reuse_scenes and not _final_artifact_matches_current_scene_videos(artifact, reuse_scenes, language):
                return None
            path = _artifact_local_path(artifact) if artifact is not None else None
            return path if path is not None and path.exists() else None

        latest_paths: dict[str, Path | None] = {
            variant: latest_variant_path(variant)
            for variant in FINAL_VIDEO_VARIANT_LABELS
        }
        for variant in required_variants:
            if variant in requested_variants or latest_paths.get(variant) is None:
                plan_variants.append(variant)
        if not plan_variants:
            plan_variants = normalized_variants
        delivered_all = True
        current_statuses = _latest_final_build_statuses(run.id)
        for variant in plan_variants:
            current = current_statuses.get((language, variant), {})
            attempts = int(current.get("attempt_count") or 0) + 1
            _set_final_build_status(
                run,
                language=language,
                variant=variant,
                status="running",
                extra_payload={
                    "worker_id": worker_id,
                    "attempt_count": attempts,
                    "batch_variants": plan_variants,
                    "requested_variants": normalized_variants,
                    "progress": _initial_final_build_progress(variant),
                },
            )
        heartbeat_thread = threading.Thread(
            target=_heartbeat_final_build_batch,
            args=(run, language, plan_variants, worker_id, stop_event),
            daemon=True,
            name=f"final-build-heartbeat-{run.id}-{language}-batch",
        )
        heartbeat_thread.start()

        for active_variant in plan_variants:
            source_clean_video_path = latest_paths.get("clean") if active_variant == "upscaled_4k" else None
            source_upscaled_video_path = latest_paths.get("upscaled_4k") if active_variant == "subtitled_music" else None
            source_subtitled_video_path = (
                latest_paths.get("subtitled_music") if active_variant == "titled_subtitled_music" else None
            )
            if active_variant == "upscaled_4k" and source_clean_video_path is None:
                raise RuntimeError("Cannot build 4K upscale because clean master is missing.")
            if active_variant == "subtitled_music" and source_upscaled_video_path is None:
                raise RuntimeError("Cannot build subtitled/music master because 4K master is missing.")
            def progress_callback(event: dict[str, Any], *, variant: str = active_variant) -> None:
                event_language = str(event.get("language") or "").strip().lower()
                if event_language and event_language != language:
                    return
                stage = str(event.get("stage") or "").strip()
                close_old_connections()
                try:
                    _set_final_build_status(
                        run,
                        language=language,
                        variant=variant,
                        status="running",
                        extra_payload={
                            "worker_id": worker_id,
                            "batch_variants": plan_variants,
                            "requested_variants": normalized_variants,
                            "progress": _final_build_stage_progress(stage, variant),
                        },
                    )
                finally:
                    close_old_connections()

            post = _build_final_videos_for_run(
                run,
                target_language=language,
                target_variants=[active_variant],
                progress_callback=progress_callback,
                source_clean_video_path=source_clean_video_path,
                source_upscaled_video_path=source_upscaled_video_path,
                source_subtitled_video_path=source_subtitled_video_path,
            )
            artifact = _latest_final_artifact(run.id, language, active_variant)
            latest_paths[active_variant] = _render_video_path_from_post(post, language, active_variant) or latest_variant_path(active_variant)
            _set_final_build_status(
                run,
                language=language,
                variant=active_variant,
                status="success",
                artifact_id=str(artifact.id) if artifact else None,
                extra_payload={
                    "worker_id": worker_id,
                    "batch_variants": plan_variants,
                    "requested_variants": normalized_variants,
                    "progress": _final_build_progress_payload("success", "Final video built", percent=100),
                },
            )
            if artifact is None:
                delivered_all = False
            else:
                delivery = deliver_final_artifact_to_telegram(run.id, artifact.id)
                if str(delivery.get("status") or "").lower() != "sent":
                    delivered_all = False
                    _set_final_build_status(
                        run,
                        language=language,
                        variant=active_variant,
                        status="success",
                        artifact_id=str(artifact.id),
                        message="Final video built, but Telegram delivery is not confirmed yet.",
                        extra_payload={
                            "worker_id": worker_id,
                            "batch_variants": plan_variants,
                            "requested_variants": normalized_variants,
                            "telegram_delivery": delivery,
                            "progress": _final_build_progress_payload(
                                "delivery_failed",
                                "Final video built; Telegram delivery needs retry",
                                percent=100,
                            ),
                        },
                    )
        _advance_series_after_successful_queued_final_build(run)
        GenerationRun.objects.filter(id=run.id).update(
            status="final_videos_delivered" if delivered_all else "final_video_delivery_failed",
            is_active=False,
            current_scene_index=None,
            current_media_substep=None,
            current_media_language_index=None,
        )
        trigger_final_completion_followup(run.id)
        stop_event.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
    except Exception as exc:
        stop_event.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        try:
            run = GenerationRun.objects.get(id=run_id)
            GenerationRun.objects.filter(id=run.id).update(status="final_render_failed")
            for variant in (plan_variants or normalized_variants):
                _set_final_build_status(
                    run,
                    language=language,
                    variant=variant,
                    status="failed",
                    message=str(exc),
                    extra_payload={
                        "worker_id": worker_id,
                        "batch_variants": plan_variants or normalized_variants,
                        "requested_variants": normalized_variants,
                        "progress": _final_build_progress_payload("failed", "Final build failed", percent=100),
                    },
                )
            trigger_final_completion_followup(run.id)
        except Exception:
            pass
    finally:
        close_old_connections()


def _run_final_build_job(run_id: str, language: str, variant: str, *, worker_id: str = "manual") -> None:
    _run_final_build_batch(run_id, language, [variant], worker_id=worker_id)


def _queue_final_build(run: GenerationRun, *, language: str, variant: str, kick_worker: bool = True) -> str:
    normalized_language = str(language or "").strip().lower()
    normalized_variant = str(variant or "").strip()
    if not normalized_language:
        raise RuntimeError("Choose a language for final render.")
    if normalized_variant not in FINAL_VIDEO_VARIANT_LABELS:
        raise RuntimeError(f"Unknown final render variant: {variant}")
    current = _latest_final_build_statuses(run.id).get((normalized_language, normalized_variant), {})
    if (
        str(current.get("status") or "") in FINAL_BUILD_ACTIVE_STATUSES
        and not _final_build_status_is_stale(current)
    ):
        return str(current.get("status"))
    _set_final_build_status(
        run,
        language=normalized_language,
        variant=normalized_variant,
        status="pending",
    )
    try:
        GenerationRun.objects.filter(id=run.id).update(status="final_render_pending", is_active=True)
    except Exception:
        # This admin helper is often exercised with lightweight run stubs in tests
        # or from queue paths where status artifacts are the source of truth.
        # A DB status update failure must not prevent the final worker kick.
        pass
    if kick_worker:
        _kick_final_build_worker(run.id)
    return "pending"


def _claim_next_final_build(worker_id: str, *, run_id: str | None = None) -> GenerationArtifact | None:
    now = timezone.now().isoformat()
    with transaction.atomic():
        rows = GenerationArtifact.objects.filter(artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE)
        if run_id:
            rows = rows.filter(run_id=run_id)
        if connection.features.has_select_for_update:
            rows = rows.select_for_update(skip_locked=getattr(connection.features, "has_select_for_update_skip_locked", False))
        for row in rows.order_by("created_at", "id"):
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            state = str(payload.get("status") or "").strip().lower()
            if state != "pending" and not _final_build_status_is_stale(payload):
                continue
            language = str(payload.get("language") or row.language or "").strip().lower()
            variant = str(payload.get("variant") or "").strip()
            if not language or variant not in FINAL_VIDEO_VARIANT_LABELS:
                continue
            run = GenerationRun.objects.get(id=row.run_id)
            _set_final_build_status(
                run,
                language=language,
                variant=variant,
                status="running",
                extra_payload={
                    "claimed_at": now,
                    "worker_id": worker_id,
                    "progress": _final_build_progress_payload("claimed", "Claimed by final_build_worker", percent=20),
                },
            )
            row.refresh_from_db()
            return row
    return None


def _claim_next_final_build_batch(worker_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
    with transaction.atomic():
        rows = GenerationArtifact.objects.filter(artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE)
        if run_id:
            rows = rows.filter(run_id=run_id)
        if connection.features.has_select_for_update:
            rows = rows.select_for_update(skip_locked=getattr(connection.features, "has_select_for_update_skip_locked", False))
        selected_run_id = None
        selected_language = ""
        for row in rows.order_by("created_at", "id"):
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            state = str(payload.get("status") or "").strip().lower()
            if state != "pending" and not _final_build_status_is_stale(payload):
                continue
            language = str(payload.get("language") or row.language or "").strip().lower()
            variant = str(payload.get("variant") or "").strip()
            if not language or variant not in FINAL_VIDEO_VARIANT_LABELS:
                continue
            selected_run_id = row.run_id
            selected_language = language
            break
        if selected_run_id is None:
            return []

        claimed: list[dict[str, Any]] = []
        now = timezone.now().isoformat()
        run = GenerationRun.objects.get(id=selected_run_id)
        batch_rows = GenerationArtifact.objects.filter(
            run_id=selected_run_id,
            artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE,
            language=selected_language,
        ).order_by("created_at", "id")
        if connection.features.has_select_for_update:
            batch_rows = batch_rows.select_for_update(skip_locked=getattr(connection.features, "has_select_for_update_skip_locked", False))
        for row in batch_rows:
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            state = str(payload.get("status") or "").strip().lower()
            if state != "pending" and not _final_build_status_is_stale(payload):
                continue
            variant = str(payload.get("variant") or "").strip()
            if variant not in FINAL_VIDEO_VARIANT_LABELS:
                continue
            _set_final_build_status(
                run,
                language=selected_language,
                variant=variant,
                status="running",
                extra_payload={
                    "claimed_at": now,
                    "worker_id": worker_id,
                    "progress": _final_build_progress_payload(
                        "claimed",
                        "Claimed by final_build_worker batch",
                        percent=20,
                    ),
                },
            )
            claimed.append(
                {
                    "run_id": str(selected_run_id),
                    "language": selected_language,
                    "variant": variant,
                }
            )
    return claimed


def _advance_series_after_successful_queued_final_build(run: GenerationRun) -> None:
    status_rows = GenerationArtifact.objects.filter(
        run_id=run.id,
        artifact_type=FINAL_BUILD_STATUS_ARTIFACT_TYPE,
    )
    queued_payloads = [
        row.payload_json
        for row in status_rows
        if isinstance(row.payload_json, dict) and row.payload_json.get("advance_series_after_success")
    ]
    if not queued_payloads:
        return
    if any(str(payload.get("status") or "").strip().lower() != "success" for payload in queued_payloads):
        return
    chat_session = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at").first()
    if chat_session is None or not isinstance(chat_session.session_json, dict):
        return
    session = copy.deepcopy(chat_session.session_json or {})
    before_advanced = bool(session.get("series_render_counter_advanced"))
    advanced = advance_series_after_successful_render(session)
    if advanced is None and before_advanced == bool(session.get("series_render_counter_advanced")):
        return
    chat_session.session_json = _strip_inline_media_payloads(session)
    chat_session.updated_at = timezone.now()
    chat_session.save(update_fields=["session_json", "updated_at"])


def _rendered_variants_summary(post: dict[str, Any]) -> str:
    renders = post.get("renders_by_language")
    if not isinstance(renders, dict):
        return "no render payload"
    parts: list[str] = []
    for language, variants in renders.items():
        if not isinstance(variants, dict):
            continue
        labels = [FINAL_VIDEO_VARIANT_LABELS.get(str(key), str(key)) for key in variants]
        parts.append(f"{language}: {', '.join(labels)}")
    return "; ".join(parts) or "no variants"


class PublicationQueueAdminForm(django_forms.Form):
    project_slug = django_forms.ChoiceField(required=True, label="Project / account set")
    target_keys = django_forms.MultipleChoiceField(
        required=True,
        widget=django_forms.CheckboxSelectMultiple,
        label="Publish targets",
    )
    video_variant = django_forms.ChoiceField(
        choices=tuple((key, label) for key, label in PUBLICATION_VIDEO_VARIANT_LABELS.items()),
        initial=DEFAULT_PUBLICATION_VIDEO_VARIANT,
        label="Video version",
    )
    scheduled_at = django_forms.DateTimeField(
        required=False,
        input_formats=("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"),
        widget=django_forms.DateTimeInput(attrs={"type": "datetime-local"}),
        label="Publication time",
        help_text="Leave empty to publish as soon as the job is approved by the delivery flow.",
    )

    def __init__(
        self,
        *args,
        copy: dict[str, str],
        project_choices: tuple[tuple[str, str], ...] = (),
        video_source_choices: dict[str, tuple[tuple[str, str], ...]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._video_source_choices = video_source_choices
        self.fields["project_slug"].choices = project_choices
        targets = build_publication_targets(copy)
        self.fields["target_keys"].choices = tuple((target.key, publication_target_label(target)) for target in targets)
        self.fields["target_keys"].initial = [target.key for target in targets if target.description]
        for language, language_label in (("ru", "RU"), ("en", "EN")):
            choices = list((video_source_choices or {}).get(language, ()))
            choices.append(("upload", f"Upload my own {language_label} MP4"))
            self.fields[f"video_source_{language}"] = django_forms.ChoiceField(
                required=False,
                choices=tuple(choices),
                label=f"{language_label} video source",
            )
            self.fields[f"custom_video_{language}"] = django_forms.FileField(
                required=False,
                label=f"Own {language_label} MP4",
                widget=django_forms.ClearableFileInput(attrs={"accept": "video/mp4,.mp4"}),
            )
        for key, label in SOCIAL_COPY_FIELD_LABELS:
            is_title = key.endswith("_title")
            self.fields[key] = django_forms.CharField(
                required=False,
                label=label,
                initial=copy.get(key, ""),
                widget=django_forms.Textarea(attrs={"rows": 2 if is_title else 5, "style": "width:100%;"}),
            )

    def clean(self):
        cleaned = super().clean()
        selected_targets = cleaned.get("target_keys") or []
        if not selected_targets:
            self.add_error("target_keys", "Choose at least one publish target.")
        if self._video_source_choices is not None:
            selected_languages = {str(key).split(":", 1)[1] for key in selected_targets if ":" in str(key)}
            for language in selected_languages:
                source_key = str(cleaned.get(f"video_source_{language}") or "")
                upload = cleaned.get(f"custom_video_{language}")
                if not source_key:
                    self.add_error(f"video_source_{language}", f"Choose a {language.upper()} video.")
                elif source_key == "upload" and upload is None:
                    self.add_error(f"custom_video_{language}", f"Choose an MP4 file for {language.upper()} targets.")
                if upload is not None:
                    name = str(getattr(upload, "name", "") or "").lower()
                    if not name.endswith(".mp4"):
                        self.add_error(f"custom_video_{language}", "Only .mp4 files are accepted.")
        return cleaned

    def copy_payload(self) -> dict[str, str]:
        return {key: str(self.cleaned_data.get(key) or "") for key, _label in SOCIAL_COPY_FIELD_LABELS}


class VKOAuthSetupAdminForm(django_forms.Form):
    project_slug = django_forms.ChoiceField(required=True, label="Project / account set")
    client_id = django_forms.CharField(required=True, label="VK ID App ID", max_length=64)

    def __init__(self, *args, project_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project_slug"].choices = project_choices


class YouTubeOAuthSetupAdminForm(django_forms.Form):
    project_slug = django_forms.ChoiceField(required=True, label="Project / account set")
    language = django_forms.ChoiceField(
        required=True,
        choices=(("ru", "Russian channel"), ("en", "English channel")),
        label="Destination",
    )
    client_id = django_forms.CharField(required=True, label="Google OAuth Client ID", max_length=256)
    client_secret = django_forms.CharField(
        required=True,
        label="Google OAuth Client secret",
        max_length=256,
        widget=django_forms.PasswordInput,
    )

    def __init__(self, *args, project_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project_slug"].choices = project_choices


class TikTokOAuthSetupAdminForm(django_forms.Form):
    project_slug = django_forms.ChoiceField(required=True, label="Project / account set")
    client_key = django_forms.CharField(required=True, label="TikTok Client key", max_length=256)
    client_secret = django_forms.CharField(
        required=True,
        label="TikTok Client secret",
        max_length=256,
        widget=django_forms.PasswordInput,
    )

    def __init__(self, *args, project_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project_slug"].choices = project_choices


def _publication_project_choices() -> tuple[tuple[str, str], ...]:
    try:
        projects = Project.objects.filter(is_active=True).exclude(slug__startswith="guest-").order_by("title", "slug")
        return tuple((project.slug, f"{project.title} ({project.slug})") for project in projects)
    except Exception:
        return ()


def _publication_all_final_artifacts(run: GenerationRun) -> list[GenerationArtifact]:
    try:
        return list(
            GenerationArtifact.objects.filter(run_id=run.id, artifact_type__in=FINAL_VIDEO_ARTIFACT_TYPES)
            .order_by("language", "-created_at", "artifact_type")
        )
    except Exception:
        return []


def _publication_artifact_video_payload(artifact: GenerationArtifact) -> dict[str, str]:
    raw = str(artifact.uri or "").strip()
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        path = Path(unquote(parsed.path))
        return {"video_path": str(path)} if path.is_file() else {}
    path = Path(raw) if raw and not raw.startswith(("http://", "https://")) else None
    if path is not None and path.is_file():
        return {"video_path": str(path)}
    return {}


def _publication_video_source_choices(run: GenerationRun) -> dict[str, tuple[tuple[str, str], ...]]:
    choices: dict[str, list[tuple[str, str]]] = {"ru": [], "en": []}
    for artifact in _publication_all_final_artifacts(run):
        language = str(artifact.language or "").strip().lower()
        if language not in choices or not _publication_artifact_video_payload(artifact):
            continue
        variant = str(artifact.artifact_type or "").removeprefix("final_")
        label = PUBLICATION_VIDEO_VARIANT_LABELS.get(variant, variant)
        created = artifact.created_at.strftime("%Y-%m-%d %H:%M:%S") if artifact.created_at else "unknown time"
        choices[language].append((f"artifact:{artifact.id}", f"{created} · {label} · {artifact.label}"))
    return {language: tuple(rows) for language, rows in choices.items()}


def _publication_video_source_initials(
    choices: dict[str, tuple[tuple[str, str], ...]],
) -> dict[str, str]:
    out: dict[str, str] = {}
    preferred_label = PUBLICATION_VIDEO_VARIANT_LABELS[DEFAULT_PUBLICATION_VIDEO_VARIANT].lower()
    for language, rows in choices.items():
        selected = next((value for value, label in rows if preferred_label in label.lower()), None)
        out[f"video_source_{language}"] = selected or (rows[0][0] if rows else "upload")
    return out


def _store_publication_custom_upload(upload, *, run_id: Any, language: str) -> Path:
    target_dir = publication_artifacts_dir() / "admin_uploads" / str(run_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}_{language}.mp4"
    with target.open("wb") as handle:
        for chunk in upload.chunks():
            handle.write(chunk)
    return target


def _publication_post_production_from_form(
    run: GenerationRun,
    form: PublicationQueueAdminForm,
) -> tuple[dict[str, Any], dict[str, str], list[Path]]:
    artifacts = {f"artifact:{row.id}": row for row in _publication_all_final_artifacts(run)}
    selected_languages = {
        str(key).split(":", 1)[1]
        for key in form.cleaned_data.get("target_keys", [])
        if ":" in str(key)
    }
    renders: dict[str, dict[str, dict[str, str]]] = {}
    labels: dict[str, str] = {}
    staging_paths: list[Path] = []
    for language in selected_languages:
        source_key = str(form.cleaned_data.get(f"video_source_{language}") or "")
        if source_key == "upload":
            path = _store_publication_custom_upload(
                form.cleaned_data[f"custom_video_{language}"],
                run_id=run.id,
                language=language,
            )
            staging_paths.append(path)
            payload = {"video_path": str(path)}
            labels[language] = f"{language.upper()} · custom upload · {path.name}"
        else:
            artifact = artifacts.get(source_key)
            if artifact is None:
                raise ValueError(f"Selected {language.upper()} artifact is no longer available.")
            payload = _publication_artifact_video_payload(artifact)
            if not payload:
                raise ValueError(f"Selected {language.upper()} artifact file is unavailable.")
            created = artifact.created_at.strftime("%Y-%m-%d %H:%M:%S") if artifact.created_at else "unknown time"
            labels[language] = f"{language.upper()} · {artifact.label} · {created}"
        renders[language] = {"clean": payload}
    return {"renders_by_language": renders}, labels, staging_paths


def _latest_chat_session_for_run(run: GenerationRun) -> tuple[ChatSession | None, dict[str, Any]]:
    row = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at").first()
    session = row.session_json if row is not None and isinstance(row.session_json, dict) else {}
    return row, session


def _publication_copy_from_latest_jobs(run: GenerationRun) -> dict[str, str]:
    try:
        recent_jobs = list(PublicationJob.objects.order_by("-created_at")[:500])
    except Exception:
        return {}
    run_jobs = [
        job for job in recent_jobs
        if isinstance(job.payload_json, dict) and str(job.payload_json.get("run_id") or "") == str(run.id)
    ]
    if not run_jobs:
        return {}
    latest_bundle = str(run_jobs[0].bundle_key or "")
    if latest_bundle:
        run_jobs = [job for job in run_jobs if str(job.bundle_key or "") == latest_bundle]
    copy_payload = {key: "" for key, _label in SOCIAL_COPY_FIELD_LABELS}
    for job in run_jobs:
        platform = str(job.platform or "").strip().lower()
        language = str(job.language or "").strip().lower()
        description = str(job.description or "")
        title = str(job.title or "")
        if platform == "youtube" and language == "ru":
            copy_payload["youtube_ru_title"] = title
            copy_payload["youtube_ru_description"] = description
        elif platform == "instagram" and language == "ru":
            copy_payload["instagram_ru"] = description
        elif platform == "vk" and language == "ru":
            copy_payload["vk_ru"] = description
        elif platform == "tiktok" and language == "ru":
            copy_payload["tiktok_ru"] = description
        elif platform == "instagram" and language == "en":
            copy_payload["instagram_en"] = description
        elif platform == "youtube" and language == "en":
            copy_payload["youtube_en_title"] = title
            copy_payload["youtube_en_description"] = description
    return copy_payload if any(str(value or "").strip() for value in copy_payload.values()) else {}


def _publication_copy_draft_uri(run_id: Any) -> str:
    return f"{PUBLICATION_COPY_DRAFT_URI_PREFIX}{run_id}"


def _publication_copy_draft_payload(run: GenerationRun) -> dict[str, Any]:
    try:
        artifact = (
            GenerationArtifact.objects.filter(
                run_id=run.id,
                artifact_type=PUBLICATION_COPY_DRAFT_ARTIFACT_TYPE,
                uri=_publication_copy_draft_uri(run.id),
            )
            .order_by("-created_at")
            .first()
        )
    except Exception:
        return {}
    payload = artifact.payload_json if artifact is not None and isinstance(artifact.payload_json, dict) else {}
    return copy.deepcopy(payload) if isinstance(payload, dict) else {}


def _publication_copy_from_draft(run: GenerationRun) -> dict[str, str]:
    payload = _publication_copy_draft_payload(run)
    copy_payload = payload.get("copy")
    if not isinstance(copy_payload, dict):
        return {}
    out = {
        key: str(copy_payload.get(key) or "")
        for key, _label in SOCIAL_COPY_FIELD_LABELS
    }
    return out if any(str(value or "").strip() for value in out.values()) else {}


def _publication_recipe_from_draft(run: GenerationRun) -> str:
    payload = _publication_copy_draft_payload(run)
    return str(payload.get("recipe") or "").strip()


def _save_publication_copy_draft(
    run: GenerationRun,
    *,
    copy_payload: dict[str, str],
    recipe: str = "",
    feedback: str = "",
) -> None:
    clean_copy = {
        key: str(copy_payload.get(key) or "")
        for key, _label in SOCIAL_COPY_FIELD_LABELS
    }
    if not any(value.strip() for value in clean_copy.values()):
        return
    payload = {
        "copy": clean_copy,
        "recipe": str(recipe or "").strip(),
        "feedback": str(feedback or "").strip(),
        "updated_at": timezone.now().isoformat(),
    }
    GenerationArtifact.objects.create(
        run_id=run.id,
        scene_id=None,
        artifact_type=PUBLICATION_COPY_DRAFT_ARTIFACT_TYPE,
        label="Publication copy draft",
        uri=_publication_copy_draft_uri(run.id),
        language=None,
        mime_type="application/json",
        payload_json=payload,
        created_at=timezone.now(),
    )


def _publication_copy_from_run(run: GenerationRun) -> dict[str, str]:
    _row, session = _latest_chat_session_for_run(run)
    pending = session.get("social_descriptions_pending")
    if isinstance(pending, dict) and isinstance(pending.get("copy"), dict):
        return {key: str(pending["copy"].get(key) or "") for key, _label in SOCIAL_COPY_FIELD_LABELS}
    draft_copy = _publication_copy_from_draft(run)
    if draft_copy:
        return draft_copy
    latest_job_copy = _publication_copy_from_latest_jobs(run)
    if latest_job_copy:
        return latest_job_copy
    title = _title_from_run(run) or _short(run.script, 80)
    description = str(run.script or "").strip()
    return {
        "youtube_ru_title": title,
        "youtube_ru_description": description,
        "instagram_ru": description,
        "vk_ru": description,
        "tiktok_ru": description,
        "instagram_en": description,
        "youtube_en_title": title,
        "youtube_en_description": description,
    }


def _looks_like_url_text(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(("http://", "https://"))


def _publication_recipe_candidates_for_run(run: GenerationRun) -> list[str]:
    candidates: list[str] = []
    try:
        _row, session = _latest_chat_session_for_run(run)
    except Exception:
        session = {}
    if isinstance(session, dict):
        for key in ("recipe_full_text", "approved_recipe_text", "user_input"):
            value = str(session.get(key) or "").strip()
            if value:
                candidates.append(value)
    try:
        drafts = GenerationDraft.objects.filter(run_id=run.id).order_by("-created_at")[:20]
        for draft in drafts:
            snapshot = draft.session_snapshot_json if isinstance(draft.session_snapshot_json, dict) else {}
            payload = draft.payload_json if isinstance(draft.payload_json, dict) else {}
            for source in (snapshot, payload):
                for key in ("recipe_full_text", "approved_recipe_text", "user_input", "recipe"):
                    value = str(source.get(key) or "").strip()
                    if value:
                        candidates.append(value)
    except Exception:
        pass
    scheduled_item = getattr(run, "scheduled_item", None)
    if scheduled_item is not None:
        value = str(getattr(scheduled_item, "recipe", "") or "").strip()
        if value:
            candidates.append(value)
    script = str(getattr(run, "script", "") or "").strip()
    if script:
        candidates.append(script)
    out: list[str] = []
    for value in candidates:
        if value and value not in out:
            out.append(value)
    return out


def _publication_recipe_for_run(run: GenerationRun) -> str:
    candidates = _publication_recipe_candidates_for_run(run)
    for value in candidates:
        if value and not _looks_like_url_text(value):
            return value
    url = next((value for value in candidates if _looks_like_url_text(value)), "")
    if url:
        try:
            from app.handlers.handlers_generation_utils import fetch_recipe_text_from_url
            from app.pipeline import generate_recipe_from_source_text

            source_text = fetch_recipe_text_from_url(url, timeout_sec=10, max_chars=10000)
            return asyncio.run(generate_recipe_from_source_text(source_text, source_url=url))
        except Exception:
            try:
                from app.handlers.handlers_generation_utils import fetch_recipe_text_from_url

                return fetch_recipe_text_from_url(url, timeout_sec=10, max_chars=10000)
            except Exception:
                return url
    return ""


def _generate_publication_copy_for_admin(
    run: GenerationRun,
    *,
    current_copy: dict[str, str],
    recipe: str | None = None,
    feedback: str = "",
) -> dict[str, str]:
    recipe_text = str(recipe if recipe is not None else _publication_recipe_for_run(run)).strip()
    if not recipe_text:
        raise ValueError("Generation run has no recipe/script text for description generation.")
    from app.handlers.handlers_state_media import (
        _generate_social_copy_from_recipe,
        _refine_social_copy_from_feedback,
    )

    base_copy = {
        key: str(current_copy.get(key) or "")
        for key, _label in SOCIAL_COPY_FIELD_LABELS
    }
    clean_feedback = str(feedback or "").strip()
    if not clean_feedback:
        return asyncio.run(_generate_social_copy_from_recipe(recipe_text))
    has_existing_copy = any(str(value or "").strip() for value in base_copy.values())
    if not has_existing_copy:
        base_copy = asyncio.run(_generate_social_copy_from_recipe(recipe_text))
    return asyncio.run(
        _refine_social_copy_from_feedback(
            recipe=recipe_text,
            previous_copy=base_copy,
            feedback=clean_feedback,
        )
    )


def _project_slug_for_publication_run(run: GenerationRun) -> str | None:
    try:
        if run.project_id and run.project:
            slug = str(run.project.slug or "").strip()
            if slug:
                return slug
    except Exception:
        pass
    try:
        _row, session = _latest_chat_session_for_run(run)
    except Exception:
        session = {}
    slug = str(session.get("selected_project_key") or "").strip()
    return slug or None


def _publication_post_production_from_final_artifacts(run: GenerationRun) -> dict[str, Any]:
    renders: dict[str, dict[str, dict[str, str]]] = {}
    for artifact in _final_artifacts_for_run(run.id):
        language = str(artifact.language or "").strip().lower()
        variant = str(artifact.artifact_type or "").removeprefix("final_")
        if not language or variant not in PUBLICATION_VIDEO_VARIANT_LABELS:
            continue
        raw = str(artifact.uri or "").strip()
        payload: dict[str, str] = {}
        if raw.startswith("file://"):
            parsed = urlparse(raw)
            payload["video_path"] = unquote(parsed.path)
        elif raw and Path(raw).exists():
            payload["video_path"] = raw
        elif raw.startswith(("http://", "https://")):
            payload["video_url"] = raw
        if payload:
            renders.setdefault(language, {})[variant] = payload
    return {"renders_by_language": renders}


def _publication_post_production_has_video(post: dict[str, Any]) -> bool:
    renders = post.get("renders_by_language") if isinstance(post, dict) else None
    if not isinstance(renders, dict):
        return False
    for variants in renders.values():
        if isinstance(variants, dict) and any(isinstance(payload, dict) and payload for payload in variants.values()):
            return True
    return False


def _publication_project_for_run(run: GenerationRun) -> Project | None:
    slug = _project_slug_for_publication_run(run)
    if slug:
        project = Project.objects.filter(slug=slug).first()
        if project is not None:
            return project
    return Project.objects.filter(is_default=True).first()


def _publication_account_index(
    run: GenerationRun,
    project_slug: str | None = None,
) -> dict[tuple[str, str], ProjectPublishAccount]:
    project = Project.objects.filter(slug=project_slug).first() if project_slug else _publication_project_for_run(run)
    if project is None:
        return {}
    rows = ProjectPublishAccount.objects.filter(project_id=project.id, is_active=True)
    return {(row.platform, row.language): row for row in rows}


def _publication_queue_form_html(
    form: PublicationQueueAdminForm,
    run: GenerationRun,
    copy_payload: dict[str, str],
    *,
    recipe: str = "",
    generation_feedback: str = "",
) -> str:
    targets = build_publication_targets(copy_payload)
    selected_raw = form["target_keys"].value() or []
    selected = set(selected_raw if isinstance(selected_raw, list) else [selected_raw])
    project_slug = str(form["project_slug"].value() or _project_slug_for_publication_run(run) or "")
    accounts = _publication_account_index(run, project_slug) if project_slug else _publication_account_index(run)
    css = """
    <style>
      .cf-pub-grid{display:grid;grid-template-columns:minmax(360px,1.2fr) minmax(320px,.8fr);gap:18px;align-items:start}
      .cf-pub-card{border:1px solid var(--border-color);border-radius:6px;padding:14px;background:var(--body-bg)}
      .cf-pub-card h2{margin:0 0 10px;font-size:16px}
      .cf-pub-target{display:grid;grid-template-columns:28px 1fr;gap:10px;padding:14px 0;border-top:1px solid var(--border-color)}
      .cf-pub-target:first-of-type{border-top:0}
      .cf-pub-target>input{width:18px;height:18px}
      .cf-pub-target-head{display:flex;justify-content:space-between;gap:12px;align-items:start}
      .cf-pub-target-copy p{margin:10px 0 0}
      .cf-pub-muted{color:var(--body-quiet-color)}
      .cf-pub-badge{display:inline-block;padding:2px 7px;border-radius:10px;font-weight:600;font-size:12px}
      .cf-pub-ok{background:#dcfce7;color:#166534}
      .cf-pub-warn{background:#fff4d8;color:#7a4b00}
      .cf-pub-bad{background:#fee2e2;color:#991b1b}
      .cf-pub-fields textarea{font-family:var(--font-family-primary);min-height:76px}
      .cf-pub-fields label{font-weight:600}
      @media(max-width:900px){.cf-pub-grid{grid-template-columns:1fr}}
    </style>
    """
    target_rows = []
    for target in targets:
        account = accounts.get((target.platform, target.language))
        selected_source = str(form[f"video_source_{target.language}"].value() or "")
        video_ready = bool(selected_source)
        desc_ready = bool(target.description)
        account_ready = bool(account and account.external_account_id)
        api_ready = platform_readiness(project_slug, target.platform, target.language) if project_slug else None
        badges = [
            ('<span class="cf-pub-badge cf-pub-ok">description</span>' if desc_ready else '<span class="cf-pub-badge cf-pub-bad">no description</span>'),
            ('<span class="cf-pub-badge cf-pub-ok">video</span>' if video_ready else '<span class="cf-pub-badge cf-pub-bad">no video</span>'),
            ('<span class="cf-pub-badge cf-pub-ok">account</span>' if account_ready else '<span class="cf-pub-badge cf-pub-warn">manual account</span>'),
            ('<span class="cf-pub-badge cf-pub-ok">direct API</span>' if api_ready and api_ready.ready else '<span class="cf-pub-badge cf-pub-warn">API setup needed</span>'),
        ]
        title_line = escape(target.title or "No separate title")
        account_line = escape(account.external_account_id if account and account.external_account_id else "no active account mapping")
        readiness_line = ""
        if api_ready and not api_ready.ready:
            readiness_line = escape(
                ("Missing: " + ", ".join(api_ready.missing))
                if api_ready.missing
                else (api_ready.note or "Direct API is not available")
            )
        checked = " checked" if target.key in selected else ""
        copy_fields = "".join(
            f"<p>{form[key].label_tag()}{form[key]}</p>"
            for key in SOCIAL_COPY_FIELDS_BY_TARGET.get(target.key, ())
        )
        target_rows.append(
            f'<div class="cf-pub-target">'
            f'<input type="checkbox" name="target_keys" value="{escape(target.key)}"{checked}>'
            f'<div><div class="cf-pub-target-head"><span><strong>{escape(publication_target_label(target))}</strong><br>'
            f'<span class="cf-pub-muted">{escape(target.language.upper())} · {title_line}<br>{account_line}'
            f'{"<br>" + readiness_line if readiness_line else ""}</span></span>'
            f'<span>{" ".join(badges)}</span></div>'
            f'<div class="cf-pub-target-copy">{copy_fields}</div></div></div>'
        )
    video_fields = []
    for language in ("ru", "en"):
        video_fields.append(
            f'<div class="cf-pub-card"><h2>{language.upper()} video</h2>'
            f'<p>{form[f"video_source_{language}"].label_tag()}{form[f"video_source_{language}"]}</p>'
            f'<p>{form[f"custom_video_{language}"].label_tag()}{form[f"custom_video_{language}"]}</p>'
            '<p class="cf-pub-muted">Every historical final build is listed. Choose “Upload my own” for an external MP4.</p></div>'
        )
    error_html = f'<div class="errornote">{escape(form.errors.as_text())}</div>' if form.errors else ""
    return mark_safe(
        css
        + error_html
        + '<section class="cf-pub-card"><h2>Project and its accounts</h2>'
        + f'<p>{form["project_slug"].label_tag()}{form["project_slug"]}</p>'
        + '<p class="cf-pub-muted">Required for generated and custom videos. Tokens and channel IDs are isolated by project.</p></section>'
        + '<div class="cf-pub-grid" style="margin-top:18px;">'
        + "".join(video_fields)
        + '</div>'
        + '<div class="cf-pub-grid">'
        + '<section class="cf-pub-card cf-pub-fields" style="margin-top:18px;"><h2>Where each description goes</h2>'
        + "".join(target_rows)
        + '</section><section class="cf-pub-card" style="margin-top:18px;"><h2>Delivery</h2>'
        + f'<p>{form["scheduled_at"].label_tag()}{form["scheduled_at"]}<br><span class="cf-pub-muted">{escape(form.fields["scheduled_at"].help_text)}</span></p>'
        + '<p class="cf-pub-muted">Ready official APIs start immediately after this form is submitted. A future time stays scheduled. Missing credentials remain visible as manual action.</p>'
        + '</section></div>'
        + '<section class="cf-pub-card" style="margin-top:18px;"><h2>Recipe</h2>'
        + f'<textarea name="publication_recipe" rows="8" style="width:100%;">{escape(recipe)}</textarea>'
        + '<p class="cf-pub-muted">This recipe is the base text sent to description generation and saved into publication jobs.</p>'
        + '</section>'
        + '<section class="cf-pub-card" style="margin-top:18px;"><h2>Generate descriptions</h2>'
        + '<p><label for="id_generation_feedback">Feedback</label>'
        + f'<textarea id="id_generation_feedback" name="generation_feedback" rows="3" style="width:100%;">{escape(generation_feedback)}</textarea></p>'
        + '<p><button type="submit" class="button" name="generate_descriptions" value="1">Generate descriptions</button></p>'
        + '<p class="cf-pub-muted">Leave feedback empty to generate descriptions from the recipe using the same flow as the bot.</p>'
        + '</section>'
        + '<input type="hidden" name="video_variant" value="clean">'
    )


def _publication_prep_panel(run: GenerationRun) -> str:
    copy_payload = _publication_copy_from_run(run)
    targets = build_publication_targets(copy_payload)
    artifacts = _final_artifacts_for_run(run.id)
    try:
        recent_jobs = PublicationJob.objects.order_by("-created_at")[:100]
        jobs = [
            job for job in recent_jobs
            if isinstance(job.payload_json, dict) and str(job.payload_json.get("run_id") or "") == str(run.id)
        ][:10]
    except Exception:
        jobs = []
    parts = ['<div class="cf-publication-prep">']
    parts.append(
        format_html(
            '<p style="margin:0 0 12px;"><a class="button" href="{}">Queue publications / publish or upload video</a></p>',
            reverse("admin:pipeline_admin_generationrun_queue_publications", args=[run.pk]),
        )
    )
    if not artifacts:
        parts.append('<p style="margin:0 0 12px;color:var(--body-quiet-color);">No final video artifacts yet.</p>')
    parts.append("<p style='margin:0 0 6px;'><strong>Targets from current copy</strong></p><ul style='margin-top:0;'>")
    for target in targets:
        parts.append(
            format_html(
                "<li>{} - {} {}</li>",
                publication_target_label(target),
                target.video_label,
                "ready" if target.description else "missing description",
            )
        )
    parts.append("</ul>")
    if jobs:
        parts.append("<p style='margin:8px 0 6px;'><strong>Recent publication jobs</strong></p><ul style='margin-top:0;'>")
        for job in jobs:
            parts.append(
                format_html(
                    "<li>{} <code>{}</code> {} {}</li>",
                    job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
                    f"{job.platform}:{job.language}",
                    job.status,
                    _admin_change_link(job, "open"),
                )
            )
        parts.append("</ul>")
    parts.append("</div>")
    return mark_safe("".join(str(part) for part in parts))


@admin.action(description="Build final title + 4 final video versions")
def build_final_title_and_videos(modeladmin, request, queryset):
    queued = 0
    queued_runs: set[Any] = set()
    for selected in queryset:
        if isinstance(selected, ChatSession):
            if not selected.generation_run_id:
                modeladmin.message_user(
                    request,
                    f"{selected}: no generation_run_id; cannot render final videos.",
                    level=messages.ERROR,
                )
                continue
            run = GenerationRun.objects.filter(id=selected.generation_run_id).first()
            if run is None:
                modeladmin.message_user(
                    request,
                    f"{selected}: generation run {selected.generation_run_id} not found.",
                    level=messages.ERROR,
                )
                continue
        else:
            run = selected
        artifacts = _final_artifacts_for_run(run.id)
        statuses = _latest_final_build_statuses(run.id)
        languages = _final_target_languages_for_run(run, artifacts, statuses)
        if not languages:
            modeladmin.message_user(request, f"{run}: no renderable languages found.", level=messages.ERROR)
            continue
        for language in languages:
            for variant in FINAL_VIDEO_VARIANT_LABELS:
                try:
                    _queue_final_build(run, language=language, variant=variant, kick_worker=False)
                except Exception as exc:
                    modeladmin.message_user(
                        request,
                        f"{run}: final build was not queued for {language}/{variant}: {exc}",
                        level=messages.ERROR,
                    )
                    continue
                queued += 1
                queued_runs.add(run.id)
    for run_id in queued_runs:
        _kick_final_build_worker(run_id)
    if queued:
        modeladmin.message_user(request, f"Queued {queued} final render job(s). Refresh run page for status.", level=messages.SUCCESS)


def _media_approval_overview(run_id) -> str:
    scenes = list(Scene.objects.filter(run_id=run_id).order_by("scene_order"))
    if not scenes:
        return "No scenes yet."

    scene_ids = [scene.id for scene in scenes]
    images_by_scene: dict[str, list[SceneImageCandidate]] = {str(scene_id): [] for scene_id in scene_ids}
    videos_by_scene: dict[str, list[SceneVideoCandidate]] = {str(scene_id): [] for scene_id in scene_ids}
    for row in (
        SceneImageCandidate.objects.filter(scene_id__in=scene_ids)
        .only("candidate_id", "scene_id", "kind", "created_at", "liked", "approved_for_video")
        .order_by("created_at", "candidate_id")
    ):
        images_by_scene.setdefault(str(row.scene_id), []).append(row)
    for row in (
        SceneVideoCandidate.objects.filter(scene_id__in=scene_ids)
        .only(
            "candidate_id",
            "scene_id",
            "language",
            "created_at",
            "liked",
            "approved_for_production",
            "speed_coef",
        )
        .order_by("created_at", "candidate_id")
    ):
        videos_by_scene.setdefault(str(row.scene_id), []).append(row)

    parts = [
        '<table style="width:100%;border-collapse:collapse;">'
        "<thead><tr>"
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Scene</th>'
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Images</th>'
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Videos</th>'
        "</tr></thead><tbody>"
    ]
    for scene in scenes:
        image_rows = images_by_scene.get(str(scene.id), [])
        video_rows = videos_by_scene.get(str(scene.id), [])
        image_items = []
        for image in image_rows:
            media_url = reverse("admin:pipeline_admin_sceneimagecandidate_media", args=[image.candidate_id])
            image_items.append(
                format_html(
                    '<li><code>{}</code> {} {} {} <a href="{}" target="_blank" rel="noopener">open</a></li>',
                    image.candidate_id,
                    image.kind,
                    _status_badge(bool(image.approved_for_video)),
                    "liked" if image.liked else "not liked",
                    media_url,
                )
                + " "
                + _admin_change_link(image, "edit")
            )
        video_items = []
        for video in video_rows:
            media_url = reverse("admin:pipeline_admin_scenevideocandidate_media", args=[video.candidate_id])
            video_items.append(
                format_html(
                    '<li><code>{}</code> lang={} speed={} {} {} <a href="{}" target="_blank" rel="noopener">open</a></li>',
                    video.candidate_id,
                    video.language,
                    video.speed_coef,
                    _status_badge(bool(video.approved_for_production)),
                    "liked" if video.liked else "not liked",
                    media_url,
                )
                + " "
                + _admin_change_link(video, "edit")
            )

        parts.append(
            format_html(
                '<tr><td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);">'
                "<strong>Scene {}</strong><br>{}</td>"
                '<td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);">{}</td>'
                '<td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);">{}</td></tr>',
                scene.scene_order,
                _short(scene.text, 140),
                mark_safe("<ul style='margin:0;padding-left:18px;'>" + "".join(str(item) for item in image_items) + "</ul>")
                if image_items
                else "No images.",
                mark_safe("<ul style='margin:0;padding-left:18px;'>" + "".join(str(item) for item in video_items) + "</ul>")
                if video_items
                else "No videos.",
            )
        )
    parts.append("</tbody></table>")
    return mark_safe("".join(str(part) for part in parts))


def _final_artifacts_for_run(run_id) -> list[GenerationArtifact]:
    rows = GenerationArtifact.objects.filter(
        run_id=run_id,
        artifact_type__in=FINAL_VIDEO_ARTIFACT_TYPES,
    ).order_by("language", "artifact_type", "-created_at")
    return _latest_final_artifacts(rows)


def _latest_final_artifacts(rows) -> list[GenerationArtifact]:
    latest: dict[tuple[str, str], GenerationArtifact] = {}
    for row in rows:
        key = (str(row.language or "").strip().lower(), str(row.artifact_type or ""))
        if key not in latest:
            latest[key] = row
    return list(latest.values())


def _final_artifact_changelist_url(
    run_id: Any,
    *,
    language: str | None = None,
    variant: str | None = None,
) -> str:
    base = reverse("admin:pipeline_admin_generationartifact_changelist")
    params = [f"q={run_id}", "final_builds=1"]
    if language:
        params.append(f"language__exact={language}")
    if variant:
        params.append(f"artifact_type__exact=final_{variant}")
    return f"{base}?{'&'.join(params)}"


def _final_target_languages_for_run(
    run: GenerationRun,
    artifacts: list[GenerationArtifact],
    statuses: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    languages = {
        str(artifact.language or "").strip().lower()
        for artifact in artifacts
        if str(artifact.language or "").strip()
    }
    languages.update(language for language, _variant in statuses)
    chat_session = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at").first()
    session = chat_session.session_json if chat_session is not None and isinstance(chat_session.session_json, dict) else {}
    targets = session.get("target_languages")
    if isinstance(targets, list):
        languages.update(str(item).strip().lower() for item in targets if str(item).strip())
    production = session.get("production")
    post = production.get("post_production") if isinstance(production, dict) else None
    post_langs = post.get("languages") if isinstance(post, dict) else None
    if isinstance(post_langs, list):
        languages.update(str(item).strip().lower() for item in post_langs if str(item).strip())
    scene_ids = list(Scene.objects.filter(run_id=run.id).values_list("id", flat=True))
    if scene_ids:
        languages.update(
            str(item).strip().lower()
            for item in SceneVideoCandidate.objects.filter(scene_id__in=scene_ids).values_list("language", flat=True).distinct()
            if str(item).strip()
        )
    if not languages and run.language:
        languages.add(str(run.language).strip().lower())
    return sorted(language for language in languages if language)


def _final_build_url(run: GenerationRun, language: str, variant: str) -> str:
    try:
        return reverse("admin:pipeline_admin_generationrun_queue_final", args=[run.pk, language, variant])
    except NoReverseMatch:
        return f"/admin/pipeline_admin/generationrun/{run.pk}/queue-final/{language}/{variant}/"


def _status_badge_html(status: str, message: str = "") -> str:
    normalized = str(status or "").strip().lower()
    palette = {
        "pending": ("#7a4b00", "#fff4d8", "pending"),
        "running": ("#075985", "#e0f2fe", "running"),
        "stale": ("#92400e", "#fef3c7", "stale"),
        "success": ("#166534", "#dcfce7", "success"),
        "failed": ("#991b1b", "#fee2e2", "failed"),
    }
    color, background, label = palette.get(normalized, ("#374151", "#f3f4f6", normalized or "idle"))
    title = str(message or "")
    return format_html(
        '<span title="{}" style="display:inline-block;padding:2px 7px;border-radius:10px;background:{};color:{};font-weight:600;">{}</span>',
        title,
        background,
        color,
        label,
    )


def _final_build_progress_html(status: dict[str, Any], display_state: str) -> str:
    progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
    raw_percent = progress.get("percent")
    try:
        percent = int(float(raw_percent))
    except (TypeError, ValueError):
        defaults = {
            "idle": 0,
            "pending": 5,
            "running": 50,
            "stale": 100,
            "success": 100,
            "failed": 100,
        }
        percent = defaults.get(str(display_state or "").strip().lower(), 0)
    percent = max(0, min(100, percent))
    state = str(display_state or "").strip().lower()
    color = {
        "pending": "#d97706",
        "running": "#0284c7",
        "stale": "#d97706",
        "success": "#16a34a",
        "failed": "#dc2626",
    }.get(state, "#6b7280")
    return format_html(
        '<div style="margin-top:6px;width:180px;max-width:100%;">'
        '<div style="height:7px;border-radius:999px;background:#e5e7eb;overflow:hidden;">'
        '<div style="height:7px;width:{}%;background:{};"></div>'
        "</div>"
        '<div style="margin-top:2px;color:var(--body-quiet-color);font-size:11px;">{}%</div>'
        "</div>",
        percent,
        color,
        percent,
    )


def _artifact_video_preview(artifact: GenerationArtifact) -> str:
    raw = str(artifact.uri or "")
    media_url = _artifact_media_url(artifact)
    if not media_url and not raw.startswith(("http://", "https://")):
        return ""
    if not str(artifact.mime_type or "").startswith("video/"):
        return ""
    return format_html(
        '<video src="{}" controls preload="none" '
        'style="display:block;max-width:340px;width:100%;margin:6px 0;border:1px solid var(--border-color);border-radius:4px;"></video>',
        media_url or raw,
    )


def _final_videos_panel(run: GenerationRun) -> str:
    artifacts = _final_artifacts_for_run(run.id)
    artifact_by_key = {
        (
            str(artifact.language or "").strip().lower(),
            str(artifact.artifact_type or "").removeprefix("final_"),
        ): artifact
        for artifact in artifacts
    }
    statuses = _latest_final_build_statuses(run.id)
    languages = _final_target_languages_for_run(run, artifacts, statuses)
    parts = ['<div class="cf-final-videos">']
    try:
        _chat_session, session, production = _run_session_for_admin_render(run)
        parts.append(_title_color_status_html(session, production, run=run))
    except Exception:
        pass
    parts.append(
        format_html(
            '<p style="margin:0 0 12px;"><a class="button" href="{}">Rebuild all final videos</a> '
            '<a href="{}" style="margin-left:8px;">open all final builds</a></p>',
            reverse("admin:pipeline_admin_generationrun_build_final", args=[run.pk]),
            _final_artifact_changelist_url(run.id),
        )
    )
    if not languages:
        parts.append(
            '<p style="margin:0;color:var(--body-quiet-color);">'
            "No renderable final video languages found yet.</p>"
        )
        parts.append("</div>")
        return mark_safe("".join(str(part) for part in parts))

    parts.append(
        '<table style="width:100%;border-collapse:collapse;margin:0 0 14px;">'
        "<thead><tr>"
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Language</th>'
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Variant</th>'
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Latest video</th>'
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Build status</th>'
        '<th style="text-align:left;border-bottom:1px solid var(--border-color);padding:6px;">Links</th>'
        "</tr></thead><tbody>"
    )
    for language in languages:
        for variant, label in FINAL_VIDEO_VARIANT_LABELS.items():
            artifact = artifact_by_key.get((language, variant))
            payload = artifact.payload_json if artifact is not None and isinstance(artifact.payload_json, dict) else {}
            size = f'{payload.get("width")}x{payload.get("height")}' if payload.get("width") and payload.get("height") else ""
            artifact_created = artifact.created_at.strftime("%Y-%m-%d %H:%M:%S") if artifact is not None and artifact.created_at else ""
            latest_video = (
                format_html(
                    "{}<div style='margin-top:4px;'><code>{}</code>{}<br>{}</div>",
                    _artifact_video_preview(artifact),
                    artifact.artifact_type,
                    format_html(" · {}", size) if size else "",
                    format_html(
                        '<span style="color:var(--body-quiet-color);">{}</span>',
                        artifact_created,
                    )
                    if artifact_created
                    else "",
                )
                if artifact is not None
                else mark_safe(
                    '<span style="color:var(--body-quiet-color);">No latest video yet.</span>'
                )
            )
            status = statuses.get((language, variant), {})
            state = str(status.get("status") or "idle")
            status_message = str(status.get("message") or "")
            updated_at = _final_build_updated_at(status)
            heartbeat_at = _final_build_time_value(status, "heartbeat_at")
            stale = _final_build_status_is_stale(status)
            active = state in FINAL_BUILD_ACTIVE_STATUSES and not stale
            display_state = "stale" if stale else state
            progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
            detail_lines: list[str] = []
            if progress.get("label"):
                detail_lines.append(str(progress.get("label")))
            if status.get("worker_id"):
                detail_lines.append(f"worker {status.get('worker_id')}")
            if status.get("attempt_count"):
                detail_lines.append(f"attempt {status.get('attempt_count')}")
            if heartbeat_at is not None:
                detail_lines.append(f"heartbeat {heartbeat_at.isoformat(timespec='seconds')}")
            elif updated_at is not None:
                detail_lines.append(f"updated {updated_at.isoformat(timespec='seconds')}")
            status_detail = (
                format_html(
                    '<div style="margin-top:4px;color:var(--body-quiet-color);font-size:12px;">{}</div>',
                    " · ".join(detail_lines),
                )
                if detail_lines
                else ""
            )
            visible_message = (
                format_html(
                    '<div style="max-width:520px;margin-top:4px;color:#991b1b;white-space:pre-wrap;">{}</div>',
                    status_message,
                )
                if state == "failed" and status_message
                else ""
            )
            if stale:
                visible_message = format_html(
                    '<div style="max-width:520px;margin-top:4px;color:#92400e;white-space:pre-wrap;">{}</div>',
                    "No recent heartbeat from final_build_worker. Rebuild is available.",
                )
            build_link = (
                format_html(
                    '<span style="color:var(--body-quiet-color);">{}</span>',
                    "Queued; waiting for final_build_worker." if state == "pending" else "Build is running in final_build_worker; refresh for status.",
                )
                if active
                else format_html(
                    '<a class="button" href="{}">Build/rebuild this video</a>',
                    _final_build_url(run, language, variant),
                )
            )
            latest_link = _generation_artifact_link(artifact, "open latest video") if artifact is not None else ""
            finder_link = _artifact_finder_link(artifact)
            links = format_html(
                "{}{}{}<div style='margin-top:6px;'><a href='{}'>all builds</a></div>",
                build_link,
                format_html("<div style='margin-top:6px;'>{}</div>", latest_link) if latest_link else "",
                format_html("<div style='margin-top:6px;'>{}</div>", finder_link) if finder_link else "",
                _final_artifact_changelist_url(run.id, language=language, variant=variant),
            )
            parts.append(
                format_html(
                    '<tr><td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);"><code>{}</code></td>'
                    '<td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);">{}</td>'
                    '<td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);">{}</td>'
                    '<td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);">{} {} {}</td>'
                    '<td style="vertical-align:top;padding:8px;border-bottom:1px solid var(--border-color);">{}</td></tr>',
                    language,
                    label,
                    latest_video,
                    _status_badge_html(display_state, status_message),
                    str(status.get("updated_at") or "")[:19],
                    format_html(
                        "{}{}{}",
                        _final_build_progress_html(status, display_state),
                        status_detail,
                        visible_message,
                    ),
                    links,
                )
            )
    parts.append("</tbody></table></div>")
    return mark_safe("".join(str(part) for part in parts))


def _clean_admin_inline_speech(value: Any) -> str:
    return " ".join(str(value or "").split())


def _admin_actor(request) -> str:
    user = getattr(request, "user", None)
    if user is None:
        return ""
    getter = getattr(user, "get_username", None)
    if callable(getter):
        return str(getter() or "").strip()
    return str(getattr(user, "username", "") or "").strip()


def _sync_series_context_from_run(session: dict[str, Any], run: GenerationRun) -> None:
    series = getattr(run, "series", None)
    if series is None:
        for key in (
            "series",
            "series_title_by_lang",
            "series_video_index",
            "series_total_videos",
            "series_counter_by_lang",
            "series_render_counter_advanced",
        ):
            session.pop(key, None)
        return
    video_index = int(getattr(series, "current_video_index", None) or getattr(run, "series_video_index", None) or 1)
    total_videos = int(getattr(series, "total_videos", None) or getattr(run, "series_total_videos", None) or 1)
    title_by_lang = {
        "ru": " ".join(str(getattr(series, "title_ru", "") or "").split()),
        "en": " ".join(str(getattr(series, "title_en", "") or "").split()),
    }
    counter_by_lang = _series_counter_by_lang_from_series(series) or {
        "ru": f"день {video_index} из {total_videos}",
        "en": f"day {video_index} of {total_videos}",
    }
    session["series"] = {
        "id": str(getattr(series, "id", "") or ""),
        "title_by_lang": title_by_lang,
        "video_index": video_index,
        "total_videos": total_videos,
        "counter_by_lang": counter_by_lang,
    }
    session["series_title_by_lang"] = title_by_lang
    session["series_video_index"] = video_index
    session["series_total_videos"] = total_videos
    session["series_counter_by_lang"] = counter_by_lang


def _sync_overlay_title_from_run(
    session: dict[str, Any],
    run: GenerationRun,
    title_by_lang: dict[str, str] | None = None,
) -> None:
    production = session.get("production")
    if not isinstance(production, dict):
        production = {}
    title_by_lang = _clean_overlay_title_by_lang(title_by_lang) or _default_overlay_title(run)
    if title_by_lang:
        ru_title = _russian_overlay_title(title_by_lang)
        session["overlay_title_by_lang"] = title_by_lang
        production["overlay_title_by_lang"] = title_by_lang
        if ru_title:
            session["overlay_title"] = ru_title
            production["overlay_title"] = ru_title
    else:
        session.pop("overlay_title_by_lang", None)
        session.pop("overlay_title", None)
        production.pop("overlay_title_by_lang", None)
        production.pop("overlay_title", None)
    session["production"] = production


def _sync_overlay_subtitle_from_run(
    session: dict[str, Any],
    subtitle_by_lang: dict[str, str] | None = None,
) -> None:
    production = session.get("production")
    if not isinstance(production, dict):
        production = {}
    subtitle_by_lang = _clean_overlay_title_by_lang(subtitle_by_lang)
    if subtitle_by_lang:
        session["overlay_subtitle_by_lang"] = subtitle_by_lang
        production["overlay_subtitle_by_lang"] = subtitle_by_lang
    else:
        session.pop("overlay_subtitle_by_lang", None)
        production.pop("overlay_subtitle_by_lang", None)
    session["production"] = production


def _record_scene_text_revision(
    scene: Scene,
    *,
    field_name: str,
    old_text: Any,
    new_text: Any,
    request=None,
    source: str = "admin",
    language: str | None = None,
    meta_json: dict[str, Any] | None = None,
) -> SceneTextRevision | None:
    old_clean = _clean_admin_inline_speech(old_text)
    new_clean = _clean_admin_inline_speech(new_text)
    if old_clean == new_clean:
        return None
    return SceneTextRevision.objects.create(
        run_id=scene.run_id,
        scene_id=scene.id,
        scene_order=scene.scene_order,
        field_name=field_name,
        language=language,
        old_text=old_clean,
        new_text=new_clean,
        source=source,
        actor=_admin_actor(request) or None,
        meta_json=meta_json or {},
        created_at=timezone.now(),
    )


def _record_scene_text_revision_changes(
    scene: Scene,
    changes: list[dict[str, Any]],
    *,
    request=None,
    source: str,
) -> None:
    for change in changes:
        _record_scene_text_revision(
            scene,
            field_name=str(change.get("field_name") or ""),
            language=change.get("language"),
            old_text=change.get("old_text"),
            new_text=change.get("new_text"),
            request=request,
            source=source,
            meta_json=change.get("meta_json") if isinstance(change.get("meta_json"), dict) else None,
        )


def _scene_text_revision_table(rows) -> str:
    rows = list(rows)
    if not rows:
        return "No text revisions yet."
    body = []
    for row in rows:
        field = f"{row.field_name}:{row.language}" if row.language else row.field_name
        body.append(
            format_html(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><pre style='white-space:pre-wrap;margin:0'>{}</pre></td><td><pre style='white-space:pre-wrap;margin:0'>{}</pre></td></tr>",
                row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                row.scene_order or "-",
                field,
                row.actor or row.source,
                row.old_text,
                row.new_text,
            )
        )
    return format_html(
        "<table><thead><tr><th>Time</th><th>Scene</th><th>Field</th><th>Actor</th><th>Old</th><th>New</th></tr></thead><tbody>{}</tbody></table>",
        mark_safe("".join(str(item) for item in body)),
    )


class SceneInlineForm(django_forms.ModelForm):
    speech_ru = django_forms.CharField(
        required=False,
        label="Speech RU",
        widget=django_forms.Textarea(attrs={"rows": 2}),
        help_text="Русская речь для озвучки/субтитров.",
    )
    speech_en = django_forms.CharField(
        required=False,
        label="Speech EN",
        widget=django_forms.Textarea(attrs={"rows": 2}),
        help_text="Английская речь для EN-версии.",
    )

    class Meta:
        model = Scene
        fields = ("id", "scene_order", "duration_sec", "text")
        widgets = {"id": django_forms.HiddenInput}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["text"].required = False
        self.fields["scene_order"].required = False
        self.fields["duration_sec"].required = False
        speech: dict[str, Any] = {}
        if self.instance and self.instance.pk and not self.instance._state.adding:
            payload = _latest_scene_payload_for_admin(self.instance)
            payload_speech = payload.get("speech_by_lang") if isinstance(payload, dict) else None
            if isinstance(payload_speech, dict):
                speech.update(payload_speech)
            model_speech = self.instance.speech_by_lang
            if isinstance(model_speech, dict) and model_speech:
                speech.update(model_speech)
        self._initial_speech_by_lang = {
            str(key).strip().lower(): _clean_admin_inline_speech(value)
            for key, value in speech.items()
            if str(key).strip() and _clean_admin_inline_speech(value)
        }
        self.fields["speech_ru"].initial = self._initial_speech_by_lang.get(
            "ru",
            _clean_admin_inline_speech(getattr(self.instance, "text", "")),
        )
        self.fields["speech_en"].initial = self._initial_speech_by_lang.get("en", "")

    def clean_scene_order(self):
        value = self.cleaned_data.get("scene_order")
        return value if value is not None else self.instance.scene_order

    def clean_duration_sec(self):
        value = self.cleaned_data.get("duration_sec")
        return value if value is not None else self.instance.duration_sec

    def clean_text(self):
        value = str(self.cleaned_data.get("text") or "").strip()
        return value or str(getattr(self.instance, "text", "") or "")

    def save(self, commit=True):
        obj: Scene = super().save(commit=False)
        speech = dict(self._initial_speech_by_lang)
        revision_changes: list[dict[str, Any]] = []
        initial_text = _clean_admin_inline_speech(getattr(self.instance, "text", ""))
        cleaned_text = _clean_admin_inline_speech(self.cleaned_data.get("text"))
        if initial_text != cleaned_text:
            revision_changes.append(
                {
                    "field_name": "text",
                    "language": None,
                    "old_text": initial_text,
                    "new_text": cleaned_text,
                }
            )
        if "speech_ru" in self.changed_data:
            old_ru_speech = _clean_admin_inline_speech(self.fields["speech_ru"].initial)
            ru_speech = _clean_admin_inline_speech(self.cleaned_data.get("speech_ru"))
            if ru_speech:
                speech["ru"] = ru_speech
            else:
                speech.pop("ru", None)
            if old_ru_speech != ru_speech:
                revision_changes.append(
                    {
                        "field_name": "speech",
                        "language": "ru",
                        "old_text": old_ru_speech,
                        "new_text": ru_speech,
                    }
                )
        if "speech_en" in self.changed_data:
            old_en_speech = _clean_admin_inline_speech(self.fields["speech_en"].initial)
            en_speech = _clean_admin_inline_speech(self.cleaned_data.get("speech_en"))
            if en_speech:
                speech["en"] = en_speech
            else:
                speech.pop("en", None)
            if old_en_speech != en_speech:
                revision_changes.append(
                    {
                        "field_name": "speech",
                        "language": "en",
                        "old_text": old_en_speech,
                        "new_text": en_speech,
                    }
                )
        obj.speech_by_lang = speech
        obj._scene_text_revision_changes = revision_changes
        if commit:
            obj.save()
        return obj


class SceneInline(admin.TabularInline):
    model = Scene
    form = SceneInlineForm
    extra = 0
    can_delete = True
    fields = ("id", "scene_order", "duration_sec", "text", "speech_ru", "speech_en", "scene_link")
    readonly_fields = ("scene_link",)
    show_change_link = True

    @admin.display(description="Scene text")
    def short_text(self, obj: Scene) -> str:
        return _short(obj.text)

    @admin.display(description="Open")
    def scene_link(self, obj: Scene) -> str:
        if not obj.pk:
            return ""
        return _admin_change_link(obj, "open scene")


class GenerationArtifactInlineForm(django_forms.ModelForm):
    class Meta:
        model = GenerationArtifact
        fields = ("id",)
        widgets = {"id": django_forms.HiddenInput}


class GenerationArtifactInline(admin.TabularInline):
    model = GenerationArtifact
    form = GenerationArtifactInlineForm
    extra = 0
    can_delete = False
    fields = ("id", "label", "artifact_type", "scene", "language", "mime_type", "uri_link", "created_at")
    readonly_fields = ("label", "artifact_type", "scene", "language", "mime_type", "uri_link", "created_at")
    show_change_link = True

    @admin.display(description="URI")
    def uri_link(self, obj: GenerationArtifact) -> str:
        return _generation_artifact_link(obj, "open")


def _overlay_title_for_admin_edit(run: GenerationRun) -> dict[str, str]:
    session: dict[str, Any] = {}
    chat_session = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at").first()
    if chat_session is not None and isinstance(chat_session.session_json, dict):
        session = copy.deepcopy(chat_session.session_json or {})
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    title_by_lang: dict[str, str] = _stored_overlay_title_by_lang_for_run(run)
    for source in (production, session):
        source_title = _clean_overlay_title_by_lang(source.get("overlay_title_by_lang"))
        if _looks_like_default_overlay_title(source_title, run) and title_by_lang:
            ru_title = _russian_overlay_title(source_title)
            if ru_title:
                title_by_lang["ru"] = ru_title
            continue
        title_by_lang.update(source_title)
    title_by_lang.update(_active_title_override_for_run(run))
    return title_by_lang or _default_overlay_title(run)


def _overlay_subtitle_for_admin_edit(run: GenerationRun) -> dict[str, str]:
    session: dict[str, Any] = {}
    chat_session = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at").first()
    if chat_session is not None and isinstance(chat_session.session_json, dict):
        session = copy.deepcopy(chat_session.session_json or {})
    production = session.get("production") if isinstance(session.get("production"), dict) else {}
    return _overlay_subtitle_for_admin_render(run, session, production)


class GenerationRunAdminForm(django_forms.ModelForm):
    overlay_title_ru = django_forms.CharField(
        required=False,
        label="Title RU",
        widget=django_forms.Textarea(attrs={"rows": 2}),
    )
    overlay_title_en = django_forms.CharField(
        required=False,
        label="Title EN",
        widget=django_forms.Textarea(attrs={"rows": 2}),
    )
    overlay_subtitle_ru = django_forms.CharField(
        required=False,
        label="Third title RU",
        widget=django_forms.Textarea(attrs={"rows": 2}),
    )
    overlay_subtitle_en = django_forms.CharField(
        required=False,
        label="Third title EN",
        widget=django_forms.Textarea(attrs={"rows": 2}),
    )

    class Meta:
        model = GenerationRun
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            title_by_lang = _overlay_title_for_admin_edit(self.instance)
            subtitle_by_lang = _overlay_subtitle_for_admin_edit(self.instance)
        else:
            title_by_lang = _default_overlay_title(self.instance)
            subtitle_by_lang = {}
        self.fields["overlay_title_ru"].initial = title_by_lang.get("ru", "")
        self.fields["overlay_title_en"].initial = title_by_lang.get("en", "")
        self.fields["overlay_subtitle_ru"].initial = subtitle_by_lang.get("ru", "")
        self.fields["overlay_subtitle_en"].initial = subtitle_by_lang.get("en", "")

    def clean(self):
        cleaned = super().clean()
        ru_title = " ".join(str(cleaned.get("overlay_title_ru") or "").split())
        en_title = " ".join(str(cleaned.get("overlay_title_en") or "").split())
        if en_title and not ru_title:
            ru_title = en_title
        cleaned["overlay_title_ru"] = ru_title
        cleaned["overlay_title_en"] = en_title
        cleaned["overlay_subtitle_ru"] = " ".join(str(cleaned.get("overlay_subtitle_ru") or "").split())
        cleaned["overlay_subtitle_en"] = " ".join(str(cleaned.get("overlay_subtitle_en") or "").split())
        return cleaned

    def overlay_title_by_lang(self) -> dict[str, str]:
        title_by_lang = {
            "ru": " ".join(str(self.cleaned_data.get("overlay_title_ru") or "").split()),
            "en": " ".join(str(self.cleaned_data.get("overlay_title_en") or "").split()),
        }
        return {key: value for key, value in title_by_lang.items() if value}

    def overlay_subtitle_by_lang(self) -> dict[str, str]:
        subtitle_by_lang = {
            "ru": " ".join(str(self.cleaned_data.get("overlay_subtitle_ru") or "").split()),
            "en": " ".join(str(self.cleaned_data.get("overlay_subtitle_en") or "").split()),
        }
        return {key: value for key, value in subtitle_by_lang.items() if value}

    def save(self, commit=True):
        obj: GenerationRun = super().save(commit=False)
        title_by_lang = self.overlay_title_by_lang() if self.is_valid() else {}
        obj.human_title = title_by_lang.get("ru") or title_by_lang.get("en") or None
        if commit:
            obj.save()
            self.save_m2m()
        return obj


@admin.register(GenerationRun)
class GenerationRunAdmin(admin.ModelAdmin):
    form = GenerationRunAdminForm
    list_display = (
        "display_title",
        "project",
        "workflow_type",
        "status",
        "is_active",
        "is_draft",
        "active_cursor",
        "language",
        "series",
        "series_video_index",
        "series_total_videos",
        "created_at",
        "artifact_count",
    )
    list_editable = ("is_active", "is_draft", "series", "series_video_index", "series_total_videos")
    search_fields = ("human_title", "script", "id", "series__title_ru", "series__title_en")
    list_filter = ("status", "workflow_type", "language", "series", "is_draft")
    list_per_page = 50
    list_max_show_all = 200
    show_full_result_count = False
    list_select_related = ("project", "series")
    autocomplete_fields = ("series",)
    actions = (build_final_title_and_videos,)
    readonly_fields = (
        "id",
        "created_at",
        "overlay_title_color",
        "script",
        "final_videos",
        "publication_prep",
        "prompt_overview",
        "media_approval_overview",
        "scene_text_history",
    )
    fields = (
        "id",
        "overlay_title_ru",
        "overlay_title_en",
        "overlay_subtitle_ru",
        "overlay_subtitle_en",
        "project",
        "workflow_type",
        "status",
        "is_active",
        "is_draft",
        "current_scene_index",
        "current_media_substep",
        "current_media_language_index",
        "language",
        "series",
        "series_video_index",
        "series_total_videos",
        "scheduled_item",
        "created_at",
        "overlay_title_color",
        "final_videos",
        "publication_prep",
        "prompt_overview",
        "media_approval_overview",
        "scene_text_history",
        "script",
    )
    inlines = (SceneInline, GenerationArtifactInline)

    def save_model(self, request, obj: GenerationRun, form, change):
        changed_fields = set(getattr(form, "changed_data", []) or [])
        sync_overlay_title = (not change) or bool({"human_title", "overlay_title_ru", "overlay_title_en"} & changed_fields)
        sync_overlay_subtitle = (not change) or bool({"overlay_subtitle_ru", "overlay_subtitle_en"} & changed_fields)
        title_by_lang = form.overlay_title_by_lang() if hasattr(form, "overlay_title_by_lang") else None
        subtitle_by_lang = form.overlay_subtitle_by_lang() if hasattr(form, "overlay_subtitle_by_lang") else None
        super().save_model(request, obj, form, change)
        if obj.is_draft:
            _ensure_generation_draft_for_admin_run(obj)
        if sync_overlay_title:
            _write_title_override_artifact(obj, overlay_title_by_lang=title_by_lang)
        if sync_overlay_subtitle:
            _write_title_subtitle_override_artifact(obj, overlay_subtitle_by_lang=subtitle_by_lang)
        for chat_session in ChatSession.objects.filter(generation_run_id=obj.id):
            session = copy.deepcopy(chat_session.session_json or {})
            if sync_overlay_title:
                _sync_overlay_title_from_run(session, obj, title_by_lang=title_by_lang)
            if sync_overlay_subtitle:
                _sync_overlay_subtitle_from_run(session, subtitle_by_lang=subtitle_by_lang)
            _sync_series_context_from_run(session, obj)
            if obj.current_scene_index is not None:
                session["image_scene_index"] = int(obj.current_scene_index)
            if obj.current_media_substep:
                session["scene_media_substep"] = obj.current_media_substep
            if obj.current_media_language_index is not None:
                session["scene_media_language_index"] = int(obj.current_media_language_index)
            if obj.is_active and obj.current_media_substep:
                session["state"] = "waiting_scene_media"
                chat_session.state = "waiting_scene_media"
            chat_session.session_json = session
            chat_session.updated_at = timezone.now()
            chat_session.save(update_fields=["state", "session_json", "updated_at"])

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        if formset.model is Scene and form.instance and form.instance.pk:
            for scene_form in getattr(formset, "forms", []):
                scene = getattr(scene_form, "instance", None)
                changes = getattr(scene, "_scene_text_revision_changes", None)
                if scene is not None and changes:
                    _record_scene_text_revision_changes(
                        scene,
                        changes,
                        request=request,
                        source="generation_run_admin_inline",
                    )
            _renumber_scenes_for_run(form.instance.pk)
            _sync_chat_sessions_for_run_scenes(form.instance.pk)

    @admin.display(description="Final title color")
    def overlay_title_color(self, obj: GenerationRun) -> str:
        return mark_safe(_title_color_picker_html(obj))

    @admin.display(description="Cursor")
    def active_cursor(self, obj: GenerationRun) -> str:
        scene = "-" if obj.current_scene_index is None else str(int(obj.current_scene_index) + 1)
        substep = obj.current_media_substep or "-"
        lang_idx = "-" if obj.current_media_language_index is None else str(obj.current_media_language_index)
        state = "active" if obj.is_active else "inactive"
        return f"{state}: scene {scene}, {substep}, lang {lang_idx}"

    @admin.display(description="Prompts by scene")
    def prompt_overview(self, obj: GenerationRun) -> str:
        scenes = list(Scene.objects.filter(run_id=obj.id).order_by("scene_order", "id"))
        if not scenes:
            return "No scenes."
        parts = ['<div style="display:grid;gap:10px;">']
        for scene in scenes:
            video_langs = ", ".join(sorted((scene.video_prompt_by_lang or {}).keys())) or "-"
            speech_langs = ", ".join(sorted((scene.speech_by_lang or {}).keys())) or "-"
            syntax_langs = ", ".join(sorted((scene.syntax_prompt_by_lang or {}).keys())) or "-"
            parts.append(
                "<section style='border:1px solid var(--border-color);border-radius:6px;padding:10px;'>"
                f"<strong>Scene {scene.scene_order}</strong> {_admin_change_link(scene, 'edit prompts')}"
                f"<div>start: {'yes' if scene.start_frame_prompt else 'no'}; "
                f"effective: {'yes' if scene.start_frame_prompt_effective else 'no'}; "
                f"end: {'yes' if scene.end_frame_prompt else 'no'}</div>"
                f"<div>video languages: {escape(video_langs)}; speech: {escape(speech_langs)}; Syntax compact: {escape(syntax_langs)}</div>"
                "</section>"
            )
        parts.append("</div>")
        return mark_safe("".join(parts))

    @admin.display(description="Scene text history")
    def scene_text_history(self, obj: GenerationRun) -> str:
        rows = SceneTextRevision.objects.filter(run_id=obj.id).order_by("-created_at")[:50]
        return _scene_text_revision_table(rows)

    def get_queryset(self, request):
        artifact_counts = (
            GenerationArtifact.objects.filter(run_id=OuterRef("pk"))
            .order_by()
            .values("run_id")
            .annotate(total=Count("pk"))
            .values("total")
        )
        queryset = super().get_queryset(request).annotate(
            _artifact_count=Coalesce(Subquery(artifact_counts, output_field=IntegerField()), Value(0))
        )
        if request.GET.get("status__exact") == ARCHIVED_TEST_RUN_STATUS:
            return queryset
        return queryset.exclude(status=ARCHIVED_TEST_RUN_STATUS)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/title-color/",
                self.admin_site.admin_view(self.title_color_view),
                name="pipeline_admin_generationrun_title_color",
            ),
            path(
                "<path:object_id>/build-final/",
                self.admin_site.admin_view(self.build_final_view),
                name="pipeline_admin_generationrun_build_final",
            ),
            path(
                "<path:object_id>/queue-final/<str:language>/<str:variant>/",
                self.admin_site.admin_view(self.queue_final_view),
                name="pipeline_admin_generationrun_queue_final",
            ),
            path(
                "<path:object_id>/queue-publications/",
                self.admin_site.admin_view(self.queue_publications_view),
                name="pipeline_admin_generationrun_queue_publications",
            ),
        ]
        return custom + urls

    def title_color_view(self, request, object_id):
        if request.method != "POST":
            return JsonResponse({"ok": False, "error": "POST required."}, status=405)
        run = self.get_object(request, object_id)
        if run is None:
            raise Http404("Generation run not found.")
        try:
            pending_style = _stage_pending_title_color_for_run(run, request.POST.get("title_color") or "")
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "title_color": pending_style.get("title_color"),
                "message": "Saved; applies on next Build/rebuild.",
            }
        )

    def build_final_view(self, request, object_id):
        run = self.get_object(request, object_id)
        if run is None:
            raise Http404("Generation run not found.")
        queued = 0
        artifacts = _final_artifacts_for_run(run.id)
        statuses = _latest_final_build_statuses(run.id)
        languages = _final_target_languages_for_run(run, artifacts, statuses)
        for language in languages:
            for variant in FINAL_VIDEO_VARIANT_LABELS:
                try:
                    _queue_final_build(run, language=language, variant=variant, kick_worker=False)
                except Exception as exc:
                    self.message_user(
                        request,
                        f"{run}: final build was not queued for {language}/{variant}: {exc}",
                        level=messages.ERROR,
                    )
                    continue
                queued += 1
        if queued:
            _kick_final_build_worker(run.id)
            self.message_user(
                request,
                f"{run}: queued {queued} final render job(s). final_build_worker was started automatically; refresh for worker status.",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(request, f"{run}: no final render jobs were queued.", level=messages.WARNING)
        return HttpResponseRedirect(reverse("admin:pipeline_admin_generationrun_change", args=[run.pk]))

    def queue_final_view(self, request, object_id, language, variant):
        run = self.get_object(request, object_id)
        if run is None:
            raise Http404("Generation run not found.")
        try:
            status = _queue_final_build(run, language=language, variant=variant)
        except Exception as exc:
            self.message_user(request, f"{run}: final build was not queued: {exc}", level=messages.ERROR)
        else:
            self.message_user(
                request,
                f"{run}: queued {FINAL_VIDEO_VARIANT_LABELS.get(variant, variant)} ({language}); current status: {status}. final_build_worker was started automatically; refresh for worker status.",
                level=messages.SUCCESS,
            )
        return HttpResponseRedirect(reverse("admin:pipeline_admin_generationrun_change", args=[run.pk]))

    def queue_publications_view(self, request, object_id):
        run = self.get_object(request, object_id)
        if run is None:
            raise Http404("Generation run not found.")
        project_choices = _publication_project_choices()
        default_project_slug = _project_slug_for_publication_run(run) or (project_choices[0][0] if project_choices else "")
        video_source_choices = _publication_video_source_choices(run)
        video_source_initials = _publication_video_source_initials(video_source_choices)
        post_production = _publication_post_production_from_final_artifacts(run)
        has_publication_video = _publication_post_production_has_video(post_production)
        if not has_publication_video:
            self.message_user(
                request,
                f"{run}: no final video artifacts are available yet; descriptions can still be generated from the recipe.",
                level=messages.WARNING,
            )
        if has_publication_video and not all_final_artifacts_delivered(run.id):
            self.message_user(
                request,
                f"{run}: final videos are not confirmed delivered to Telegram yet; queueing from existing final artifacts is still available.",
                level=messages.WARNING,
            )
        initial_copy = _publication_copy_from_run(run)
        recipe_text = str(
            request.POST.get("publication_recipe")
            if request.method == "POST"
            else (_publication_recipe_from_draft(run) or _publication_recipe_for_run(run))
        ).strip()
        generation_feedback = str(request.POST.get("generation_feedback") or "") if request.method == "POST" else ""
        if request.method == "POST" and "generate_descriptions" in request.POST:
            posted_copy = {
                key: str(request.POST.get(key) or initial_copy.get(key) or "")
                for key, _label in SOCIAL_COPY_FIELD_LABELS
            }
            try:
                initial_copy = _generate_publication_copy_for_admin(
                    run,
                    current_copy=posted_copy,
                    recipe=recipe_text,
                    feedback=generation_feedback,
                )
            except Exception as exc:
                self.message_user(request, f"{run}: descriptions were not generated: {exc}", level=messages.ERROR)
                initial_copy = posted_copy
            else:
                try:
                    _save_publication_copy_draft(
                        run,
                        copy_payload=initial_copy,
                        recipe=recipe_text,
                        feedback=generation_feedback,
                    )
                except Exception as exc:
                    self.message_user(request, f"{run}: descriptions generated but were not saved: {exc}", level=messages.WARNING)
                self.message_user(
                    request,
                    f"{run}: descriptions generated and saved; review/edit copy below before queueing publication jobs.",
                    level=messages.SUCCESS,
                )
                generation_feedback = ""
            form = PublicationQueueAdminForm(
                None,
                copy=initial_copy,
                project_choices=project_choices,
                video_source_choices=video_source_choices,
                initial={
                    "project_slug": str(request.POST.get("project_slug") or default_project_slug),
                    "target_keys": [target.key for target in build_publication_targets(initial_copy) if target.description],
                    "video_variant": str(request.POST.get("video_variant") or DEFAULT_PUBLICATION_VIDEO_VARIANT),
                    "scheduled_at": request.POST.get("scheduled_at") or None,
                    **video_source_initials,
                    "video_source_ru": str(request.POST.get("video_source_ru") or video_source_initials.get("video_source_ru") or "upload"),
                    "video_source_en": str(request.POST.get("video_source_en") or video_source_initials.get("video_source_en") or "upload"),
                    **initial_copy,
                },
            )
        else:
            form = PublicationQueueAdminForm(
                request.POST or None,
                request.FILES or None,
                copy=initial_copy,
                project_choices=project_choices,
                video_source_choices=video_source_choices,
                initial={
                    "project_slug": default_project_slug,
                    "target_keys": [target.key for target in build_publication_targets(initial_copy) if target.description],
                    "video_variant": DEFAULT_PUBLICATION_VIDEO_VARIANT,
                    **video_source_initials,
                    **initial_copy,
                },
            )
        if request.method == "POST" and "generate_descriptions" not in request.POST and form.is_valid():
            try:
                _save_publication_copy_draft(
                    run,
                    copy_payload=form.copy_payload(),
                    recipe=recipe_text,
                    feedback=generation_feedback,
                )
            except Exception as exc:
                self.message_user(request, f"{run}: descriptions were not saved as draft: {exc}", level=messages.WARNING)
            staging_paths: list[Path] = []
            try:
                selected_post_production, source_labels, staging_paths = _publication_post_production_from_form(run, form)
                with SessionLocal() as db:
                    jobs = queue_publication_bundle(
                        db,
                        project_slug=str(form.cleaned_data["project_slug"]),
                        recipe=f"{run.id}\n\n{recipe_text}",
                        copy=form.copy_payload(),
                        post_production=selected_post_production,
                        origin="admin_generation_run",
                        selected_target_keys=form.cleaned_data["target_keys"],
                        video_variant="clean",
                        video_source_labels=source_labels,
                        scheduled_at=form.cleaned_data.get("scheduled_at"),
                    )
                    for job in jobs:
                        job.payload_json = {**(job.payload_json or {}), "run_id": str(run.id)}
                    db.commit()
            except Exception as exc:
                self.message_user(request, f"{run}: publication jobs were not queued: {exc}", level=messages.ERROR)
            else:
                dispatch_error = ""
                ready_job_ids = [
                    str(job.id)
                    for job in jobs
                    if job.status == "approved" and job.scheduled_at is None
                ]
                if ready_job_ids:
                    try:
                        from .tasks import publish_publication_jobs_task

                        publish_publication_jobs_task.delay(ready_job_ids)
                    except Exception as exc:
                        dispatch_error = f" Direct API worker was not queued: {exc}"
                self.message_user(
                    request,
                    publication_jobs_summary(jobs) + dispatch_error,
                    level=messages.SUCCESS if jobs and not dispatch_error else messages.WARNING,
                )
                return HttpResponseRedirect(reverse("admin:pipeline_admin_publicationjob_changelist"))
            finally:
                for path in staging_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
        csrf = get_token(request)
        back_url = reverse("admin:pipeline_admin_generationrun_change", args=[run.pk])
        display_copy = {
            key: str(form.data.get(key) if form.is_bound else initial_copy.get(key) or "")
            for key, _label in SOCIAL_COPY_FIELD_LABELS
        }
        html = format_html(
            '<!doctype html><html><head><title>Queue publications</title></head>'
            '<body class="app-pipeline_admin model-generationrun change-form">'
            '<div id="container"><div id="content" class="colM">'
            '<h1>Queue publications for {}</h1>'
            '<p><a href="{}">Back to video</a></p>'
            '<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrfmiddlewaretoken" value="{}">'
            '{}'
            '<div class="submit-row"><input type="submit" value="Publish selected targets / Queue publication jobs" class="default"></div>'
            '</form></div></div></body></html>',
            run.human_title or run.id,
            back_url,
            csrf,
            _publication_queue_form_html(
                form,
                run,
                display_copy,
                recipe=recipe_text,
                generation_feedback=generation_feedback,
            ),
        )
        return HttpResponse(html)

    @admin.display(description="Title", ordering="human_title")
    def display_title(self, obj: GenerationRun) -> str:
        return obj.human_title or _short(obj.script) or str(obj.id)

    @admin.display(description="Artifacts")
    def artifact_count(self, obj: GenerationRun) -> int:
        return int(obj._artifact_count)

    @admin.display(description="Approved media by scene")
    def media_approval_overview(self, obj: GenerationRun) -> str:
        return _media_approval_overview(obj.id)

    @admin.display(description="Final videos")
    def final_videos(self, obj: GenerationRun) -> str:
        return _final_videos_panel(obj)

    @admin.display(description="Publication prep")
    def publication_prep(self, obj: GenerationRun) -> str:
        return _publication_prep_panel(obj)


@admin.register(ContentSeries)
class ContentSeriesAdmin(admin.ModelAdmin):
    list_display = (
        "title_ru",
        "title_en",
        "current_video_index",
        "total_videos",
        "is_active",
        "updated_at",
        "run_count",
    )
    list_editable = ("current_video_index", "total_videos", "is_active")
    list_filter = ("is_active",)
    search_fields = ("title_ru", "title_en", "slug", "notes")
    readonly_fields = ("id", "created_at", "updated_at", "generation_runs")
    fields = (
        "id",
        "project_id",
        "slug",
        "title_ru",
        "title_en",
        "current_video_index",
        "total_videos",
        "is_active",
        "notes",
        "meta_json",
        "generation_runs",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Episode")
    def episode(self, obj: ContentSeries) -> str:
        return f"{obj.current_video_index}/{obj.total_videos}"

    @admin.display(description="Runs")
    def run_count(self, obj: ContentSeries) -> int:
        return GenerationRun.objects.filter(series_id=obj.id).count()

    @admin.display(description="Generation runs")
    def generation_runs(self, obj: ContentSeries) -> str:
        rows = GenerationRun.objects.filter(series_id=obj.id).order_by("-created_at")[:25]
        if not rows:
            return "No generation runs yet."
        items = [
            format_html(
                "<li>{} {} {}</li>",
                row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                _admin_change_link(row, row.human_title or str(row.id)),
                f"({row.series_video_index}/{row.series_total_videos})"
                if row.series_video_index and row.series_total_videos
                else "",
            )
            for row in rows
        ]
        return format_html("<ul>{}</ul>", mark_safe("".join(str(item) for item in items)))


class ScheduledGenerationImportForm(django_forms.Form):
    file = django_forms.FileField(label="Excel .xlsx file")


class ScheduledGenerationItemAdminForm(django_forms.ModelForm):
    generation_mode = django_forms.ChoiceField(
        label="Generation mode",
        required=False,
        choices=(
            (GENERATION_MODE_NORMAL, "Normal"),
            (GENERATION_MODE_SILENCE, "Silence / Kling RU-only"),
            (GENERATION_MODE_LONG, "Long / Horizontal RU-only"),
        ),
    )
    title_color = django_forms.CharField(
        label="Title color",
        required=False,
        widget=django_forms.TextInput(attrs={"type": "color"}),
    )
    comment = django_forms.CharField(
        label="Comment for viral plan",
        required=False,
        widget=django_forms.Textarea(attrs={"rows": 4}),
    )

    class Meta:
        model = ScheduledGenerationItem
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound:
            return
        payload = (
            self.instance.payload_json
            if self.instance is not None and isinstance(self.instance.payload_json, dict)
            else {}
        )
        self.fields["generation_mode"].initial = normalize_generation_mode(payload.get(GENERATION_MODE_SESSION_KEY))

    def clean_generation_mode(self) -> str:
        return normalize_generation_mode(self.cleaned_data.get("generation_mode"))

    def clean_title_color(self) -> str:
        normalized = _normalize_title_color(self.cleaned_data.get("title_color"))
        return normalized or SCHEDULED_DEFAULT_TITLE_COLOR

    def clean_source_row_key(self) -> str | None:
        return str(self.cleaned_data.get("source_row_key") or "").strip() or None

    def save(self, commit=True):
        obj = super().save(commit=False)
        payload = dict(obj.payload_json or {})
        mode = normalize_generation_mode(self.cleaned_data.get("generation_mode"))
        if mode in {GENERATION_MODE_SILENCE, GENERATION_MODE_LONG}:
            payload[GENERATION_MODE_SESSION_KEY] = mode
        else:
            payload.pop(GENERATION_MODE_SESSION_KEY, None)
        obj.payload_json = payload
        if commit:
            obj.save()
            self.save_m2m()
        return obj


class ScheduledGenerationEventInline(admin.TabularInline):
    model = ScheduledGenerationEvent
    extra = 0
    can_delete = False
    readonly_fields = ("created_at", "event_type", "stage", "status", "message", "generation_run", "payload_json")
    fields = readonly_fields
    ordering = ("-created_at",)
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.action(description="Mark selected items pending now")
def mark_scheduled_items_pending_now(modeladmin, request, queryset):
    now = timezone.now()
    updated = queryset.update(status="pending", run_at=now, error_text=None, claimed_at=None, started_at=None, finished_at=None)
    modeladmin.message_user(request, f"{updated} scheduled item(s) marked pending for immediate processing.", messages.SUCCESS)


@admin.action(description="Run selected scheduled items now")
def run_scheduled_items_now(modeladmin, request, queryset):
    results: list[str] = []
    for item in queryset.order_by("run_at", "created_at"):
        queued = queue_scheduled_generation_item_now(item.id)
        detail = f" ({queued.backend}: {queued.detail})" if queued.detail else f" ({queued.backend})"
        results.append(f"{item.title}: {queued.status}{detail}")
    level = messages.SUCCESS if results else messages.WARNING
    modeladmin.message_user(
        request,
        "Run-now complete.\n" + ("\n".join(results) if results else "No scheduled items selected."),
        level,
    )


@admin.register(ScheduledGenerationItem)
class ScheduledGenerationItemAdmin(admin.ModelAdmin):
    form = ScheduledGenerationItemAdminForm
    change_list_template = "admin/pipeline_admin/scheduledgenerationitem/change_list.html"
    list_display = (
        "run_at",
        "title_short",
        "status",
        "is_draft",
        "title_color",
        "overlay_subtitle_short",
        "generation_mode_short",
        "comment_short",
        "series",
        "source_row_key",
        "linked_run",
        "run_now_link",
        "error_short",
    )
    list_filter = ("status", "is_draft", "series")
    search_fields = ("title", "recipe", "comment", "overlay_subtitle", "series__title_ru", "series__title_en", "source_row_key", "source_file_path", "error_text")
    autocomplete_fields = ("series", "generation_run")
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "claimed_at",
        "started_at",
        "finished_at",
        "linked_run",
        "source_file_path",
        "payload_json",
    )
    fields = (
        "id",
        "title",
        "recipe",
        "comment",
        "overlay_subtitle",
        "generation_mode",
        "title_color",
        "run_at",
        "is_draft",
        "series",
        "status",
        "generation_run",
        "linked_run",
        "source_row_key",
        "source_file_path",
        "error_text",
        "payload_json",
        "claimed_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    actions = (mark_scheduled_items_pending_now, run_scheduled_items_now)
    inlines = (ScheduledGenerationEventInline,)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "download-template/",
                self.admin_site.admin_view(self.download_template_view),
                name="pipeline_admin_scheduledgenerationitem_download_template",
            ),
            path(
                "import-xlsx/",
                self.admin_site.admin_view(self.import_xlsx_view),
                name="pipeline_admin_scheduledgenerationitem_import_xlsx",
            ),
            path(
                "export-xlsx/",
                self.admin_site.admin_view(self.export_xlsx_view),
                name="pipeline_admin_scheduledgenerationitem_export_xlsx",
            ),
            path(
                "<path:object_id>/run-now/",
                self.admin_site.admin_view(self.run_now_view),
                name="pipeline_admin_scheduledgenerationitem_run_now",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra = dict(extra_context or {})
        extra["scheduled_generation_links"] = {
            "template": reverse("admin:pipeline_admin_scheduledgenerationitem_download_template"),
            "import": reverse("admin:pipeline_admin_scheduledgenerationitem_import_xlsx"),
            "export": reverse("admin:pipeline_admin_scheduledgenerationitem_export_xlsx"),
        }
        return super().changelist_view(request, extra_context=extra)

    @admin.display(description="Title")
    def title_short(self, obj: ScheduledGenerationItem) -> str:
        return _short(obj.title, 80)

    @admin.display(description="Comment")
    def comment_short(self, obj: ScheduledGenerationItem) -> str:
        return _short(obj.comment, 60)

    @admin.display(description="Subtitle")
    def overlay_subtitle_short(self, obj: ScheduledGenerationItem) -> str:
        return _short(obj.overlay_subtitle, 60)

    @admin.display(description="Mode")
    def generation_mode_short(self, obj: ScheduledGenerationItem) -> str:
        payload = obj.payload_json if isinstance(obj.payload_json, dict) else {}
        mode = normalize_generation_mode(payload.get(GENERATION_MODE_SESSION_KEY))
        if mode == GENERATION_MODE_SILENCE:
            return "Silence"
        if mode == GENERATION_MODE_LONG:
            return "Long"
        return "Normal"

    @admin.display(description="Run")
    def linked_run(self, obj: ScheduledGenerationItem) -> str:
        run = obj.generation_run
        if run is None:
            return ""
        return _admin_change_link(run, run.human_title or str(run.id))

    @admin.display(description="Error")
    def error_short(self, obj: ScheduledGenerationItem) -> str:
        return _short(obj.error_text, 90)

    @admin.display(description="Run now")
    def run_now_link(self, obj: ScheduledGenerationItem) -> str:
        if not obj.pk:
            return ""
        url = reverse("admin:pipeline_admin_scheduledgenerationitem_run_now", args=[obj.pk])
        return format_html('<a href="{}">run now</a>', url)

    def download_template_view(self, request):
        data = build_scheduled_generation_template_bytes()
        response = HttpResponse(
            data,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="scheduled_generation_template.xlsx"'
        return response

    def export_xlsx_view(self, request):
        data = export_scheduled_generation_items_to_xlsx(self.get_queryset(request).order_by("run_at", "created_at"))
        response = HttpResponse(
            data,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="scheduled_generation_export.xlsx"'
        return response

    def import_xlsx_view(self, request):
        if request.method == "POST":
            form = ScheduledGenerationImportForm(request.POST, request.FILES)
            if form.is_valid():
                upload = form.cleaned_data["file"]
                try:
                    result = import_scheduled_generation_xlsx(upload, original_name=getattr(upload, "name", "upload.xlsx"))
                except Exception as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                else:
                    level = messages.WARNING if result.errors else messages.SUCCESS
                    message = (
                        f"Import complete: created={result.created}, updated={result.updated}, "
                        f"skipped={result.skipped}. Saved copy: {result.saved_path}"
                    )
                    if result.errors:
                        message += "\n" + "\n".join(result.errors[:10])
                    self.message_user(request, message, level)
                    return HttpResponseRedirect(reverse("admin:pipeline_admin_scheduledgenerationitem_changelist"))
        else:
            form = ScheduledGenerationImportForm()
        csrf = get_token(request)
        html = (
            "<h1>Import scheduled generation Excel .xlsx</h1>"
            "<p>Upload an .xlsx file with columns: title, recipe, comment, overlay_subtitle, title_color, run_at, is_draft, source_row_key.</p>"
            f'<form method="post" enctype="multipart/form-data">'
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{escape(csrf)}">'
            f"{form.as_p()}"
            '<button type="submit" class="default">Import .xlsx</button>'
            "</form>"
            f'<p><a href="{reverse("admin:pipeline_admin_scheduledgenerationitem_changelist")}">Back to queue</a></p>'
        )
        return HttpResponse(html)

    def run_now_view(self, request, object_id):
        obj = self.get_queryset(request).filter(pk=object_id).first()
        if obj is None:
            raise Http404("Scheduled generation item not found.")
        try:
            queued = queue_scheduled_generation_item_now(obj.id)
        except Exception as exc:
            self.message_user(request, f"{obj.title}: run-now failed: {exc}", messages.ERROR)
        else:
            detail = f" ({queued.backend}: {queued.detail})" if queued.detail else f" ({queued.backend})"
            self.message_user(request, f"{obj.title}: queued, status={queued.status}{detail}", messages.SUCCESS)
        return HttpResponseRedirect(reverse("admin:pipeline_admin_scheduledgenerationitem_changelist"))


@admin.register(ScheduledGenerationEvent)
class ScheduledGenerationEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "scheduled_item", "event_type", "stage", "status", "message_short", "generation_run")
    list_filter = ("event_type", "stage", "status", "created_at")
    search_fields = ("scheduled_item__title", "scheduled_item__recipe", "message", "generation_run__human_title", "generation_run__id")
    readonly_fields = ("id", "scheduled_item", "generation_run", "event_type", "stage", "status", "message", "payload_json", "created_at")
    fields = readonly_fields

    @admin.display(description="Message")
    def message_short(self, obj: ScheduledGenerationEvent) -> str:
        return _short(obj.message, 120)


@admin.register(GenerationDraft)
class GenerationDraftAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title_short", "chat_id", "stage", "scene_index", "substep", "language", "linked_run")
    list_filter = ("stage", "substep", "language")
    search_fields = ("title", "chat_id", "run__human_title", "run__id")
    readonly_fields = ("id", "created_at", "linked_run", "session_snapshot_json", "payload_json")
    fields = (
        "id",
        "title",
        "chat_id",
        "run",
        "linked_run",
        "stage",
        "scene_index",
        "substep",
        "language",
        "session_snapshot_json",
        "payload_json",
        "created_at",
    )

    @admin.display(description="Title")
    def title_short(self, obj: GenerationDraft) -> str:
        return _short(obj.title, 90)

    @admin.display(description="Run")
    def linked_run(self, obj: GenerationDraft) -> str:
        run = obj.run
        if run is None:
            return ""
        return _admin_change_link(run, run.human_title or str(run.id))


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("slug", "title", "owner", "is_default", "is_active", "updated_at")
    list_editable = ("is_default", "is_active")
    list_filter = ("is_default", "is_active")
    search_fields = ("slug", "title", "description", "owner__normalized_username", "owner__telegram_id")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(ProjectPublishAccount)
class ProjectPublishAccountAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "platform",
        "language",
        "account_key",
        "external_account_id",
        "publish_policy",
        "direct_api_status",
        "is_active",
        "updated_at",
    )
    list_editable = ("external_account_id", "publish_policy", "is_active")
    list_filter = ("platform", "language", "is_active", "publish_policy")
    search_fields = ("project__slug", "project__title", "platform", "language", "account_key", "external_account_id")
    raw_id_fields = ("project",)
    readonly_fields = ("id", "created_at", "updated_at")

    @admin.display(description="Direct API")
    def direct_api_status(self, obj: ProjectPublishAccount) -> str:
        readiness = platform_readiness(obj.project.slug, obj.platform, obj.language)
        if readiness.ready:
            return "ready"
        return readiness.note or f"missing {len(readiness.missing)} setting(s)"


@admin.action(description="Publish selected jobs now through official APIs")
def publish_selected_publication_jobs(modeladmin, request, queryset):
    job_ids = [str(job_id) for job_id in queryset.values_list("id", flat=True)]
    if not job_ids:
        modeladmin.message_user(request, "No publication jobs selected.", level=messages.WARNING)
        return
    try:
        from .tasks import publish_publication_jobs_task

        publish_publication_jobs_task.delay(job_ids)
    except Exception as exc:
        modeladmin.message_user(request, f"Direct API worker was not queued: {exc}", level=messages.ERROR)
        return
    modeladmin.message_user(request, f"Queued {len(job_ids)} publication job(s) for direct API delivery.", level=messages.SUCCESS)


@admin.register(PublicationJob)
class PublicationJobAdmin(admin.ModelAdmin):
    change_list_template = "admin/pipeline_admin/publicationjob/change_list.html"
    list_display = (
        "created_at",
        "platform",
        "language",
        "status",
        "video_label",
        "title_short",
        "project",
        "attempt_count",
    )
    list_editable = ("status",)
    list_filter = ("status", "platform", "language", "publish_policy")
    search_fields = ("bundle_key", "title", "description", "video_path", "external_post_id", "publish_error")
    raw_id_fields = ("project",)
    readonly_fields = ("id", "created_at", "updated_at", "payload_json")
    actions = (publish_selected_publication_jobs,)
    fields = (
        "id",
        "project",
        "bundle_key",
        "origin",
        "platform",
        "language",
        "account_key",
        "external_account_id",
        "status",
        "video_path",
        "video_label",
        "title",
        "description",
        "hashtags_json",
        "recipe",
        "publish_policy",
        "payload_json",
        "scheduled_at",
        "approved_at",
        "published_at",
        "external_post_id",
        "publish_error",
        "attempt_count",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Title")
    def title_short(self, obj: PublicationJob) -> str:
        return _short(obj.title or obj.description, 80)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "prepare/",
                self.admin_site.admin_view(self.prepare_view),
                name="pipeline_admin_publicationjob_prepare",
            ),
            path(
                "vk-oauth/",
                self.admin_site.admin_view(self.vk_oauth_view),
                name="pipeline_admin_publicationjob_vk_oauth",
            ),
            path(
                "vk-oauth/callback/",
                self.admin_site.admin_view(self.vk_oauth_callback_view),
                name="pipeline_admin_publicationjob_vk_oauth_callback",
            ),
            path(
                "youtube-oauth/",
                self.admin_site.admin_view(self.youtube_oauth_view),
                name="pipeline_admin_publicationjob_youtube_oauth",
            ),
            path(
                "youtube-oauth/callback/",
                self.admin_site.admin_view(self.youtube_oauth_callback_view),
                name="pipeline_admin_publicationjob_youtube_oauth_callback",
            ),
            path(
                "tiktok-oauth/",
                self.admin_site.admin_view(self.tiktok_oauth_view),
                name="pipeline_admin_publicationjob_tiktok_oauth",
            ),
            path(
                "tiktok-oauth/callback/",
                self.admin_site.admin_view(self.tiktok_oauth_callback_view),
                name="pipeline_admin_publicationjob_tiktok_oauth_callback",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra = dict(extra_context or {})
        extra["prepare_publication_url"] = reverse("admin:pipeline_admin_publicationjob_prepare")
        extra["vk_oauth_url"] = reverse("admin:pipeline_admin_publicationjob_vk_oauth")
        extra["youtube_oauth_url"] = reverse("admin:pipeline_admin_publicationjob_youtube_oauth")
        extra["tiktok_oauth_url"] = reverse("admin:pipeline_admin_publicationjob_tiktok_oauth")
        return super().changelist_view(request, extra_context=extra)

    def youtube_oauth_view(self, request):
        project_choices = _publication_project_choices()
        initial_project = "grandma_kitchen" if any(key == "grandma_kitchen" for key, _ in project_choices) else (
            project_choices[0][0] if project_choices else ""
        )
        initial_language = "ru"
        initial_client_id = (
            project_env(initial_project, "YOUTUBE_CLIENT_ID_RU")
            or project_env(initial_project, "YOUTUBE_CLIENT_ID")
            if initial_project
            else ""
        )
        form = YouTubeOAuthSetupAdminForm(
            request.POST or None,
            project_choices=project_choices,
            initial={
                "project_slug": initial_project,
                "language": initial_language,
                "client_id": initial_client_id,
            },
        )
        callback_url = request.build_absolute_uri(
            reverse("admin:pipeline_admin_publicationjob_youtube_oauth_callback")
        )
        if request.method == "POST" and form.is_valid():
            project_slug = str(form.cleaned_data["project_slug"])
            language = str(form.cleaned_data["language"]).lower()
            suffix = language.upper()
            client_id = str(form.cleaned_data["client_id"]).strip()
            client_secret = str(form.cleaned_data["client_secret"]).strip()
            state = uuid.uuid4().hex + uuid.uuid4().hex
            request.session[f"youtube_oauth:{state}"] = {
                "project_slug": project_slug,
                "language": language,
                "client_id": client_id,
                "redirect_uri": callback_url,
                "created_at": int(time.time()),
            }
            request.session.modified = True
            set_project_secrets(
                project_slug,
                {
                    f"YOUTUBE_CLIENT_ID_{suffix}": client_id,
                    f"YOUTUBE_CLIENT_SECRET_{suffix}": client_secret,
                    f"YOUTUBE_REDIRECT_URI_{suffix}": callback_url,
                },
            )
            return HttpResponseRedirect(
                build_youtube_authorize_url(
                    client_id=client_id,
                    redirect_uri=callback_url,
                    state=state,
                )
            )
        csrf_token = get_token(request)
        html = format_html(
            '<!doctype html><html><head><title>Connect YouTube publishing</title></head><body>'
            '<div id="container"><div id="content" class="colM"><h1>Connect YouTube publishing API</h1>'
            '<p>Create a Google OAuth client of type <strong>Web application</strong>, enable YouTube Data API v3, '
            'and add this exact authorized redirect URI:</p><p><code>{}</code></p>'
            '<p>The connection requests only <code>youtube.upload</code> and <code>youtube.readonly</code>. '
            'The returned channel ID is checked and stored separately for RU and EN.</p>'
            '<form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="{}">{}'
            '<button type="submit" class="default">Continue to Google</button></form>'
            '<p><a href="{}">Back to publication jobs</a></p></div></div></body></html>',
            callback_url,
            csrf_token,
            mark_safe(form.as_p()),
            reverse("admin:pipeline_admin_publicationjob_changelist"),
        )
        return HttpResponse(html)

    def youtube_oauth_callback_view(self, request):
        state = str(request.GET.get("state") or "")
        flow = request.session.pop(f"youtube_oauth:{state}", None) if state else None
        request.session.modified = True
        if not isinstance(flow, dict) or int(time.time()) - int(flow.get("created_at") or 0) > 900:
            return HttpResponse("YouTube authorization session is missing or expired.", status=400)
        error = str(request.GET.get("error_description") or request.GET.get("error") or "").strip()
        if error:
            return HttpResponse(f"YouTube authorization was not completed: {escape(error)}", status=400)
        code = str(request.GET.get("code") or "").strip()
        if not code:
            return HttpResponse("Google did not return an authorization code.", status=400)
        project_slug = str(flow["project_slug"])
        language = str(flow["language"]).lower()
        suffix = language.upper()
        try:
            tokens = exchange_youtube_code(
                client_id=str(flow["client_id"]),
                client_secret=(
                    project_env(project_slug, f"YOUTUBE_CLIENT_SECRET_{suffix}")
                    or project_env(project_slug, "YOUTUBE_CLIENT_SECRET")
                ),
                redirect_uri=str(flow["redirect_uri"]),
                code=code,
            )
            channels = youtube_channels(tokens.access_token)
            expected_channel_id = project_env(project_slug, f"YOUTUBE_CHANNEL_ID_{suffix}")
            channel_ids = {row["id"] for row in channels}
            if expected_channel_id and expected_channel_id not in channel_ids:
                raise ValueError(
                    f"Google OAuth returned channels {sorted(channel_ids) or ['none']}, expected {expected_channel_id}."
                )
            if not expected_channel_id:
                if len(channels) != 1:
                    raise ValueError(
                        f"Expected exactly one YouTube channel, got {[(row['id'], row['title']) for row in channels]}."
                    )
                expected_channel_id = channels[0]["id"]
            set_project_secrets(
                project_slug,
                {
                    f"YOUTUBE_REFRESH_TOKEN_{suffix}": tokens.refresh_token,
                    f"YOUTUBE_CHANNEL_ID_{suffix}": expected_channel_id,
                },
            )
        except Exception as exc:
            return HttpResponse(f"YouTube connection failed: {escape(str(exc))}", status=400)
        channel_title = next(
            (row["title"] for row in channels if row["id"] == expected_channel_id),
            expected_channel_id,
        )
        messages.success(
            request,
            f"YouTube {language.upper()} connected for {project_slug}: {channel_title} ({expected_channel_id}).",
        )
        return HttpResponseRedirect(reverse("admin:pipeline_admin_publicationjob_changelist"))

    def tiktok_oauth_view(self, request):
        project_choices = _publication_project_choices()
        initial_project = "grandma_kitchen" if any(key == "grandma_kitchen" for key, _ in project_choices) else (
            project_choices[0][0] if project_choices else ""
        )
        form = TikTokOAuthSetupAdminForm(
            request.POST or None,
            project_choices=project_choices,
            initial={
                "project_slug": initial_project,
                "client_key": project_env(initial_project, "TIKTOK_CLIENT_KEY") if initial_project else "",
            },
        )
        callback_path = reverse("admin:pipeline_admin_publicationjob_tiktok_oauth_callback")
        callback_url = f"http://localhost:8000{callback_path}"
        if request.method == "POST" and form.is_valid():
            project_slug = str(form.cleaned_data["project_slug"])
            client_key = str(form.cleaned_data["client_key"]).strip()
            client_secret = str(form.cleaned_data["client_secret"]).strip()
            verifier, challenge = new_tiktok_pkce_pair()
            state = uuid.uuid4().hex + uuid.uuid4().hex
            request.session[f"tiktok_oauth:{state}"] = {
                "project_slug": project_slug,
                "client_key": client_key,
                "redirect_uri": callback_url,
                "code_verifier": verifier,
                "created_at": int(time.time()),
            }
            request.session.modified = True
            set_project_secrets(
                project_slug,
                {
                    "TIKTOK_CLIENT_KEY": client_key,
                    "TIKTOK_CLIENT_SECRET": client_secret,
                    "TIKTOK_REDIRECT_URI": callback_url,
                },
            )
            return HttpResponseRedirect(
                build_tiktok_authorize_url(
                    client_key=client_key,
                    redirect_uri=callback_url,
                    state=state,
                    code_challenge=challenge,
                )
            )
        csrf_token = get_token(request)
        html = format_html(
            '<!doctype html><html><head><title>Connect TikTok publishing</title></head><body>'
            '<div id="container"><div id="content" class="colM"><h1>Connect TikTok publishing API</h1>'
            '<p>Use a TikTok Developer app with the <strong>Desktop</strong> platform, Login Kit, and Content Posting API. '
            'Add this exact redirect URI:</p><p><code>{}</code></p>'
            '<p>Request approval for <code>video.publish</code>. Until the app passes TikTok audit, direct posts are restricted '
            'to <code>SELF_ONLY</code>.</p>'
            '<form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="{}">{}'
            '<button type="submit" class="default">Continue to TikTok</button></form>'
            '<p><a href="{}">Back to publication jobs</a></p></div></div></body></html>',
            callback_url,
            csrf_token,
            mark_safe(form.as_p()),
            reverse("admin:pipeline_admin_publicationjob_changelist"),
        )
        return HttpResponse(html)

    def tiktok_oauth_callback_view(self, request):
        state = str(request.GET.get("state") or "")
        flow = request.session.pop(f"tiktok_oauth:{state}", None) if state else None
        request.session.modified = True
        if not isinstance(flow, dict) or int(time.time()) - int(flow.get("created_at") or 0) > 900:
            return HttpResponse("TikTok authorization session is missing or expired.", status=400)
        error = str(request.GET.get("error_description") or request.GET.get("error") or "").strip()
        if error:
            return HttpResponse(f"TikTok authorization was not completed: {escape(error)}", status=400)
        code = str(request.GET.get("code") or "").strip()
        if not code:
            return HttpResponse("TikTok did not return an authorization code.", status=400)
        project_slug = str(flow["project_slug"])
        try:
            tokens = exchange_tiktok_code(
                client_key=str(flow["client_key"]),
                client_secret=project_env(project_slug, "TIKTOK_CLIENT_SECRET"),
                redirect_uri=str(flow["redirect_uri"]),
                code=code,
                code_verifier=str(flow["code_verifier"]),
            )
            expected_open_id = project_env(project_slug, "TIKTOK_OPEN_ID")
            if expected_open_id and tokens.open_id != expected_open_id:
                raise ValueError(
                    f"TikTok token belongs to {tokens.open_id}, expected {expected_open_id}."
                )
            user = tiktok_user(tokens.access_token)
            if user.get("open_id") and user["open_id"] != tokens.open_id:
                raise ValueError("TikTok user lookup returned a different open_id.")
            set_project_secrets(
                project_slug,
                {
                    "TIKTOK_ACCESS_TOKEN": tokens.access_token,
                    "TIKTOK_REFRESH_TOKEN": tokens.refresh_token,
                    "TIKTOK_OPEN_ID": tokens.open_id,
                },
            )
        except Exception as exc:
            return HttpResponse(f"TikTok connection failed: {escape(str(exc))}", status=400)
        messages.success(
            request,
            f"TikTok publishing connected for {project_slug}: {user.get('display_name') or tokens.open_id}.",
        )
        return HttpResponseRedirect(reverse("admin:pipeline_admin_publicationjob_changelist"))

    def vk_oauth_view(self, request):
        project_choices = _publication_project_choices()
        initial_project = "grandma_kitchen" if any(key == "grandma_kitchen" for key, _ in project_choices) else (
            project_choices[0][0] if project_choices else ""
        )
        initial_client_id = project_env(initial_project, "VK_CLIENT_ID") if initial_project else ""
        form = VKOAuthSetupAdminForm(
            request.POST or None,
            project_choices=project_choices,
            initial={"project_slug": initial_project, "client_id": initial_client_id},
        )
        request_callback_url = request.build_absolute_uri(
            reverse("admin:pipeline_admin_publicationjob_vk_oauth_callback")
        )
        request_callback_parts = urlparse(request_callback_url)
        if request_callback_parts.hostname in {"localhost", "127.0.0.1", "::1"}:
            request_callback_url = f"http://localhost{request_callback_parts.path}"
        callback_url = project_env(initial_project, "VK_REDIRECT_URI") or request_callback_url
        if request.method == "POST" and form.is_valid():
            project_slug = str(form.cleaned_data["project_slug"])
            client_id = str(form.cleaned_data["client_id"]).strip()
            callback_url = project_env(project_slug, "VK_REDIRECT_URI") or request_callback_url
            verifier, challenge = new_pkce_pair()
            state = uuid.uuid4().hex + uuid.uuid4().hex
            request.session[f"vk_oauth:{state}"] = {
                "project_slug": project_slug,
                "client_id": client_id,
                "redirect_uri": callback_url,
                "code_verifier": verifier,
                "created_at": int(time.time()),
            }
            request.session.modified = True
            set_project_secrets(
                project_slug,
                {"VK_CLIENT_ID": client_id, "VK_REDIRECT_URI": callback_url},
            )
            return HttpResponseRedirect(
                build_authorize_url(
                    client_id=client_id,
                    redirect_uri=callback_url,
                    state=state,
                    code_challenge=challenge,
                )
            )
        csrf_token = get_token(request)
        html = format_html(
            '<!doctype html><html><head><title>Connect VK publishing</title></head><body>'
            '<div id="container"><div id="content" class="colM"><h1>Connect VK publishing API</h1>'
            '<p><strong>Use a VK ID application with the Web platform.</strong> A VK Mini App ID will not work. '
            'Create it in the <a href="https://id.vk.com/about/business/go" target="_blank" rel="noopener">VK ID application cabinet</a>. '
            'Protected and service keys are not used here.</p>'
            '<p>Add this exact redirect URL to the trusted redirect URLs of that VK ID Web application:</p>'
            '<p><code>{}</code></p>'
            '<p>The authorization requests only <code>video</code>, <code>groups</code>, and <code>wall</code>. '
            'The returned user and managed community are checked against the selected project settings before any token is saved.</p>'
            '<form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="{}">{}'
            '<button type="submit" class="default">Continue to VK ID</button></form>'
            '<p><a href="{}">Back to publication jobs</a></p></div></div></body></html>',
            callback_url,
            csrf_token,
            mark_safe(form.as_p()),
            reverse("admin:pipeline_admin_publicationjob_changelist"),
        )
        return HttpResponse(html)

    def vk_oauth_callback_view(self, request):
        state = str(request.GET.get("state") or "")
        flow_key = f"vk_oauth:{state}"
        flow = request.session.pop(flow_key, None) if state else None
        request.session.modified = True
        if not isinstance(flow, dict) or int(time.time()) - int(flow.get("created_at") or 0) > 900:
            return HttpResponse("VK authorization session is missing or expired.", status=400)
        error = str(request.GET.get("error_description") or request.GET.get("error") or "").strip()
        if error:
            return HttpResponse(f"VK authorization was not completed: {escape(error)}", status=400)
        code = str(request.GET.get("code") or "").strip()
        device_id = str(request.GET.get("device_id") or "").strip()
        if not code or not device_id:
            return HttpResponse("VK did not return code/device_id.", status=400)
        project_slug = str(flow["project_slug"])
        try:
            tokens = exchange_authorization_code(
                client_id=str(flow["client_id"]),
                redirect_uri=str(flow["redirect_uri"]),
                code=code,
                code_verifier=str(flow["code_verifier"]),
                device_id=device_id,
                state=state,
            )
            _verify_vk_identity(
                project_slug,
                token=tokens.access_token,
                group_id=project_env(project_slug, "VK_GROUP_ID_RU"),
                api_version=os.getenv("VK_API_VERSION", "5.199"),
            )
            set_project_secrets(
                project_slug,
                {
                    "VK_ACCESS_TOKEN": tokens.access_token,
                    "VK_REFRESH_TOKEN": tokens.refresh_token,
                    "VK_DEVICE_ID": tokens.device_id,
                },
            )
        except Exception as exc:
            return HttpResponse(f"VK connection failed: {escape(str(exc))}", status=400)
        messages.success(request, f"VK publishing API connected for project {project_slug}.")
        return HttpResponseRedirect(reverse("admin:pipeline_admin_publicationjob_changelist"))

    def prepare_view(self, request):
        run_id = str(request.GET.get("run_id") or "").strip()
        if run_id:
            run = GenerationRun.objects.filter(id=run_id).first()
            if run is None:
                raise Http404("Generation run not found.")
            return HttpResponseRedirect(reverse("admin:pipeline_admin_generationrun_queue_publications", args=[run.id]))
        runs = (
            GenerationRun.objects.filter(project__is_active=True)
            .exclude(project__slug__startswith="guest-")
            .select_related("project")
            .order_by("-created_at")[:100]
        )
        rows = []
        for run in runs:
            url = reverse("admin:pipeline_admin_publicationjob_prepare") + f"?run_id={run.id}"
            rows.append(
                format_html(
                    '<tr><td>{}</td><td>{}</td><td>{:%Y-%m-%d %H:%M}</td><td><a class="button" href="{}">Choose video / upload own MP4</a></td></tr>',
                    run.project.title if run.project else "No project",
                    run.human_title or _short(run.script, 100),
                    run.created_at,
                    url,
                )
            )
        back_url = reverse("admin:pipeline_admin_publicationjob_changelist")
        html = format_html(
            '<!doctype html><html><head><title>Prepare publication</title></head><body class="app-pipeline_admin">'
            '<div id="container"><div id="content" class="colM"><h1>Prepare video publication</h1>'
            '<p>Choose a generation as the description context. On the next page you can use any historical build or upload your own MP4, and must choose the target project.</p>'
            '<p><a href="{}">Back to publication jobs</a></p>'
            '<table><thead><tr><th>Project</th><th>Generation</th><th>Created</th><th></th></tr></thead><tbody>{}</tbody></table>'
            '</div></div></body></html>',
            back_url,
            mark_safe("".join(str(row) for row in rows)),
        )
        return HttpResponse(html)


@admin.register(VideoOverview)
class VideoOverviewAdmin(admin.ModelAdmin):
    list_display = ("display_title", "status", "language", "series", "created_at", "scene_count", "artifact_count")
    search_fields = ("human_title", "script", "id", "series__title_ru", "series__title_en")
    list_filter = ("status", "language", "series")
    actions = (build_final_title_and_videos,)
    readonly_fields = ("id", "created_at", "script", "media_approval_overview", "video_map")
    fields = ("id", "human_title", "status", "language", "created_at", "media_approval_overview", "script", "video_map")

    @admin.display(description="Video")
    def display_title(self, obj: VideoOverview) -> str:
        return obj.human_title or _short(obj.script, 100) or str(obj.id)

    @admin.display(description="Scenes")
    def scene_count(self, obj: VideoOverview) -> int:
        return Scene.objects.filter(run_id=obj.id).count()

    @admin.display(description="Artifacts")
    def artifact_count(self, obj: VideoOverview) -> int:
        return GenerationArtifact.objects.filter(run_id=obj.id).count()

    @admin.display(description="Approved media by scene")
    def media_approval_overview(self, obj: VideoOverview) -> str:
        return _media_approval_overview(obj.id)

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
                        f"{_generation_artifact_link(artifact, 'open')}"
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
                    f"{_generation_artifact_link(artifact, 'open')}"
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
    actions = (build_final_title_and_videos,)
    readonly_fields = ("created_at", "updated_at", "checkpoint_timeline")
    fieldsets = (
        ("Session", {"fields": ("chat_id", "state", "generation_run_id", "created_at", "updated_at")}),
        ("Pipeline text", {"fields": ("selected_concept", "viral_concept_brief", "recipe_full_text")}),
        (
            "Plans",
            {
                "fields": (
                    "scenes_plan_json",
                    "production_plan_json",
                    "overlay_title_by_lang_json",
                    "overlay_title_style_json",
                )
            },
        ),
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
        (
            "Current media step",
            {
                "description": (
                    "To move the bot to a concrete scene step, fill these fields, check "
                    '"Apply media step override", then save. Scene number is 1-based.'
                ),
                "fields": (
                    "apply_media_step_override",
                    "media_scene_number",
                    "media_substep",
                    "media_language_index",
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
    form = SceneAdminForm
    list_display = ("scene_order", "run_link", "duration_sec", "short_text")
    list_filter = ("run__language", "run__status")
    search_fields = ("text", "run__human_title", "run__script", "run__id")
    readonly_fields = ("id", "run_link", "artifacts", "images", "videos", "text_history")
    fields = (
        "id",
        "run",
        "run_link",
        "scene_order",
        "duration_sec",
        "text",
        "start_frame_prompt",
        "start_frame_prompt_effective",
        "end_frame_prompt",
        "video_prompt_by_lang",
        "speech_by_lang",
        "syntax_prompt_by_lang",
        "prompt_meta_json",
        "production_scene_json",
        "text_history",
        "artifacts",
        "images",
        "videos",
    )
    raw_id_fields = ("run",)

    def get_form(self, request, obj=None, change=False, **kwargs):
        form_class = super().get_form(request, obj, change, **kwargs)
        production_scene = _latest_scene_payload_for_admin(obj) if obj is not None else {}

        class BoundSceneAdminForm(form_class):
            def __init__(self, *args, **form_kwargs):
                form_kwargs.setdefault("production_scene", production_scene)
                super().__init__(*args, **form_kwargs)

        return BoundSceneAdminForm

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.GET.get("run__status__exact") == ARCHIVED_TEST_RUN_STATUS:
            return queryset
        return queryset.exclude(run__status=ARCHIVED_TEST_RUN_STATUS)

    @admin.display(description="Run")
    def run_link(self, obj: Scene) -> str:
        return _admin_change_link(obj.run, "open video")

    @admin.display(description="Text")
    def short_text(self, obj: Scene) -> str:
        return _short(obj.text)

    def save_model(self, request, obj: Scene, form, change):
        old_run_id = None
        old_scene = None
        if change and obj.pk:
            old_scene = Scene.objects.filter(pk=obj.pk).first()
            old_run_id = old_scene.run_id if old_scene is not None else None
        super().save_model(request, obj, form, change)
        if old_scene is not None:
            _record_scene_text_revision(
                obj,
                field_name="text",
                old_text=old_scene.text,
                new_text=obj.text,
                request=request,
                source="scene_admin",
            )
            old_speech = old_scene.speech_by_lang if isinstance(old_scene.speech_by_lang, dict) else {}
            new_speech = obj.speech_by_lang if isinstance(obj.speech_by_lang, dict) else {}
            for lang in sorted(set(old_speech) | set(new_speech)):
                _record_scene_text_revision(
                    obj,
                    field_name="speech",
                    language=str(lang).strip().lower() or None,
                    old_text=old_speech.get(lang, ""),
                    new_text=new_speech.get(lang, ""),
                    request=request,
                    source="scene_admin",
                )
        payload = form.cleaned_data.get("production_scene_json") if hasattr(form, "cleaned_data") else None
        payloads = {str(obj.id): payload} if isinstance(payload, dict) and payload else {}
        if old_run_id and old_run_id != obj.run_id:
            _renumber_scenes_for_run(old_run_id)
            _sync_chat_sessions_for_run_scenes(old_run_id)
        _renumber_scenes_for_run(obj.run_id)
        _sync_chat_sessions_for_run_scenes(obj.run_id, override_payloads_by_scene_id=payloads)

    @admin.display(description="Text history")
    def text_history(self, obj: Scene) -> str:
        if not obj.pk:
            return "Save the scene first."
        rows = SceneTextRevision.objects.filter(scene_id=obj.id).order_by("-created_at")[:30]
        return _scene_text_revision_table(rows)

    def delete_model(self, request, obj: Scene):
        run_id = obj.run_id
        super().delete_model(request, obj)
        _renumber_scenes_for_run(run_id)
        _sync_chat_sessions_for_run_scenes(run_id)

    def delete_queryset(self, request, queryset):
        run_ids = {row.run_id for row in queryset}
        super().delete_queryset(request, queryset)
        for run_id in run_ids:
            _renumber_scenes_for_run(run_id)
            _sync_chat_sessions_for_run_scenes(run_id)

    @admin.display(description="Artifacts")
    def artifacts(self, obj: Scene) -> str:
        rows = GenerationArtifact.objects.filter(scene_id=obj.id).order_by("created_at")
        if not rows:
            return "No artifacts yet."
        items = [
            format_html(
                "<li>{} <code>{}</code> {}</li>",
                row.label,
                row.artifact_type,
                _generation_artifact_link(row, "open"),
            )
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


@admin.register(SceneTextRevision)
class SceneTextRevisionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "run", "scene_order", "field_name", "language", "source", "actor", "short_new_text")
    list_filter = ("field_name", "language", "source", "created_at")
    search_fields = ("run__human_title", "run__id", "old_text", "new_text", "actor")
    readonly_fields = (
        "id",
        "run",
        "scene",
        "scene_order",
        "field_name",
        "language",
        "old_text",
        "new_text",
        "source",
        "actor",
        "meta_json",
        "created_at",
    )
    fields = readonly_fields

    @admin.display(description="New text")
    def short_new_text(self, obj: SceneTextRevision) -> str:
        return _short(obj.new_text, 120)


@admin.register(GenerationArtifact)
class GenerationArtifactAdmin(admin.ModelAdmin):
    class FinalBuildsFilter(admin.SimpleListFilter):
        title = "final builds"
        parameter_name = "final_builds"

        def lookups(self, request, model_admin):
            return (("1", "Final builds only"),)

        def queryset(self, request, queryset):
            if self.value() == "1":
                return queryset.filter(artifact_type__in=FINAL_VIDEO_ARTIFACT_TYPES)
            return queryset

    list_display = ("label", "artifact_type", "language", "run", "scene", "uri_link", "created_at")
    list_filter = (FinalBuildsFilter, "artifact_type", "language", "mime_type")
    search_fields = ("label", "uri", "run__id", "run__human_title", "run__script")
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

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.GET.get("run__status__exact") == ARCHIVED_TEST_RUN_STATUS:
            return queryset
        return queryset.exclude(run__status=ARCHIVED_TEST_RUN_STATUS)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/media/",
                self.admin_site.admin_view(self.media_view),
                name="pipeline_admin_generationartifact_media",
            )
        ]
        return custom + urls

    def media_view(self, request, object_id):
        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404("Artifact not found.")
        raw = str(obj.uri or "")
        if raw.startswith("db://scene_image_candidates/"):
            candidate_id = raw.rsplit("/", 1)[-1]
            row = SceneImageCandidate.objects.filter(candidate_id=candidate_id).first()
            return _candidate_media_response(request, row, blob_attr="image_blob", default_content_type="image/png")
        if raw.startswith("db://scene_video_candidates/"):
            candidate_id = raw.rsplit("/", 1)[-1]
            row = SceneVideoCandidate.objects.filter(candidate_id=candidate_id).first()
            return _candidate_media_response(request, row, blob_attr="video_blob", default_content_type="video/mp4")
        if raw.startswith(("http://", "https://")):
            return HttpResponseRedirect(raw)
        path_obj = _artifact_local_path(obj)
        if path_obj is None or not path_obj.is_file():
            raise Http404("Artifact file not found.")
        content_type = obj.mime_type or mimetypes.guess_type(path_obj.name)[0] or "application/octet-stream"
        return _local_file_response(path_obj, content_type=content_type, request=request)

    @admin.display(description="URI")
    def uri_link(self, obj: GenerationArtifact) -> str:
        return _generation_artifact_link(obj)

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
        media_url = _artifact_media_url(obj)
        if str(obj.mime_type or "").startswith("image/") and media_url:
            return format_html('<img src="{}" style="max-width:420px;max-height:320px;border-radius:4px;">', media_url)
        if str(obj.mime_type or "").startswith("video/") and media_url:
            return format_html('<video src="{}" controls preload="none" style="max-width:520px;width:100%;"></video>', media_url)
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
        return _candidate_media_response(request, row, blob_attr="image_blob", default_content_type="image/png")

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
        return _candidate_media_response(request, row, blob_attr="video_blob", default_content_type="video/mp4")

    @admin.display(description="Preview")
    def preview(self, obj: SceneVideoCandidate) -> str:
        return _video_preview(obj)

    @admin.display(description="Video")
    def watch(self, obj: SceneVideoCandidate) -> str:
        media_url = reverse("admin:pipeline_admin_scenevideocandidate_media", args=[obj.candidate_id])
        return format_html('<a href="{}" target="_blank" rel="noopener">open video</a>', media_url)
