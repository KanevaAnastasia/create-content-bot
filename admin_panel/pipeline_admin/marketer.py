"""Staff-only marketer page and bounded background report collection."""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse

from app.marketer import METRICS, MetricsUnavailable, collect_metrics, diagnose, marketing_brief, group_videos, youtube_history
from app.publication_config import project_env
from app.social_publishers import _youtube_access_token, _vk_access_token as _vk_marketer_token, _tiktok_access_token
from .models import ContentSeries, GenerationRun, Project, PublicationJob
from .marketer_display import format_brief, behaviour_summary, readable_plan
from app.marketer import collect_followers, collect_behaviour
from app.marketer_daily import MOSCOW, snapshot_history, dashboard, day_key, observation_time
from app.marketer_market import collect_market, parse_references
from app.marketer_strategy import early_controls, field_notes, pilot_plan, unique_videos
from app.marketer_video_review import review_sources
from app.marketer_screening import recommended_items, review_market
from app.project_modes import project_mode_labels


REPORT_DIR = Path(__file__).resolve().parents[2] / "runtime" / "marketer"


@contextmanager
def report_lock(project_id):
    key = str(uuid.UUID(str(project_id)))
    REPORT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (REPORT_DIR / f"{key}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield REPORT_DIR / f"{key}.json"


def read_report(project_id):
    with report_lock(project_id) as path:
        return json.loads(path.read_text()) if path.exists() else {}


def write_report(path, data):
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x") as handle:
            os.chmod(temp, 0o600)
            json.dump(data, handle, ensure_ascii=False)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def reserve_report(project_id, action):
    with report_lock(project_id) as path:
        data = json.loads(path.read_text()) if path.exists() else {}
        if data.get("state") in {"queued", "running"} and time.time() - data.get("started", 0) < 1200:
            return None
        if time.time() - data.get("finished", 0) < 60:
            return None
        task_id = uuid.uuid4().hex
        data.update(state="queued", started=time.time(), task_id=task_id, action=action, error="")
        write_report(path, data)
        return task_id


def update_report(project_id, task_id, **fields):
    with report_lock(project_id) as path:
        data = json.loads(path.read_text()) if path.exists() else {}
        if data.get("task_id") != task_id:
            return False
        data.update(fields)
        write_report(path, data)
        return True


def save_references(project_id, refs, revision):
    with report_lock(project_id) as path:
        report = json.loads(path.read_text()) if path.exists() else {}
        if revision != report.get("reference_revision", 0):
            return False
        if refs != report.get("references", []):
            report.update(references=refs, reference_revision=revision + 1)
            write_report(path, report)
        return True


def record_edition_review(project_id, edition_id, note):
    """Attach a live review to its exact saved edition under the report lock."""
    note = note.strip()
    if not note:
        raise ValueError("Пустая заметка ревьюера.")
    with report_lock(project_id) as path:
        report = json.loads(path.read_text()) if path.exists() else {}
        retained = {edition["id"] for edition in report.get("plan_history", [])}
        if edition_id not in retained:
            raise ValueError("Редакция отсутствует в истории этого проекта.")
        # Independent of the worker's plan_history field when a new plan is in progress.
        reviews = {key: value for key, value in report.get("edition_reviews", {}).items() if key in retained}
        reviews[edition_id] = note
        write_report(path, {**report, "edition_reviews": reviews})


def set_editor_provider(project_id, provider):
    if provider not in ("default", "openai"):
        raise ValueError("Неизвестный редактор")
    with report_lock(project_id) as path:
        report = json.loads(path.read_text()) if path.exists() else {}
        report["editor_provider"] = provider
        write_report(path, report)


def job_dict(job):
    payload = job.payload_json or {}
    url = payload.get("external_post_url") or ""
    if job.platform == "youtube" and job.external_post_id:
        url = f"https://www.youtube.com/watch?v={job.external_post_id}"
    if not str(url).startswith("https://"):
        url = ""
    return {"id": str(job.id), "project_id": str(job.project_id), "project_slug": job.project.slug,
            "bundle_key": str(job.bundle_key or ""),
            "platform": job.platform, "language": job.language, "account_key": job.account_key,
            "instagram_trial": bool(payload.get("instagram_trial")),
            "title": job.title or (job.description or "Без названия")[:140],
            "posted_at": job.published_at.isoformat() if job.published_at else None,
            "external_post_id": job.external_post_id, "url": url,
            "caption": (job.description or "")[:1200], "run_id": payload.get("run_id")}


def attach_series(project, rows):
    run_ids = set()
    for row in rows:
        try:
            run_ids.add(uuid.UUID(str(row.get("run_id"))))
        except ValueError:
            continue
    if not run_ids:
        return
    runs = {str(run.id): run for run in GenerationRun.objects.filter(project=project, id__in=run_ids)
            .select_related("series").only("id", "workflow_type", "series__title_ru", "series__project_id")}
    for row in rows:
        run = runs.get(str(row.get("run_id")))
        if run:
            row["mode"] = run.workflow_type
            if run.series and run.series.project_id == project.id:
                row["series"] = run.series.title_ru


def public_tokens(slug):
    cache = {}

    def get(platform, lang):
        key = (platform, lang)
        if key not in cache:
            try:
                cache[key] = (_youtube_access_token(slug, lang) if platform == "youtube" else _vk_marketer_token(slug))
            except Exception:
                cache[key] = ""
        if not cache[key]:
            raise MetricsUnavailable(f"{platform} {lang.upper()}. Нет доступа к поиску этого проекта.")
        return cache[key]
    return get


def build_strategy(project, task_id, query="", language="ru", refresh_market=True):
    report = read_report(project.id)
    if report.get("task_id") != task_id:
        return
    rows = unique_videos(report.get("rows", []), report.get("analytics_exclusions", {}))
    refs = report.get("references", [])
    languages = [lang for lang in ("ru", "en") if any(r.get("language") == lang for r in rows)
                 or project_env(project.slug, "YOUTUBE_CHANNEL_ID_" + lang.upper())]
    if not (languages or refs or query) and not rows:
        update_report(project.id, task_id, strategy_notice="Пока нет публикаций, подключённых каналов или ссылок для анализа.")
        return
    profile = {"provider": report.get("editor_provider", "default"), "slug": project.slug, "title": project.title, "description": project.description or "",
               "series": list(ContentSeries.objects.filter(project_id=project.id, is_active=True).values_list("title_ru", flat=True)[:30]),
               "modes": project_mode_labels(project.slug), "languages": languages, "fetched_at": report.get("fetched_at")}
    overview = dashboard(report, rows)
    profile["comparison_day"] = overview.get("comparison_day")
    controls = early_controls(rows, report.get("daily_snapshots", []))
    for row in rows:
        row["control_48h"] = controls.get(row["id"])
    if report.get("brief") and isinstance(report["brief"], dict):
        profile["previous_decision"] = report["brief"].get("summary", "")
    revision = report.get("reference_revision", 0)
    market = report.get("market") or {}
    if refresh_market or not market.get("at") or revision != report.get("market_reference_revision"):
        market = collect_market(profile, rows, refs, market, public_tokens(project.slug), query=query, language=language)
    if not update_report(project.id, task_id, market=market, market_reference_revision=revision, strategy_notice=""):
        return
    screening = review_market(profile, market, report.get("market_reviews", {}))
    recommendations = recommended_items(market)
    studied_market = {**market, "items": recommendations}
    if not update_report(project.id, task_id, market_reviews=screening, market=market):
        return
    studies = review_sources(studied_market, rows, report.get("video_studies", {}))
    profile.update(favorites=market["favorites"], studies=studies)
    notes = field_notes(profile, rows, studied_market)
    backup = pilot_plan(profile, notes)
    if not update_report(project.id, task_id, video_studies=studies, editorial_notes=notes, pilot=backup):
        return
    history = report.get("plan_history", [])
    def edition(plan, provider, stamp):
        return (history + [{"id": task_id, "at": stamp, "day": day_key(stamp), "provider": provider, "plan": plan,
                            "data_at": report.get("fetched_at"), "market_at": market["at"]}])[-30:]
    try:
        brief = marketing_brief(profile, rows, recommendations, [r["query"] for r in market["searches"]])
    except Exception:
        update_report(project.id, task_id, plan_history=edition(backup, "pilot", time.time()), brief_error="Редактор не вернул проверяемый план. Доступны свежие наблюдения и резервный план съёмки.")
        return
    stamp = time.time()
    previous = report.get("previous_brief")
    if report.get("brief_at") and day_key(report["brief_at"]) != day_key(stamp):
        previous = {"day": day_key(report["brief_at"]), "plan": report.get("brief")}
    update_report(project.id, task_id, brief=brief, brief_at=stamp, brief_data_at=report.get("fetched_at"),
                  brief_market_at=market["at"], brief_provider=profile["provider"], brief_reference_revision=revision, brief_error="", previous_brief=previous,
                  plan_history=edition(brief, profile["provider"], stamp))


def run_report(project_id, task_id, action, query="", language="ru"):
    if not update_report(project_id, task_id, state="running"):
        return
    try:
        project = Project.objects.get(pk=project_id, is_active=True)
        if action in {"brief", "niche"}:
            build_strategy(project, task_id, query if action == "niche" else "", language, refresh_market=action == "niche")
        elif action == "behaviour":
            existing = read_report(project_id)
            rows = existing.get("rows", [])
            tokens = {}
            for row in rows[:30]:
                if row.get("platform") != "youtube":
                    continue
                try:
                    lang = row["language"]
                    if lang not in tokens:
                        tokens[lang] = _youtube_access_token(project.slug, lang)
                    row["behaviour"] = collect_behaviour(row, tokens[lang])
                except Exception:
                    row["behaviour"] = {**row.get("behaviour", {}), "warning": "Не удалось обновить: проверьте доступ YouTube Analytics и доступность данных ролика. Если ниже есть цифры, это прежний срез."}
            update_report(project_id, task_id, rows=rows, behaviour_fetched_at=time.time())
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=90)
            jobs = list(PublicationJob.objects.filter(project=project, status="published", published_at__gte=cutoff)
                        .select_related("project").order_by("-published_at")[:300])
            rows = [job_dict(job) for job in jobs]
            existing = read_report(project_id)
            history = existing.get("history", [])
            if action in {"history", "daily", "analyze"}:
                notices = []
                for lang in ("ru", "en"):
                    if action != "history" and not project_env(project.slug, "YOUTUBE_CHANNEL_ID_" + lang.upper()):
                        continue
                    try:
                        imported = youtube_history(project.slug, lang, _youtube_access_token(project.slug, lang))
                        history = [r for r in history if r.get("language") != lang] + imported
                    except Exception:
                        notices.append(f"YouTube {lang.upper()}: импорт недоступен; прежние данные сохранены. Проверьте доступ к чтению канала.")
                update_report(project_id, task_id, history=history, history_notices=notices)
            known = {(r["platform"], r["language"], r["external_post_id"]) for r in rows}
            for row in history:
                if (row["platform"], row["language"], row["external_post_id"]) not in known:
                    rows.append({**row, "project_id": str(project.id), "project_slug": project.slug})
            rows = unique_videos(rows, existing.get("analytics_exclusions", {}))
            attach_series(project, rows)
            tokens = {}
            for row in rows:
                key = (row["platform"], row["language"])
                if key in tokens:
                    continue
                try:
                    tokens[key] = (_youtube_access_token(project.slug, row["language"]) if key[0] == "youtube"
                                   else project_env(project.slug, f"INSTAGRAM_ACCESS_TOKEN_{key[1].upper()}")
                                   if key[0] == "instagram" else _vk_marketer_token(project.slug)
                                   if key[0] == "vk" else _tiktok_access_token(project.slug)
                                   if key[0] == "tiktok" else "")
                except Exception:
                    tokens[key] = ""

            previous_rows = {r["id"]: r for r in existing.get("rows", [])}
            def collect(row):
                result = dict(row)
                try:
                    token = tokens[(row["platform"], row["language"])]
                    if not token and row["platform"] in {"youtube", "instagram", "vk", "tiktok"}:
                        raise MetricsUnavailable("Нет действующего токена этого проекта/языка. Переподключите аккаунт.")
                    result.update(collect_metrics(row, token))
                except MetricsUnavailable as exc:
                    result.update(metrics=dict.fromkeys(METRICS), warnings=[str(exc)])
                if row["platform"] == "youtube":
                    try:
                        result["behaviour"] = collect_behaviour(row, tokens[("youtube", row["language"])])
                    except Exception:
                        result["behaviour"] = {**(previous_rows.get(row["id"], {}).get("behaviour") or {}), "warning": "Ошибка обновления YouTube Analytics. Если показаны значения, это предыдущий срез; проверьте его дату."}
                return result

            with ThreadPoolExecutor(max_workers=4) as pool:
                collected = list(pool.map(collect, rows))
            accounts = []
            old_accounts = {(a["platform"], a["language"]): a for a in existing.get("accounts", [])}
            for (platform, lang), token in tokens.items():
                try:
                    account = collect_followers(project.slug, platform, lang, token)
                    previous = old_accounts.get((platform, lang), {})
                    before, after = previous.get("followers"), account.get("followers")
                    same_account = previous.get("account_id") == account.get("account_id")
                    account["delta"] = after - before if same_account and before is not None and after is not None else None
                    account["previous_at"] = previous.get("fetched_at") if same_account else None
                except MetricsUnavailable as exc:
                    account = {"platform": platform, "language": lang, "followers": None, "warning": str(exc)}
                accounts.append(account)
            # Keep one preceding snapshot to show real deltas on the next refresh.
            old = {r["id"]: r for r in read_report(project_id).get("rows", [])}
            for row in collected:
                previous = old.get(row["id"], {})
                if previous.get("behaviour") and "behaviour" not in row:
                    row["behaviour"] = previous["behaviour"]
                row["previous"] = {"metrics": previous.get("metrics", {}), "fetched_at": previous.get("fetched_at")}
            stamp = time.time()
            snapshots = snapshot_history(existing.get("daily_snapshots", []), accounts, collected, stamp)
            update_report(project_id, task_id, rows=collected, accounts=accounts, fetched_at=stamp,
                          daily_snapshots=snapshots)
            if action in {"daily", "analyze"}:
                build_strategy(project, task_id, language=language)
        update_report(project_id, task_id, state="done", finished=time.time())
    except Exception:
        update_report(project_id, task_id, state="error", finished=time.time(),
                      error="Не удалось обновить отчёт. Проверьте доступ к API и работу сервиса. Предыдущие данные сохранены.")


class MarketerForm(forms.Form):
    project = forms.ModelChoiceField(queryset=Project.objects.none(), label="Проект")
    language = forms.ChoiceField(initial="ru", required=False, choices=(("ru", "Русский"), ("en", "English")), label="Язык исследования")
    query = forms.CharField(max_length=160, required=False, label="Тема / запрос для поиска",
                           widget=forms.TextInput(attrs={"placeholder": "Например: привычки, прокрастинация"}))
    references = forms.CharField(required=False, max_length=16000, label="Любимые ролики", widget=forms.Textarea(attrs={"rows": 4}))
    reference_revision = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=(("analyze", "Анализ проекта"), ("references", "Сохранить подборку"), ("refresh", "Статистика"), ("history", "История YouTube"), ("behaviour", "Удержание YouTube"), ("niche", "Ниша"), ("brief", "AI-разбор")), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(is_active=True).order_by("title")


def marketer_view(request):
    if not request.user.has_perm("pipeline_admin.view_publicationjob"):
        raise PermissionDenied
    projects = Project.objects.filter(is_active=True).order_by("title")
    form = MarketerForm(request.POST if request.method == "POST" else None)
    selected = request.POST.get("project") if request.method == "POST" else request.GET.get("project")
    try:
        project = projects.filter(pk=uuid.UUID(selected)).first() if selected else projects.first()
    except (ValueError, TypeError):
        project = None
    if request.method == "POST" and not request.user.has_perm("pipeline_admin.change_publicationjob"):
        raise PermissionDenied
    if request.method == "POST" and form.is_valid():
        project = form.cleaned_data["project"]
        action = form.cleaned_data["action"] or "refresh"
        query = form.cleaned_data["query"].strip()
        if action == "references":
            try:
                refs = parse_references(form.cleaned_data["references"])
            except ValueError as exc:
                form.add_error("references", str(exc))
            else:
                if save_references(project.id, refs, form.cleaned_data["reference_revision"] or 0):
                    messages.success(request, "Подборка сохранена. Следующий анализ начнётся с этих ссылок.")
                    return HttpResponseRedirect(reverse("marketer") + f"?project={project.id}#references")
                form.add_error("references", "Подборка уже менялась. Откройте страницу заново перед сохранением.")
        else:
            task_id = reserve_report(project.id, action)
            if task_id:
                from .tasks import marketer_report_task
                try:
                    marketer_report_task.delay(str(project.id), task_id, action, query, form.cleaned_data["language"] or "ru")
                    messages.success(request, "Сбор запущен в фоне. Обновите страницу через минуту; публикации не меняются.")
                except Exception:
                    update_report(project.id, task_id, state="error", finished=time.time(), error="Очередь недоступна. Запустите Celery и повторите.")
                    messages.error(request, "Не удалось отправить задачу в очередь.")
            else:
                messages.info(request, "Сбор уже идёт или с предыдущего обновления прошло меньше минуты.")
            return HttpResponseRedirect(reverse("marketer") + f"?project={project.id}")
    report = read_report(project.id) if project else {}
    if request.method == "GET" and request.GET.get("plan"):
        archived = next((e for e in report.get("plan_history", []) if e["id"] == request.GET["plan"]), None)
        if not archived:
            raise Http404("Выпуск отчёта не найден в этом проекте.")
        return TemplateResponse(request, "admin/pipeline_admin/marketer_edition.html", {
            **admin.site.each_context(request), "title": project.title, "project": project, "edition": archived,
            "brief_plan": readable_plan(archived["plan"]), "report": {"brief_provider": archived["provider"]},
            "review_note": report.get("edition_reviews", {}).get(archived["id"]),
            "plan_time": datetime.fromtimestamp(archived["at"], MOSCOW)})
    if request.method != "POST":
        form = MarketerForm(initial={"project": project, "language": "ru",
                    "reference_revision": report.get("reference_revision", 0),
                    "references": "\n".join(r["url"] + (" | " + r["note"] if r.get("note") else "") for r in report.get("references", []))})
    observed = datetime.fromtimestamp(report["fetched_at"], timezone.utc) if report.get("fetched_at") else None
    rows = diagnose(unique_videos(report.get("rows", []), report.get("analytics_exclusions", {})), now=observed)
    for row in rows:
        row["behaviour_display"] = behaviour_summary(row)
        row["metric_cells"] = [row.get("metrics", {}).get(m) for m in METRICS]
        current = row.get("metrics", {}).get("views")
        prev = row.get("previous", {}).get("metrics", {}).get("views")
        row["delta"] = current - prev if current is not None and prev is not None else None
    pending = report.get("state") in {"queued", "running"}
    stale = pending and time.time() - report.get("started", 0) >= 1200
    overview = dashboard(report, rows)
    groups = group_videos(rows)
    video_page = Paginator(groups, 12).get_page(request.GET.get("page"))
    market = report.get("market") or {}
    for item in market.get("items", []) + market.get("favorites", []):
        item["study"] = (report.get("video_studies") or {}).get(item["url"], {})
    selected_items = recommended_items(market)
    remaining_items = [item for item in market.get("items", []) if item not in selected_items]
    display_plan = report.get("pilot") if report.get("brief_error") and report.get("pilot") else report.get("brief")
    history = report.get("plan_history") or []
    reviewed_id = history[-1]["id"] if history and history[-1]["plan"] == display_plan else None
    previous = report.get("previous_brief")
    if previous and isinstance(previous.get("plan"), dict):
        report["previous_brief"] = {**previous, "plan": readable_plan(previous["plan"])}
    context = {**admin.site.each_context(request), "title": "Маркетолог", "form": form, "overview": overview,
               "brief_html": format_brief(report.get("brief")) if isinstance(report.get("brief"), str) else "",
               "brief_plan": readable_plan(display_plan), "review_note": report.get("edition_reviews", {}).get(reviewed_id),
               "plan_time": datetime.fromtimestamp(report["brief_at"], MOSCOW) if report.get("brief_at") else None,
               "market_time": observation_time(market.get("at")),
               "market": market, "selected_items": selected_items, "remaining_items": remaining_items,
               "video_notice": next((v.get("warning") for v in (report.get("video_studies") or {}).values() if v.get("provider_block")), ""),
               "notes": report.get("editorial_notes") or {},
               "legacy_plan": bool(display_plan and isinstance(display_plan, dict) and not display_plan.get("pilot") and not report.get("brief_market_at")),
               "project": project, "projects": projects, "report": report, "rows": rows, "video_groups": groups,
               "visible_groups": video_page.object_list, "video_page": video_page,
               "brief_stale": bool(report.get("brief")) and (
                   report.get("brief_data_at") != report.get("fetched_at")
                   or report.get("brief_market_at") != (report.get("market") or {}).get("at")
                   or report.get("brief_provider", "default") != report.get("editor_provider", "default")
                   or report.get("brief_reference_revision", 0) != report.get("reference_revision", 0)),
               "references_stale": report.get("market_reference_revision", 0) != report.get("reference_revision", 0),
               "pending": pending and not stale, "stale": stale,
               "can_change": request.user.has_perm("pipeline_admin.change_publicationjob")}
    return TemplateResponse(request, "admin/pipeline_admin/marketer.html", context)
