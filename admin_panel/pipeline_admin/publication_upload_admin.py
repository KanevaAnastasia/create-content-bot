"""Polished unified publication upload workflow for the Django admin."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe

from app.db import SessionLocal
from app.publications import (
    build_publication_targets,
    publication_artifacts_dir,
    publication_jobs_summary,
    publication_target_label,
    queue_publication_bundle,
)
from app.social_publishers import platform_readiness

from . import admin as legacy
from .models import GenerationRun, Project, ProjectPublishAccount, PublicationJob


MAX_COVER_BYTES = 50 * 1024 * 1024
TARGET_ICONS = {
    "youtube": "▶",
    "instagram": "◎",
    "vk": "VK",
    "tiktok": "♪",
}


class PublicationUploadForm(legacy.PublicationQueueAdminForm):
    """One form for a generated video or a completely custom upload."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
                help_text="Необязательно. JPG или PNG до 50 МБ.",
            )
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.pop("style", None)
                field.widget.attrs["class"] = "cf-upload-textarea"
            elif isinstance(field.widget, (forms.Select, forms.DateTimeInput)):
                field.widget.attrs["class"] = "cf-upload-control"

    def clean(self):
        cleaned = super().clean()
        selected = {str(value) for value in cleaned.get("target_keys") or []}
        for target_key in selected:
            for field_name in legacy.SOCIAL_COPY_FIELDS_BY_TARGET.get(target_key, ()):
                if not str(cleaned.get(field_name) or "").strip():
                    self.add_error(field_name, "Заполните поле для выбранной площадки.")
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
                "ready": bool(readiness and readiness.ready),
                "account": str(account.external_account_id or "") if account else "",
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
                },
                {
                    "code": "en",
                    "title": "English version",
                    "hint": "One video for all selected English destinations",
                    "cover_hint": "YouTube will apply this thumbnail. Other current APIs do not accept a separate local cover, so the file is kept with the job.",
                    "source": form["video_source_en"],
                    "upload": form["custom_video_en"],
                    "cover": form["cover_en"],
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
    try:
        with SessionLocal() as db:
            jobs = queue_publication_bundle(
                db,
                project_slug=str(form.cleaned_data["project_slug"]),
                recipe=(f"{run.id}\n\n{recipe}" if run is not None else recipe),
                copy=form.copy_payload(),
                post_production=post_production,
                origin="admin_generation_run" if run is not None else "admin_custom_upload",
                selected_target_keys=form.cleaned_data["target_keys"],
                video_variant="clean",
                video_source_labels=source_labels,
                cover_sources=cover_sources,
                scheduled_at=form.cleaned_data.get("scheduled_at"),
            )
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
        from .tasks import publish_publication_jobs_task

        publish_publication_jobs_task.delay(ready_ids)
    except Exception as exc:
        return f" Фоновая отправка не запущена: {type(exc).__name__}."
    return ""


def _initial_project(project_choices: tuple[tuple[str, str], ...], preferred: str = "") -> str:
    keys = {key for key, _label in project_choices}
    if preferred in keys:
        return preferred
    if "grandma_kitchen" in keys:
        return "grandma_kitchen"
    return project_choices[0][0] if project_choices else ""


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
    copy_payload = {key: "" for key, _label in legacy.SOCIAL_COPY_FIELD_LABELS}
    form = PublicationUploadForm(
        request.POST or None,
        request.FILES or None,
        copy=copy_payload,
        project_choices=project_choices,
        video_source_choices={"ru": (), "en": ()},
        initial={
            "project_slug": _initial_project(project_choices),
            "video_variant": "clean",
            "video_source_ru": "upload",
            "video_source_en": "upload",
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            jobs = _queue_form_jobs(form, run=None, recipe="Ручная заливка из админки")
        except Exception as exc:
            self.message_user(request, f"Публикации не созданы: {exc}", level=messages.ERROR)
        else:
            dispatch_error = _dispatch_ready_jobs(jobs)
            self.message_user(
                request,
                publication_jobs_summary(jobs) + dispatch_error,
                level=messages.SUCCESS if jobs and not dispatch_error else messages.WARNING,
            )
            return HttpResponseRedirect(reverse("admin:pipeline_admin_publicationjob_changelist"))
    displayed_copy = _copy_payload_from_form(form, copy_payload)
    response = TemplateResponse(
        request,
        "admin/pipeline_admin/publication_upload.html",
        _upload_page_context(request, self, form=form, copy_payload=displayed_copy),
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

    copy_payload = legacy._publication_copy_from_run(run)
    recipe = str(
        request.POST.get("publication_recipe")
        if request.method == "POST"
        else (legacy._publication_recipe_from_draft(run) or legacy._publication_recipe_for_run(run))
    ).strip()
    feedback = str(request.POST.get("generation_feedback") or "") if request.method == "POST" else ""
    if request.method == "POST" and "generate_descriptions" in request.POST:
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
            )
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
                "project_slug": str(request.POST.get("project_slug") or default_project),
                "target_keys": request.POST.getlist("target_keys") or [
                    target.key for target in build_publication_targets(copy_payload) if target.description
                ],
                "video_variant": "clean",
                "scheduled_at": request.POST.get("scheduled_at") or None,
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
                **source_initials,
                **copy_payload,
            },
        )
    if request.method == "POST" and "generate_descriptions" not in request.POST and form.is_valid():
        try:
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
            return HttpResponseRedirect(reverse("admin:pipeline_admin_publicationjob_changelist"))

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
    legacy._final_videos_panel = _final_videos_with_publish_choices
    legacy._publication_prep_panel = _enhanced_prep_panel
    admin.site.index_template = "admin/pipeline_admin/index.html"


configure_publication_upload_admin()
