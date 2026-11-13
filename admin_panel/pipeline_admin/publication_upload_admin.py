"""Polished unified publication upload workflow for the Django admin."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe

from app.db import SessionLocal
from app.publication_config import project_env
from app.publications import (
    apply_project_publication_copy_rules,
    build_publication_targets,
    publication_artifacts_dir,
    publication_jobs_summary,
    publication_target_label,
    queue_publication_bundle,
    strip_instagram_ru_attribution,
)
from app.social_publishers import platform_readiness

from . import admin as legacy
from .models import ChatSession, GenerationArtifact, GenerationRun, Project, ProjectPublishAccount, PublicationJob


MAX_COVER_BYTES = 50 * 1024 * 1024
PUBLICATION_TRIAL_COPY_DRAFT_ARTIFACT_TYPE = "publication_trial_copy_draft"
TARGET_ICONS = {
    "youtube": "▶",
    "instagram": "◎",
    "vk": "VK",
    "tiktok": "♪",
}
TIKTOK_PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Публично для всех",
    "MUTUAL_FOLLOW_FRIENDS": "Друзья (взаимные подписки)",
    "FOLLOWER_OF_CREATOR": "Подписчики автора",
    "SELF_ONLY": "Только я",
}


class PublicationUploadForm(legacy.PublicationQueueAdminForm):
    """One form for a generated video or a completely custom upload."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        submitted_privacy = str(self.data.get("tiktok_privacy_level") or "") if self.is_bound else ""
        privacy_choices = [("", "Сначала загрузите настройки аккаунта")]
        if submitted_privacy:
            privacy_choices.append(
                (submitted_privacy, TIKTOK_PRIVACY_LABELS.get(submitted_privacy, submitted_privacy))
            )
        self.fields["tiktok_privacy_level"] = forms.ChoiceField(
            required=False,
            label="Кто увидит видео",
            choices=privacy_choices,
            help_text="Список загружается напрямую из TikTok Creator Info; значение по умолчанию не выбирается.",
        )
        for name, label, help_text in (
            ("tiktok_allow_comment", "Разрешить комментарии", "Будет отключено, если аккаунт TikTok запрещает комментарии."),
            ("tiktok_allow_duet", "Разрешить Duet", "Будет отключено, если аккаунт или приватность запрещают Duet."),
            ("tiktok_allow_stitch", "Разрешить Stitch", "Будет отключено, если аккаунт или приватность запрещают Stitch."),
            ("tiktok_brand_content", "Продвигает сторонний бренд", "Commercial content disclosure: branded content."),
            ("tiktok_brand_organic", "Продвигает свой бренд", "Commercial content disclosure: your brand."),
            ("tiktok_is_aigc", "Содержит AI-generated content", "Передаётся TikTok как признак AI-generated content."),
        ):
            self.fields[name] = forms.BooleanField(
                required=False,
                label=label,
                help_text=help_text,
                initial=(name == "tiktok_is_aigc"),
            )
        self.fields["tiktok_music_usage_confirmed"] = forms.BooleanField(
            required=False,
            label="Подтверждаю права на музыку и звук",
            help_text="Обязательно для публикации: в видео нет чужой музыки либо у проекта есть права на её использование.",
        )
        self.fields["tiktok_publish_confirmed"] = forms.BooleanField(
            required=False,
            label="Просмотрела видео и описание, разрешаю публикацию в выбранный TikTok",
            help_text="Видео будет передано выбранному провайдеру публикации. Проверьте аккаунт и приватность выше.",
        )
        self.fields["instagram_ru_trial"] = forms.BooleanField(
            required=False,
            label="Сначала пробный Reel, затем обычный",
            help_text="Будут созданы две последовательные публикации с разными описаниями.",
        )
        self.fields["instagram_ru_trial_description"] = forms.CharField(
            required=False,
            label="Отдельное описание для пробного Reel",
            help_text="Генерируется отдельно. Строка про Syntx сюда не добавляется.",
            widget=forms.Textarea(attrs={"rows": 6}),
        )
        self.fields["project_slug"].label = "Проект и набор аккаунтов"
        self.fields["target_keys"].label = "Куда публиковать"
        self.fields["scheduled_at"].label = "Когда публиковать"
        for language, label in (("ru", "Русское видео"), ("en", "English video")):
            source = self.fields[f"video_source_{language}"]
            source.label = "Версия видео"
            source.choices = (("", "Не выбирать видео"), *tuple(source.choices))
            source.widget = forms.RadioSelect(choices=source.choices)
            self.fields[f"custom_video_{language}"].label = f"Загрузить свой MP4 · {label}"
            self.fields[f"cover_{language}"] = forms.FileField(
                required=False,
                label=f"Обложка · {label}",
                widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,.jpg,.jpeg,.png"}),
                help_text="Сформируйте кадр около 1-й секунды кнопкой ниже или загрузите свой JPG/PNG до 50 МБ.",
            )
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.pop("style", None)
                field.widget.attrs["class"] = "cf-upload-textarea"
                value = self.data.get(name) if self.is_bound else self.initial.get(name, field.initial or "")
                lines = str(value or "").splitlines() or [""]
                estimated_rows = sum(max(1, (len(line) // 72) + 1) for line in lines) + 1
                field.widget.attrs["rows"] = max(7, estimated_rows)
            elif isinstance(field.widget, (forms.Select, forms.DateTimeInput)):
                field.widget.attrs["class"] = "cf-upload-control"

    def clean(self):
        cleaned = super().clean()
        selected = {str(value) for value in cleaned.get("target_keys") or []}
        for target_key in selected:
            for field_name in legacy.SOCIAL_COPY_FIELDS_BY_TARGET.get(target_key, ()):
                if not str(cleaned.get(field_name) or "").strip():
                    self.add_error(field_name, "Заполните поле для выбранной площадки.")
        if (
            "instagram:ru" in selected
            and cleaned.get("instagram_ru_trial")
            and not str(cleaned.get("instagram_ru_trial_description") or "").strip()
        ):
            self.add_error(
                "instagram_ru_trial_description",
                "Сгенерируйте или заполните отдельное описание для пробного Reel.",
            )
        if "tiktok:ru" in selected:
            if not cleaned.get("tiktok_publish_confirmed"):
                self.add_error("tiktok_publish_confirmed", "Подтвердите просмотр видео и согласие на публикацию.")
            if not str(cleaned.get("tiktok_privacy_level") or "").strip():
                self.add_error(
                    "tiktok_privacy_level",
                    "Загрузите настройки аккаунта и явно выберите приватность TikTok.",
                )
            if not cleaned.get("tiktok_music_usage_confirmed"):
                self.add_error(
                    "tiktok_music_usage_confirmed",
                    "Подтвердите права на музыку и звук перед публикацией в TikTok.",
                )
        for language in ("ru", "en"):
            cover = cleaned.get(f"cover_{language}")
            if cover is None:
                continue
            suffix = Path(str(getattr(cover, "name", ""))).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png"}:
                self.add_error(f"cover_{language}", "Обложка должна быть JPG или PNG.")
            if int(getattr(cover, "size", 0) or 0) > MAX_COVER_BYTES:
                self.add_error(f"cover_{language}", "Обложка больше 50 МБ.")
        return cleaned


def _copy_payload_from_form(form: PublicationUploadForm, fallback: dict[str, str]) -> dict[str, str]:
    if form.is_bound:
        return {
            key: str(form.data.get(key) or "")
            for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS
        }
    return {
        key: str(form.initial.get(key) or fallback.get(key) or "")
        for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS
    }


def _selected_target_keys(form: PublicationUploadForm) -> set[str]:
    raw = form["target_keys"].value() or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(value) for value in raw}


def _target_cards(form: PublicationUploadForm, copy_payload: dict[str, str]) -> list[dict[str, Any]]:
    selected = _selected_target_keys(form)
    project_slug = str(form["project_slug"].value() or "")
    account_index: dict[tuple[str, str], ProjectPublishAccount] = {}
    try:
        project = Project.objects.filter(slug=project_slug).first()
        if project is not None:
            rows = ProjectPublishAccount.objects.filter(project_id=project.id, is_active=True)
            account_index = {(row.platform, row.language): row for row in rows}
    except Exception:
        pass
    cards: list[dict[str, Any]] = []
    for target in build_publication_targets(copy_payload):
        try:
            readiness = platform_readiness(project_slug, target.platform, target.language)
        except Exception:
            readiness = None
        account = account_index.get((target.platform, target.language))
        fields = [form[name] for name in legacy.SOCIAL_COPY_FIELDS_BY_TARGET.get(target.key, ())]
        cards.append(
            {
                "key": target.key,
                "platform": target.platform,
                "language": target.language,
                "label": publication_target_label(target),
                "icon": TARGET_ICONS.get(target.platform, "•"),
                "checked": target.key in selected,
                "fields": fields,
                "trial_field": form["instagram_ru_trial"] if target.key == "instagram:ru" else None,
                "trial_description_field": (
                    form["instagram_ru_trial_description"] if target.key == "instagram:ru" else None
                ),
                "tiktok_fields": (
                    tuple(
                        form[name]
                        for name in (
                            "tiktok_privacy_level",
                            "tiktok_allow_comment",
                            "tiktok_allow_duet",
                            "tiktok_allow_stitch",
                            "tiktok_brand_content",
                            "tiktok_brand_organic",
                            "tiktok_is_aigc",
                            "tiktok_music_usage_confirmed",
                            "tiktok_publish_confirmed",
                        )
                    )
                    if target.key == "tiktok:ru"
                    else ()
                ),
                "ready": bool(readiness and readiness.ready),
                "provider_note": readiness.note if readiness else "",
                "account": (
                    "@" + project_env(project_slug, "ZERNIO_TIKTOK_USERNAME").lstrip("@")
                    if target.platform == "tiktok" and project_env(project_slug, "TIKTOK_PROVIDER") == "zernio"
                    else str(account.external_account_id or "") if account else ""
                ),
                "missing": tuple(readiness.missing) if readiness and not readiness.ready else (),
            }
        )
    return cards


def _upload_page_context(
    request,
    modeladmin,
    *,
    form: PublicationUploadForm,
    copy_payload: dict[str, str],
    run: GenerationRun | None = None,
    recipe: str = "",
    generation_feedback: str = "",
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    admin_site = getattr(modeladmin, "admin_site", None)
    if admin_site is not None and hasattr(request, "user"):
        context.update(admin_site.each_context(request))
    video_previews = _publication_video_previews(run) if run is not None else {"ru": (), "en": ()}
    context.update(
        {
            "title": "Заливка видео",
            "subtitle": (
                f"Из генерации: {run.human_title or run.id}"
                if run is not None
                else "Свои видео, тексты и площадки"
            ),
            "opts": PublicationJob._meta,
            "form": form,
            "target_cards": _target_cards(form, copy_payload),
            "language_panels": (
                {
                    "code": "ru",
                    "title": "Русская версия",
                    "hint": "Одно видео для всех выбранных русских площадок",
                    "cover_hint": "YouTube установит файл как обложку. Для остальных площадок он сохранится в задании; их текущие API не принимают отдельное локальное изображение.",
                    "source": form["video_source_ru"],
                    "upload": form["custom_video_ru"],
                    "cover": form["cover_ru"],
                    "video_previews": video_previews["ru"],
                },
                {
                    "code": "en",
                    "title": "English version",
                    "hint": "One video for all selected English destinations",
                    "cover_hint": "YouTube will apply this thumbnail. Other current APIs do not accept a separate local cover, so the file is kept with the job.",
                    "source": form["video_source_en"],
                    "upload": form["custom_video_en"],
                    "cover": form["cover_en"],
                    "video_previews": video_previews["en"],
                },
            ),
            "run": run,
            "recipe": recipe,
            "generation_feedback": generation_feedback,
            "back_url": (
                reverse("admin:pipeline_admin_generationrun_change", args=[run.pk])
                if run is not None
                else reverse("admin:index")
            ),
            "jobs_url": reverse("admin:pipeline_admin_publicationjob_changelist"),
            "tiktok_creator_info_url": reverse(
                "admin:pipeline_admin_publicationjob_tiktok_creator_info"
            ),
        }
    )
    return context


def _store_cover_upload(upload, *, scope: str, language: str) -> Path:
    suffix = Path(str(upload.name)).suffix.lower()
    target_dir = publication_artifacts_dir() / "admin_uploads" / scope
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}_{language}_cover{suffix}"
    with target.open("wb") as handle:
        for chunk in upload.chunks():
            handle.write(chunk)
    return target


def _publication_video_previews(run: GenerationRun) -> dict[str, tuple[dict[str, str], ...]]:
    result: dict[str, list[dict[str, str]]] = {"ru": [], "en": []}
    for artifact in legacy._publication_all_final_artifacts(run):
        language = str(artifact.language or "").strip().lower()
        if language not in result:
            continue
        result[language].append(
            {
                "source": f"artifact:{artifact.id}",
                "url": legacy._artifact_media_url(artifact),
                "label": str(artifact.label or "Final video"),
            }
        )
    return {language: tuple(rows) for language, rows in result.items()}


def _post_production_and_covers(
    form: PublicationUploadForm,
    *,
    run: GenerationRun | None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str], list[Path]]:
    artifacts = (
        {f"artifact:{row.id}": row for row in legacy._publication_all_final_artifacts(run)}
        if run is not None
        else {}
    )
    selected_languages = {
        str(key).split(":", 1)[1]
        for key in form.cleaned_data.get("target_keys", [])
        if ":" in str(key)
    }
    renders: dict[str, dict[str, dict[str, str]]] = {}
    labels: dict[str, str] = {}
    covers: dict[str, str] = {}
    staging: list[Path] = []
    scope = str(run.id) if run is not None else f"standalone-{uuid.uuid4()}"
    for language in selected_languages:
        source_key = str(form.cleaned_data.get(f"video_source_{language}") or "")
        if source_key == "upload":
            path = legacy._store_publication_custom_upload(
                form.cleaned_data[f"custom_video_{language}"],
                run_id=scope,
                language=language,
            )
            staging.append(path)
            payload = {"video_path": str(path)}
            labels[language] = f"{language.upper()} · custom upload · {path.name}"
        else:
            artifact = artifacts.get(source_key)
            if artifact is None:
                raise ValueError(f"Выбранная версия {language.upper()} больше недоступна.")
            payload = legacy._publication_artifact_video_payload(artifact)
            if not payload:
                raise ValueError(f"Файл выбранной версии {language.upper()} недоступен.")
            created = artifact.created_at.strftime("%Y-%m-%d %H:%M:%S") if artifact.created_at else ""
            labels[language] = f"{language.upper()} · {artifact.label} · {created}"
        renders[language] = {"clean": payload}
        cover = form.cleaned_data.get(f"cover_{language}")
        if cover is not None:
            cover_path = _store_cover_upload(cover, scope=scope, language=language)
            staging.append(cover_path)
            covers[language] = str(cover_path)
    return {"renders_by_language": renders}, labels, covers, staging


def _queue_form_jobs(
    form: PublicationUploadForm,
    *,
    run: GenerationRun | None,
    recipe: str,
) -> list[Any]:
    post_production, source_labels, cover_sources, staging = _post_production_and_covers(form, run=run)
    copy_payload = form.copy_payload()
    trial_copy = {
        "instagram_ru": str(form.cleaned_data.get("instagram_ru_trial_description") or "")
    }
    try:
        with SessionLocal() as db:
            jobs = queue_publication_bundle(
                db,
                project_slug=str(form.cleaned_data["project_slug"]),
                recipe=(f"{run.id}\n\n{recipe}" if run is not None else recipe),
                copy=copy_payload,
                post_production=post_production,
                origin="admin_generation_run" if run is not None else "admin_custom_upload",
                selected_target_keys=form.cleaned_data["target_keys"],
                video_variant="clean",
                video_source_labels=source_labels,
                cover_sources=cover_sources,
                trial_copy=trial_copy,
                trial_target_keys=(
                    ["instagram:ru"]
                    if form.cleaned_data.get("instagram_ru_trial")
                    else []
                ),
                scheduled_at=form.cleaned_data.get("scheduled_at"),
                source_run_id=str(run.id) if run is not None else None,
            )
            tiktok_options = {
                "tiktok_privacy_level": str(form.cleaned_data.get("tiktok_privacy_level") or ""),
                "tiktok_allow_comment": bool(form.cleaned_data.get("tiktok_allow_comment")),
                "tiktok_allow_duet": bool(form.cleaned_data.get("tiktok_allow_duet")),
                "tiktok_allow_stitch": bool(form.cleaned_data.get("tiktok_allow_stitch")),
                "tiktok_brand_content": bool(form.cleaned_data.get("tiktok_brand_content")),
                "tiktok_brand_organic": bool(form.cleaned_data.get("tiktok_brand_organic")),
                "tiktok_is_aigc": bool(form.cleaned_data.get("tiktok_is_aigc")),
                "tiktok_music_usage_confirmed": bool(
                    form.cleaned_data.get("tiktok_music_usage_confirmed")
                ),
                "tiktok_publish_confirmed": bool(form.cleaned_data.get("tiktok_publish_confirmed")),
            }
            for job in jobs:
                if str(job.platform or "").lower() == "tiktok":
                    job.payload_json = {**(job.payload_json or {}), **tiktok_options}
            if run is not None:
                for job in jobs:
                    job.payload_json = {**(job.payload_json or {}), "run_id": str(run.id)}
            db.commit()
        return jobs
    finally:
        for path in staging:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _dispatch_ready_jobs(jobs: list[Any]) -> str:
    ready_ids = [
        str(job.id)
        for job in jobs
        if job.status == "approved" and job.scheduled_at is None
    ]
    if not ready_ids:
        return ""
    try:
        from .tasks import dispatch_publication_jobs

        dispatch_publication_jobs(ready_ids)
    except Exception as exc:
        return f" Фоновая отправка не запущена: {type(exc).__name__}."
    return ""


def _publication_jobs_monitor_url(jobs: list[Any]) -> str:
    base_url = reverse("admin:pipeline_admin_publicationjob_changelist")
    if not jobs:
        return base_url
    bundle_key = str(getattr(jobs[0], "bundle_key", "") or "")
    root_bundle = bundle_key[:-6] if bundle_key.endswith("-trial") else bundle_key
    return (
        reverse("admin:pipeline_admin_publicationjob_monitor", args=[root_bundle])
        if root_bundle else base_url
    )


def _initial_project(project_choices: tuple[tuple[str, str], ...], preferred: str = "") -> str:
    keys = {key for key, _label in project_choices}
    if preferred in keys:
        return preferred
    if "grandma_kitchen" in keys:
        return "grandma_kitchen"
    return project_choices[0][0] if project_choices else ""


_original_publication_copy_from_run = legacy._publication_copy_from_run


def _copy_timestamp(value: Any) -> float:
    try:
        return float(value.timestamp())
    except Exception:
        return 0.0


def _freshest_publication_copy_from_run(run: GenerationRun) -> dict[str, str]:
    """Prefer the newest bot copy, admin draft, or queued publication bundle."""
    candidates: list[tuple[float, dict[str, str]]] = []
    try:
        sessions = ChatSession.objects.filter(generation_run_id=run.id).order_by("-updated_at")[:20]
        for session in sessions:
            payload = session.session_json if isinstance(session.session_json, dict) else {}
            for session_key in ("social_descriptions_pending", "social_descriptions_approved"):
                source = payload.get(session_key)
                copy = source.get("copy") if isinstance(source, dict) else None
                if isinstance(copy, dict) and any(str(value or "").strip() for value in copy.values()):
                    candidates.append(
                        (
                            _copy_timestamp(session.updated_at),
                            {key: str(copy.get(key) or "") for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS},
                        )
                    )
                    break
            if candidates:
                break
    except Exception:
        pass
    try:
        pending_rows = GenerationArtifact.objects.filter(
            run_id=run.id, artifact_type="run_pending_input",
            payload_json__session_key="social_descriptions_pending",
        ).order_by("-created_at")[:20]
        for artifact in pending_rows:
            payload = artifact.payload_json or {}
            if payload.get("session_key") != "social_descriptions_pending":
                continue
            pending = payload.get("pending") or {}
            copy = pending.get("copy") if isinstance(pending, dict) else None
            if isinstance(copy, dict) and any(copy.values()):
                candidates.append((_copy_timestamp(artifact.created_at), {
                    key: str(copy.get(key) or "") for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS
                }))
                break
    except Exception:
        pass
    try:
        draft = (
            GenerationArtifact.objects.filter(
                run_id=run.id,
                artifact_type__in=(
                    legacy.PUBLICATION_COPY_DRAFT_ARTIFACT_TYPE,
                    "publication_copy_approved",
                ),
            )
            .order_by("-created_at")
            .first()
        )
        payload = draft.payload_json if draft is not None and isinstance(draft.payload_json, dict) else {}
        copy = payload.get("copy") if isinstance(payload, dict) else None
        if isinstance(copy, dict) and any(str(value or "").strip() for value in copy.values()):
            candidates.append(
                (
                    _copy_timestamp(draft.created_at),
                    {key: str(copy.get(key) or "") for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS},
                )
            )
    except Exception:
        pass
    try:
        recent_jobs = list(PublicationJob.objects.order_by("-created_at")[:500])
        recipe_candidates = {str(getattr(run, "script", "") or "").strip()}
        for resolver in (legacy._publication_recipe_from_draft, legacy._publication_recipe_for_run):
            try:
                recipe_candidates.add(str(resolver(run) or "").strip())
            except Exception:
                pass
        recipe_candidates.discard("")
        run_jobs = [
            job
            for job in recent_jobs
            if (
                (
                    isinstance(job.payload_json, dict)
                    and str(job.payload_json.get("run_id") or "") == str(run.id)
                )
                or (
                    not str((job.payload_json or {}).get("run_id") or "").strip()
                    and str(job.recipe or "").strip() in recipe_candidates
                )
            )
            and not bool((job.payload_json or {}).get("instagram_trial"))
        ]
        if run_jobs:
            latest_bundle = str(run_jobs[0].bundle_key or "")
            if latest_bundle:
                run_jobs = [job for job in run_jobs if str(job.bundle_key or "") == latest_bundle]
            copy = (
                dict(max(candidates, key=lambda item: item[0])[1])
                if candidates
                else {key: "" for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS}
            )
            for job in run_jobs:
                platform = str(job.platform or "").lower()
                language = str(job.language or "").lower()
                if platform == "youtube" and language == "ru":
                    copy["youtube_ru_title"] = str(job.title or "")
                    copy["youtube_ru_description"] = str(job.description or "")
                elif platform == "instagram" and language == "ru":
                    copy["instagram_ru"] = str(job.description or "")
                elif platform == "vk" and language == "ru":
                    copy["vk_ru"] = str(job.description or "")
                elif platform == "tiktok" and language == "ru":
                    copy["tiktok_ru"] = str(job.description or "")
                elif platform == "instagram" and language == "en":
                    copy["instagram_en"] = str(job.description or "")
                elif platform == "youtube" and language == "en":
                    copy["youtube_en_title"] = str(job.title or "")
                    copy["youtube_en_description"] = str(job.description or "")
            if any(str(value or "").strip() for value in copy.values()):
                candidates.append((_copy_timestamp(run_jobs[0].created_at), copy))
    except Exception:
        pass
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return _original_publication_copy_from_run(run)


def _latest_instagram_ru_trial_description(run: GenerationRun) -> str:
    candidates: list[tuple[float, str]] = []
    try:
        draft = (
            GenerationArtifact.objects.filter(
                run_id=run.id,
                artifact_type=PUBLICATION_TRIAL_COPY_DRAFT_ARTIFACT_TYPE,
            )
            .order_by("-created_at")
            .first()
        )
        payload = draft.payload_json if draft is not None and isinstance(draft.payload_json, dict) else {}
        description = strip_instagram_ru_attribution(str(payload.get("description") or ""))
        if description:
            candidates.append((_copy_timestamp(draft.created_at), description))
    except Exception:
        pass
    try:
        for job in PublicationJob.objects.order_by("-created_at")[:500]:
            payload = job.payload_json if isinstance(job.payload_json, dict) else {}
            if (
                str(payload.get("run_id") or "") == str(run.id)
                and bool(payload.get("instagram_trial"))
                and str(job.platform or "").lower() == "instagram"
                and str(job.language or "").lower() == "ru"
            ):
                description = strip_instagram_ru_attribution(str(job.description or ""))
                if description:
                    candidates.append((_copy_timestamp(job.created_at), description))
                break
    except Exception:
        pass
    return max(candidates, key=lambda item: item[0])[1] if candidates else ""


def _save_instagram_ru_trial_draft(run: GenerationRun, description: str) -> None:
    clean = strip_instagram_ru_attribution(description)
    if not clean:
        return
    GenerationArtifact.objects.create(
        run_id=run.id,
        scene_id=None,
        artifact_type=PUBLICATION_TRIAL_COPY_DRAFT_ARTIFACT_TYPE,
        label="Instagram RU Trial copy draft",
        uri=f"db://generation_runs/{run.id}/publication_copy/instagram_ru_trial",
        language="ru",
        mime_type="application/json",
        payload_json={"description": clean},
        created_at=timezone.now(),
    )


def _generate_instagram_ru_trial_description(
    *,
    recipe: str,
    normal_copy: dict[str, str],
    feedback: str = "",
    project_slug: str | None = None,
) -> str:
    """Generate a distinct RU Trial Reel caption without the normal attribution."""
    from app.handlers.handlers_state_media import _refine_social_copy_from_feedback

    clean_recipe = str(recipe or "").strip()
    if not clean_recipe:
        raise ValueError("Для пробного описания нужен исходный текст или рецепт.")
    instruction = (
        "Отдельно создай НОВОЕ альтернативное описание только для Instagram RU Trial Reel. "
        "Оно должно отличаться от обычного Instagram RU описания, начинаться с сильного хука "
        "для новой аудитории и соответствовать рецепту. Не добавляй строку «Сделано с помощью "
        "@syntx_ai» и вообще не упоминай Syntx. Верни остальные поля без изменений."
    )
    if str(feedback or "").strip():
        instruction += f" Дополнительное пожелание пользователя: {str(feedback).strip()}"
    normal_description = strip_instagram_ru_attribution(
        str(normal_copy.get("instagram_ru") or "")
    ).casefold()
    for attempt in range(2):
        generated = asyncio.run(
            _refine_social_copy_from_feedback(
                recipe=clean_recipe,
                previous_copy=normal_copy,
                feedback=(
                    instruction
                    if attempt == 0
                    else instruction
                    + " Предыдущий вариант совпал с основным. Перепиши его полностью: другой хук, другая структура и формулировки."
                ),
                project_slug=project_slug,
            )
        )
        trial_description = strip_instagram_ru_attribution(
            str(generated.get("instagram_ru") or "")
        )
        if trial_description and trial_description.casefold() != normal_description:
            return trial_description
    raise ValueError("Генератор дважды вернул одинаковые основной и Trial тексты. Повторите генерацию.")


def standalone_upload_view(self, request):
    run_id = str(request.GET.get("run_id") or "").strip()
    if run_id:
        run = GenerationRun.objects.filter(id=run_id).first()
        if run is None:
            raise Http404("Generation run not found.")
        return HttpResponseRedirect(
            reverse("admin:pipeline_admin_generationrun_queue_publications", args=[run.id])
        )
    project_choices = legacy._publication_project_choices()
    initial_project = _initial_project(project_choices)
    copy_payload = apply_project_publication_copy_rules(
        initial_project,
        {key: "" for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS},
    )
    recipe = str(request.POST.get("publication_recipe") or "").strip() if request.method == "POST" else ""
    feedback = str(request.POST.get("generation_feedback") or "") if request.method == "POST" else ""
    selected_project = str(request.POST.get("project_slug") or initial_project)
    trial_description = str(request.POST.get("instagram_ru_trial_description") or "")
    if request.method == "POST" and "generate_descriptions" in request.POST:
        posted_copy = {
            key: str(request.POST.get(key) or "")
            for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS
        }
        try:
            copy_payload = legacy._generate_publication_copy_for_admin(
                None,
                current_copy=posted_copy,
                recipe=recipe,
                feedback=feedback,
                project_slug=selected_project,
            )
            copy_payload = apply_project_publication_copy_rules(selected_project, copy_payload)
            trial_description = _generate_instagram_ru_trial_description(
                recipe=recipe,
                normal_copy=copy_payload,
                feedback=feedback,
                project_slug=selected_project,
            )
        except Exception as exc:
            self.message_user(request, f"Описания не сгенерированы: {exc}", level=messages.ERROR)
            copy_payload = posted_copy
        else:
            self.message_user(
                request,
                "Все описания созданы отдельно, включая уникальный текст Trial Reel.",
                level=messages.SUCCESS,
            )
            feedback = ""
        form = PublicationUploadForm(
            None,
            copy=copy_payload,
            project_choices=project_choices,
            video_source_choices={"ru": (), "en": ()},
            initial={
                "project_slug": selected_project,
                "target_keys": request.POST.getlist("target_keys") or [
                    target.key for target in build_publication_targets(copy_payload) if target.description
                ],
                "video_variant": "clean",
                "video_source_ru": "upload",
                "video_source_en": "upload",
                "instagram_ru_trial": bool(request.POST.get("instagram_ru_trial")),
                "instagram_ru_trial_description": trial_description,
                **copy_payload,
            },
        )
    else:
        form = PublicationUploadForm(
            request.POST or None,
            request.FILES or None,
            copy=copy_payload,
            project_choices=project_choices,
            video_source_choices={"ru": (), "en": ()},
            initial={
                "project_slug": initial_project,
                "video_variant": "clean",
                "video_source_ru": "upload",
                "video_source_en": "upload",
            },
        )
    if request.method == "POST" and "generate_descriptions" not in request.POST and form.is_valid():
        try:
            jobs = _queue_form_jobs(
                form,
                run=None,
                recipe=recipe or "Ручная заливка из админки",
            )
        except Exception as exc:
            self.message_user(request, f"Публикации не созданы: {exc}", level=messages.ERROR)
        else:
            dispatch_error = _dispatch_ready_jobs(jobs)
            self.message_user(
                request,
                publication_jobs_summary(jobs) + dispatch_error,
                level=messages.SUCCESS if jobs and not dispatch_error else messages.WARNING,
            )
            return HttpResponseRedirect(_publication_jobs_monitor_url(jobs))
    displayed_copy = _copy_payload_from_form(form, copy_payload)
    response = TemplateResponse(
        request,
        "admin/pipeline_admin/publication_upload.html",
        _upload_page_context(
            request,
            self,
            form=form,
            copy_payload=displayed_copy,
            recipe=recipe,
            generation_feedback=feedback,
        ),
    )
    response.render()
    return response


def generation_upload_view(self, request, object_id):
    run = self.get_object(request, object_id)
    if run is None:
        raise Http404("Generation run not found.")
    project_choices = legacy._publication_project_choices()
    default_project = _initial_project(
        project_choices,
        legacy._project_slug_for_publication_run(run) or "",
    )
    source_choices = legacy._publication_video_source_choices(run)
    source_initials = legacy._publication_video_source_initials(source_choices)
    for language in ("ru", "en"):
        query_key = f"video_source_{language}"
        query_value = str(request.GET.get(query_key) or "")
        valid_values = {value for value, _label in source_choices.get(language, ())}
        if query_key in request.GET and (not query_value or query_value in valid_values):
            source_initials[query_key] = query_value

    if not legacy._publication_post_production_has_video(
        legacy._publication_post_production_from_final_artifacts(run)
    ):
        self.message_user(
            request,
            "Финальные версии ещё не найдены, но можно загрузить свой MP4.",
            level=messages.WARNING,
        )
    elif not legacy.all_final_artifacts_delivered(run.id):
        self.message_user(
            request,
            "Финальные видео ещё не подтверждены в Telegram; существующие файлы всё равно доступны.",
            level=messages.WARNING,
        )

    copy_payload = apply_project_publication_copy_rules(
        default_project,
        legacy._publication_copy_from_run(run),
    )
    if request.method == "GET" and not any(copy_payload.values()):
        self.message_user(
            request,
            "Описания для этого запуска не сохранены. Вставьте тексты из бота или "
            "сгенерируйте описания кнопкой ниже. Исходный сценарий не подставляется вместо них.",
            level=messages.WARNING,
        )
    trial_description = (
        str(request.POST.get("instagram_ru_trial_description") or "")
        if request.method == "POST"
        else _latest_instagram_ru_trial_description(run)
    )
    recipe = str(
        request.POST.get("publication_recipe")
        if request.method == "POST"
        else (legacy._publication_recipe_from_draft(run) or legacy._publication_recipe_for_run(run))
    ).strip()
    feedback = str(request.POST.get("generation_feedback") or "") if request.method == "POST" else ""
    if request.method == "POST" and "generate_descriptions" in request.POST:
        selected_project = str(request.POST.get("project_slug") or default_project)
        posted_copy = {
            key: str(request.POST.get(key) or copy_payload.get(key) or "")
            for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS
        }
        try:
            copy_payload = legacy._generate_publication_copy_for_admin(
                run,
                current_copy=posted_copy,
                recipe=recipe,
                feedback=feedback,
                project_slug=selected_project,
            )
            copy_payload = apply_project_publication_copy_rules(selected_project, copy_payload)
            trial_description = _generate_instagram_ru_trial_description(
                recipe=recipe,
                normal_copy=copy_payload,
                feedback=feedback,
                project_slug=selected_project,
            )
            _save_instagram_ru_trial_draft(run, trial_description)
            legacy._save_publication_copy_draft(
                run,
                copy_payload=copy_payload,
                recipe=recipe,
                feedback=feedback,
            )
        except Exception as exc:
            self.message_user(request, f"Описания не сгенерированы: {exc}", level=messages.ERROR)
            copy_payload = posted_copy
        else:
            self.message_user(request, "Описания созданы и сохранены. Проверьте их перед публикацией.", level=messages.SUCCESS)
            feedback = ""
        form = PublicationUploadForm(
            None,
            copy=copy_payload,
            project_choices=project_choices,
            video_source_choices=source_choices,
            initial={
                "project_slug": selected_project,
                "target_keys": request.POST.getlist("target_keys") or [
                    target.key for target in build_publication_targets(copy_payload) if target.description
                ],
                "video_variant": "clean",
                "scheduled_at": request.POST.get("scheduled_at") or None,
                "instagram_ru_trial": bool(request.POST.get("instagram_ru_trial")),
                "instagram_ru_trial_description": trial_description,
                **source_initials,
                "video_source_ru": str(request.POST.get("video_source_ru") or source_initials.get("video_source_ru") or ""),
                "video_source_en": str(request.POST.get("video_source_en") or source_initials.get("video_source_en") or ""),
                **copy_payload,
            },
        )
    else:
        form = PublicationUploadForm(
            request.POST or None,
            request.FILES or None,
            copy=copy_payload,
            project_choices=project_choices,
            video_source_choices=source_choices,
            initial={
                "project_slug": default_project,
                "target_keys": [
                    target.key for target in build_publication_targets(copy_payload) if target.description
                ],
                "video_variant": "clean",
                "instagram_ru_trial_description": trial_description,
                **source_initials,
                **copy_payload,
            },
        )
    if request.method == "POST" and "generate_descriptions" not in request.POST and form.is_valid():
        try:
            _save_instagram_ru_trial_draft(
                run,
                str(form.cleaned_data.get("instagram_ru_trial_description") or ""),
            )
            legacy._save_publication_copy_draft(
                run,
                copy_payload=form.copy_payload(),
                recipe=recipe,
                feedback=feedback,
            )
            jobs = _queue_form_jobs(form, run=run, recipe=recipe)
        except Exception as exc:
            self.message_user(request, f"Публикации не созданы: {exc}", level=messages.ERROR)
        else:
            dispatch_error = _dispatch_ready_jobs(jobs)
            self.message_user(
                request,
                publication_jobs_summary(jobs) + dispatch_error,
                level=messages.SUCCESS if jobs and not dispatch_error else messages.WARNING,
            )
            return HttpResponseRedirect(_publication_jobs_monitor_url(jobs))

    displayed_copy = _copy_payload_from_form(form, copy_payload)
    response = TemplateResponse(
        request,
        "admin/pipeline_admin/publication_upload.html",
        _upload_page_context(
            request,
            self,
            form=form,
            copy_payload=displayed_copy,
            run=run,
            recipe=recipe,
            generation_feedback=feedback,
        ),
    )
    response.render()
    return response


_configured = False
_original_prep_panel = legacy._publication_prep_panel
_original_final_videos_panel = legacy._final_videos_panel


def _final_video_publish_chooser(run: GenerationRun) -> str:
    try:
        artifacts = legacy._publication_all_final_artifacts(run)
    except Exception:
        artifacts = []
    by_language: dict[str, list[Any]] = {"ru": [], "en": []}
    for artifact in artifacts:
        language = str(getattr(artifact, "language", "") or "").strip().lower()
        if language not in by_language or not legacy._publication_artifact_video_payload(artifact):
            continue
        by_language[language].append(artifact)
    if not any(by_language.values()):
        return ""

    sections: list[str] = []
    for language, title in (("ru", "Русская версия"), ("en", "English version")):
        cards: list[str] = [
            '<label class="cf-publish-none">'
            f'<input type="radio" name="cf_publish_source_{language}" value="" checked>'
            '<span><strong>Не выбирать видео этого языка</strong><br>'
            '<span class="cf-publish-meta">Можно загрузить свой MP4 уже на странице заливки.</span></span></label>'
        ]
        for artifact in by_language[language]:
            value = f"artifact:{artifact.id}"
            created = artifact.created_at.strftime("%Y-%m-%d %H:%M:%S") if artifact.created_at else ""
            variant = str(artifact.artifact_type or "").removeprefix("final_")
            variant_label = legacy.PUBLICATION_VIDEO_VARIANT_LABELS.get(variant, variant)
            preview = legacy._artifact_video_preview(artifact)
            open_link = legacy._generation_artifact_link(artifact, "Открыть файл")
            cards.append(
                '<article class="cf-publish-artifact">'
                f'<label class="cf-publish-artifact-choice"><input type="radio" '
                f'name="cf_publish_source_{language}" value="{escape(value)}">'
                '<span><strong>Использовать эту версию</strong><br>'
                f'<span class="cf-publish-meta">{escape(variant_label)} · {escape(str(artifact.label or ""))}'
                f'{" · " + escape(created) if created else ""}</span></span></label>'
                f'<div class="cf-publish-preview">{preview}</div>'
                f'<div class="cf-publish-open">{open_link}</div>'
                '</article>'
            )
        sections.append(
            f'<section class="cf-publish-language"><h3>{title}</h3>'
            + "".join(cards)
            + "</section>"
        )
    action = reverse("admin:pipeline_admin_generationrun_queue_publications", args=[run.pk])
    return mark_safe(
        '<style>'
        '.cf-final-publish-picker{margin:0 0 18px;padding:18px;border:1px solid var(--border-color);border-radius:14px;'
        'background:color-mix(in srgb,var(--body-bg) 92%,#7c3aed 8%)}'
        '.cf-final-publish-picker h2{margin:0 0 5px}.cf-publish-grid{display:grid;grid-template-columns:repeat(2,minmax(300px,1fr));gap:14px}'
        '.cf-publish-language{padding:14px;border:1px solid var(--border-color);border-radius:12px;background:var(--body-bg)}'
        '.cf-publish-language h3{margin:0 0 10px}.cf-publish-artifact{padding:11px;margin:0 0 10px;border:1px solid var(--border-color);border-radius:10px}'
        '.cf-publish-artifact:has(input:checked){border-color:#7c3aed;box-shadow:0 0 0 2px color-mix(in srgb,#7c3aed 24%,transparent)}'
        '.cf-publish-artifact-choice{display:grid;grid-template-columns:22px 1fr;gap:8px;cursor:pointer}.cf-publish-artifact-choice input{margin-top:2px}'
        '.cf-publish-none{display:grid;grid-template-columns:22px 1fr;gap:8px;padding:10px;margin:0 0 10px;border:1px dashed var(--border-color);border-radius:10px;cursor:pointer}'
        '.cf-publish-none:has(input:checked){border-color:#7c3aed;background:color-mix(in srgb,var(--body-bg) 92%,#7c3aed 8%)}'
        '.cf-publish-meta{color:var(--body-quiet-color);font-size:12px}.cf-publish-preview video{max-width:280px!important;margin:9px 0!important}'
        '.cf-publish-open{margin-top:5px}.cf-publish-action{margin:14px 0 0}.cf-publish-action .button{padding:9px 16px}'
        '@media(max-width:900px){.cf-publish-grid{grid-template-columns:1fr}}'
        '</style>'
        '<div class="cf-final-publish-picker" data-upload-url="' + escape(action) + '">'
        '<h2>Выберите готовое видео для заливки</h2>'
        '<p class="cf-publish-meta" style="margin:0 0 14px;">Все сохранённые версии доступны. Для каждого языка можно выбрать не больше одной версии.</p>'
        '<div class="cf-publish-grid">'
        + "".join(sections)
        + '</div><p class="cf-publish-action"><button class="button" type="button" id="cf-open-publication-upload">'
        'Перейти к заливке с выбранными версиями</button></p></div>'
        '<script>(function(){const root=document.querySelector(".cf-final-publish-picker");'
        'const button=document.getElementById("cf-open-publication-upload");if(!root||!button)return;'
        'button.addEventListener("click",function(){const params=new URLSearchParams();'
        '["ru","en"].forEach(function(lang){const selected=root.querySelector(`input[name="cf_publish_source_${lang}"]:checked`);'
        'if(selected)params.set("video_source_"+lang,selected.value);});'
        'window.location.href=root.dataset.uploadUrl+(params.toString()?"?"+params.toString():"");});})();</script>'
    )


def _final_videos_with_publish_choices(run: GenerationRun) -> str:
    chooser = _final_video_publish_chooser(run)
    return mark_safe(str(chooser) + str(_original_final_videos_panel(run)))


def _enhanced_prep_panel(run: GenerationRun) -> str:
    return _original_prep_panel(run)


def configure_publication_upload_admin() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    legacy.PublicationJobAdmin.prepare_view = standalone_upload_view
    legacy.GenerationRunAdmin.queue_publications_view = generation_upload_view
    legacy._publication_copy_from_run = _freshest_publication_copy_from_run
    legacy._final_videos_panel = _final_videos_with_publish_choices
    legacy._publication_prep_panel = _enhanced_prep_panel
    admin.site.index_template = "admin/pipeline_admin/index.html"


configure_publication_upload_admin()
