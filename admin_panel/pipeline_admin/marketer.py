"""Staff-only marketer page and bounded background report collection."""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse

from app.marketer import METRICS, MetricsUnavailable, collect_metrics, diagnose, discover_niche, marketing_brief, group_videos, youtube_history
from app.publication_config import project_env
from app.social_publishers import _youtube_access_token, _vk_access_token as _vk_marketer_token, _tiktok_access_token
from .models import Project, PublicationJob
from .marketer_display import format_brief, behaviour_summary
from app.marketer import collect_followers, collect_behaviour
from app.marketer_daily import snapshot_history, dashboard


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
            "external_post_id": job.external_post_id, "url": url}


def run_report(project_id, task_id, action, query="", language="ru"):
    if not update_report(project_id, task_id, state="running"):
        return
    try:
        project = Project.objects.get(pk=project_id, is_active=True)
        if action == "brief":
            existing = read_report(project_id)
            if not existing.get("rows") and not existing.get("niche"):
                raise ValueError("Collect evidence first")
            brief = marketing_brief(project.title, existing.get("rows", []), existing.get("niche", []), existing.get("niche_query", ""))
            update_report(project_id, task_id, brief=brief, brief_at=time.time(),
                          brief_data_at=existing.get("fetched_at"), brief_niche_at=existing.get("niche_fetched"), brief_error="")
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
        elif action == "niche":
            token = _youtube_access_token(project.slug, language)
            candidates = discover_niche(query, language, token)
            update_report(project_id, task_id, niche=candidates, niche_query=query,
                          niche_language=language, niche_fetched=time.time())
        else:
            jobs = list(PublicationJob.objects.filter(project=project, status="published")
                        .select_related("project").order_by("-published_at")[:30])
            rows = [job_dict(job) for job in jobs]
            existing = read_report(project_id)
            history = existing.get("history", [])
            if action == "history":
                notices = []
                for lang in ("ru", "en"):
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
            if action == "daily":
                # Metrics survive an AI failure; never replace a good plan with an empty one.
                try:
                    brief = marketing_brief(project.title, collected, [], "")
                    update_report(project_id, task_id, brief=brief, brief_at=time.time(),
                                  brief_data_at=stamp, brief_niche_at=existing.get("niche_fetched"), brief_error="")
                except Exception:
                    update_report(project_id, task_id, brief_error="Утренние данные собраны, но три совета не удалось обновить. Прежний анализ сохранён.")
        update_report(project_id, task_id, state="done", finished=time.time())
    except Exception:
        update_report(project_id, task_id, state="error", finished=time.time(),
                      error="Не удалось обновить отчёт. Проверьте доступ к API и работу сервиса. Предыдущие данные сохранены.")


class MarketerForm(forms.Form):
    project = forms.ModelChoiceField(queryset=Project.objects.none(), label="Проект")
    language = forms.ChoiceField(choices=(("ru", "Русский"), ("en", "English")), label="Язык исследования")
    query = forms.CharField(max_length=160, required=False, label="Тема / запрос для поиска",
                           widget=forms.TextInput(attrs={"placeholder": "Например: привычки, прокрастинация"}))
    action = forms.ChoiceField(choices=(("refresh", "Статистика"), ("history", "История YouTube"), ("behaviour", "Удержание YouTube"), ("niche", "Ниша"), ("brief", "AI-разбор")), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(is_active=True).order_by("title")


def marketer_view(request):
    if not request.user.has_perm("pipeline_admin.view_publicationjob"):
        raise PermissionDenied
    projects = Project.objects.filter(is_active=True).order_by("title")
    form = MarketerForm(request.POST if request.method == "POST" else None)
    selected = request.GET.get("project")
    try:
        project = projects.filter(pk=uuid.UUID(selected)).first() if selected else projects.first()
    except (ValueError, TypeError):
        project = None
    if request.method == "POST" and form.is_valid():
        if not request.user.has_perm("pipeline_admin.change_publicationjob"):
            raise PermissionDenied
        project = form.cleaned_data["project"]
        action = form.cleaned_data["action"] or "refresh"
        query = form.cleaned_data["query"].strip()
        if action == "niche" and not query:
            form.add_error("query", "Введите тему: она определяет выборку конкурентов.")
        elif action == "brief" and not (read_report(project.id).get("rows") or read_report(project.id).get("niche")):
            form.add_error(None, "Сначала соберите статистику или исследуйте нишу.")
        else:
            task_id = reserve_report(project.id, action)
            if task_id:
                from .tasks import marketer_report_task
                try:
                    marketer_report_task.delay(str(project.id), task_id, action, query, form.cleaned_data["language"])
                    messages.success(request, "Сбор запущен в фоне. Обновите страницу через минуту; публикации не меняются.")
                except Exception:
                    update_report(project.id, task_id, state="error", finished=time.time(), error="Очередь недоступна. Запустите Celery и повторите.")
                    messages.error(request, "Не удалось отправить задачу в очередь.")
            else:
                messages.info(request, "Сбор уже идёт или с предыдущего обновления прошло меньше минуты.")
            return HttpResponseRedirect(reverse("marketer") + f"?project={project.id}")
    report = read_report(project.id) if project else {}
    if request.method != "POST":
        form = MarketerForm(initial={"project": project, "language": "ru", "query": report.get("niche_query", "")})
    rows = diagnose(report.get("rows", []))
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
    context = {**admin.site.each_context(request), "title": "Маркетолог", "form": form, "overview": overview,
               "brief_html": format_brief(report.get("brief")) if isinstance(report.get("brief"), str) else "",
               "brief_plan": report.get("brief") if isinstance(report.get("brief"), dict) else None,
               "project": project, "projects": projects, "report": report, "rows": rows, "video_groups": groups,
               "visible_groups": video_page.object_list, "video_page": video_page,
               "brief_stale": bool(report.get("brief")) and (report.get("brief_data_at") != report.get("fetched_at") or report.get("brief_niche_at") != report.get("niche_fetched")),
               "pending": pending and not stale, "stale": stale,
               "can_change": request.user.has_perm("pipeline_admin.change_publicationjob")}
    return TemplateResponse(request, "admin/pipeline_admin/marketer.html", context)
