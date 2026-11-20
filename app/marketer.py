"""Read-only marketing diagnostics. Never calls a publication endpoint."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from statistics import median

import requests

from .publication_config import project_env, shared_env


METRICS = ("views", "reach", "likes", "comments", "saves", "shares")


def collect_followers(slug, platform, language, token):
    """Account-level snapshot, never attributed to an individual video."""
    approximate = False
    if platform == "youtube":
        account_id = project_env(slug, f"YOUTUBE_CHANNEL_ID_{language.upper()}")
        if not account_id:
            raise MetricsUnavailable("Не настроен канал.")
        items = api_get("https://www.googleapis.com/youtube/v3/channels", token,
                        {"part": "statistics", "id": account_id}).get("items", [])
        if not items or items[0].get("id") != account_id:
            raise MetricsUnavailable("Не удалось подтвердить канал.")
        stats = items[0].get("statistics", {})
        value = None if stats.get("hiddenSubscriberCount") else number(stats.get("subscriberCount"))
        approximate = True
    elif platform == "instagram":
        account_id = project_env(slug, f"INSTAGRAM_BUSINESS_ACCOUNT_ID_{language.upper()}")
        if not account_id or not re.fullmatch(r"\d+", account_id):
            raise MetricsUnavailable("Не настроен Instagram-аккаунт.")
        data = api_get(f"https://graph.facebook.com/{shared_env('META_GRAPH_API_VERSION', 'v27.0')}/{account_id}",
                       token, {"fields": "id,followers_count"})
        if str(data.get("id")) != account_id:
            raise MetricsUnavailable("Не удалось подтвердить Instagram-аккаунт.")
        value = number(data.get("followers_count"))
    elif platform == "tiktok":
        from .marketer_tiktok import account
        user = account(slug, token, stats=True)
        account_id, value = user["open_id"], number(user.get("follower_count"))
    elif platform == "vk":
        from .marketer_vk import followers
        account_id, value = followers(slug, token)
    else:
        raise MetricsUnavailable("Сбор подписчиков этой площадки пока не подключён.")
    return {"platform": platform, "language": language, "account_id": account_id,
            "followers": value, "approximate": approximate, "fetched_at": datetime.now(timezone.utc).isoformat()}


def collect_behaviour(row, token):
    """YouTube aggregate metrics and retention, not individual viewer events."""
    if row.get("platform") != "youtube":
        raise MetricsUnavailable("Разбор удержания этой площадки пока не подключён.")
    video_id = str(row.get("external_post_id") or "")
    channel = project_env(row["project_slug"], f"YOUTUBE_CHANNEL_ID_{row['language'].upper()}")
    if not channel or not re.fullmatch(r"[\w-]{11}", video_id):
        raise MetricsUnavailable("Не настроен канал или ID ролика.")
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=90)
    params = {"ids": f"channel=={channel}", "startDate": start.isoformat(), "endDate": today.isoformat(),
              "filters": f"video=={video_id}",
              "metrics": "subscribersGained,subscribersLost,averageViewDuration,averageViewPercentage"}
    url = "https://youtubeanalytics.googleapis.com/v2/reports"
    report = api_get(url, token, params)
    values = dict(zip([c.get("name") for c in report.get("columnHeaders", [])], (report.get("rows") or [[]])[0]))
    result = {key: number(values.get(remote)) for key, remote in (
        ("subscribers_gained", "subscribersGained"), ("subscribers_lost", "subscribersLost"),
        ("average_view_seconds", "averageViewDuration"), ("average_view_percentage", "averageViewPercentage"))}
    result.update(period=f"{start.isoformat()} — {today.isoformat()} (UTC)", fetched_at=datetime.now(timezone.utc).isoformat())
    video = api_get("https://www.googleapis.com/youtube/v3/videos", token,
                    {"part": "contentDetails,snippet", "id": video_id}).get("items", [])
    if not video or video[0].get("snippet", {}).get("channelId") != channel:
        raise MetricsUnavailable("Канал ролика не совпадает с настройкой проекта.")
    duration = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", video[0].get("contentDetails", {}).get("duration", ""))
    if duration:
        result["duration_seconds"] = sum(float(v or 0) * factor for v, factor in zip(duration.groups(), (3600, 60, 1)))
    try:
        retention = api_get(url, token, {**params, "dimensions": "elapsedVideoTimeRatio", "metrics": "audienceWatchRatio"})
        headers = [c.get("name") for c in retention.get("columnHeaders", [])]
        points = []
        for values in retention.get("rows", []):
            item = dict(zip(headers, values))
            x, y = number(item.get("elapsedVideoTimeRatio")), number(item.get("audienceWatchRatio"))
            if x is not None and 0 <= x <= 1 and y is not None:
                points.append({"position": round(x * 100, 2), "watch_ratio": round(y * 100, 2)})
        result["retention"] = sorted(points, key=lambda p: p["position"])
    except MetricsUnavailable:
        result["retention_warning"] = "API не вернул удержание. Оно может быть недоступно для этого видео или периода."
    return result


def group_videos(rows):
    """Only a shared, project-scoped source bundle proves that posts belong together."""
    groups = {}
    for row in rows:
        bundle = row.get("bundle_key") or ""
        if row.get("instagram_trial") and bundle.endswith("-trial"):
            bundle = bundle[:-6]
        key = (row.get("project_id"), bundle or row["id"])
        group = groups.setdefault(key, {"title": row.get("title"), "rows": [], "thumbnail": ""})
        group["rows"].append(row)
        if row.get("platform") == "youtube" and re.fullmatch(r"[\w-]{11}", str(row.get("external_post_id", ""))):
            group["thumbnail"] = f"https://i.ytimg.com/vi/{row['external_post_id']}/mqdefault.jpg"
    return list(groups.values())


def youtube_history(slug, language, token, now=None):
    """Read at most 50 recent uploads, never infer ownership from titles."""
    channel = project_env(slug, f"YOUTUBE_CHANNEL_ID_{language.upper()}")
    if not channel:
        raise MetricsUnavailable("Не настроен ID YouTube-канала этого проекта и языка.")
    base = "https://www.googleapis.com/youtube/v3/"
    items = api_get(base + "channels", token, {"part": "contentDetails", "id": channel}).get("items", [])
    if not items or items[0].get("id") != channel:
        raise MetricsUnavailable("Настроенный канал недоступен.")
    playlist = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not playlist:
        raise MetricsUnavailable("API не вернул список загрузок канала.")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=90)
    result, cursor = [], ""
    for _ in range(1):
        data = api_get(base + "playlistItems", token, {"part": "snippet,contentDetails", "playlistId": playlist,
                                                       "maxResults": 50, "pageToken": cursor})
        for item in data.get("items", []):
            snippet, detail = item.get("snippet", {}), item.get("contentDetails", {})
            stamp = detail.get("videoPublishedAt")
            try:
                if not stamp or datetime.fromisoformat(stamp.replace("Z", "+00:00")) < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            video_id = detail.get("videoId", "")
            if not re.fullmatch(r"[\w-]{11}", video_id):
                continue
            result.append({"id": f"history:youtube:{channel}:{video_id}", "platform": "youtube",
                           "language": language, "account_key": "default", "external_post_id": video_id,
                           "title": snippet.get("title") or "Без названия", "posted_at": stamp,
                           "url": f"https://www.youtube.com/watch?v={video_id}", "origin": "history"})
        cursor = data.get("nextPageToken")
        if not cursor:
            break
    return result


def marketing_brief(project, rows, niche, query):
    from .marketer_strategy import generate_plan
    return generate_plan(project, rows, niche, query)


def parse_marketing_plan(output):
    from .marketer_strategy import parse_plan
    return parse_plan(output)


class MetricsUnavailable(RuntimeError):
    pass


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def api_get(url, token, params):
    try:
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                                params=params, timeout=(5, 20))
        data = response.json()
    except (requests.RequestException, ValueError):
        raise MetricsUnavailable("Сервис не ответил. Повторите обновление позже.") from None
    if not response.ok or not isinstance(data, dict) or "error" in data:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        if isinstance(error, dict) and error.get("code") == 100 and "does not support" in str(error.get("message", "")) and "media product type" in str(error.get("message", "")):
            raise MetricsUnavailable("Instagram API не поддерживает эту метрику для данного типа публикации.")
        # Do not expose provider messages / URLs: they can contain credentials.
        raise MetricsUnavailable(f"API HTTP {response.status_code}: проверьте доступ к статистике и существование ролика.")
    return data


def collect_metrics(job, token):
    """job is a plain, project-scoped dict; missing values remain None."""
    values = dict.fromkeys(METRICS)
    warnings = []
    metric_errors = {}
    comments_sample = None
    extra = {}
    post_id = str(job.get("external_post_id") or "")
    if not re.fullmatch(r"[\w-]{1,80}", post_id):
        raise MetricsUnavailable("Нет корректного ID публикации.")
    if job["platform"] == "youtube":
        data = api_get("https://www.googleapis.com/youtube/v3/videos", token,
                       {"part": "statistics,snippet,contentDetails", "id": post_id}).get("items", [])
        if not data:
            raise MetricsUnavailable("Видео недоступно или удалено.")
        item = data[0]
        expected = project_env(job["project_slug"], f"YOUTUBE_CHANNEL_ID_{job['language'].upper()}")
        if expected and item.get("snippet", {}).get("channelId") != expected:
            raise MetricsUnavailable("Канал ролика не совпадает с настройками проекта.")
        stats = item.get("statistics", {})
        for key, remote in (("views", "viewCount"), ("likes", "likeCount"), ("comments", "commentCount")):
            values[key] = number(stats.get(remote))
        extra["duration_seconds"] = duration_seconds(item.get("contentDetails", {}).get("duration"))
        extra["channel_id"] = item.get("snippet", {}).get("channelId")
        extra["caption"] = str(item.get("snippet", {}).get("description") or "")[:700]
        comments_sample = youtube_comments(post_id, token, values["comments"])
    elif job["platform"] == "instagram":
        base = f"https://graph.facebook.com/{shared_env('META_GRAPH_API_VERSION', 'v27.0')}"
        basic = api_get(f"{base}/{post_id}", token, {"fields": "id,like_count,comments_count"})
        values.update(likes=number(basic.get("like_count")), comments=number(basic.get("comments_count")))
        from .marketer_comments import summarize_comments
        try:
            comment_data = api_get(f"{base}/{post_id}/comments", token, {"fields": "text,timestamp", "limit": 30})
            comments_sample = summarize_comments(comment_data.get("data", []), bool(comment_data.get("paging", {}).get("next")))
        except MetricsUnavailable as exc:
            comments_sample = {"warning": str(exc)}
        # Independent requests preserve available metrics when one is unsupported.
        for metric, key in (("views", "views"), ("reach", "reach"), ("saved", "saves"), ("shares", "shares"),
                            ("follows", "subscribers_gained"), ("ig_reels_avg_watch_time", "average_watch_ms"),
                            ("ig_reels_video_view_total_time", "total_watch_ms"), ("reels_skip_rate", "skip_rate"),
                            ("total_interactions", "total_interactions")):
            values[key] = None
            try:
                rows = api_get(f"{base}/{post_id}/insights", token, {"metric": metric}).get("data", [])
                row = next((r for r in rows if r.get("name") == metric), {})
                points = row.get("values") or []
                value = points[-1].get("value") if points else (row.get("total_value") or {}).get("value")
                values[key] = number(value)
                if values[key] is None:
                    metric_errors[key] = "API пока не вернул значение."
                    warnings.append(f"{metric}: API пока не вернул значение.")
            except MetricsUnavailable as exc:
                metric_errors[key] = str(exc)
                warnings.append(f"{metric}: {exc}")
    elif job["platform"] == "tiktok":
        from .marketer_tiktok import video_metrics
        values = video_metrics(job, token)
        warnings.append("TikTok Display API не возвращает охват, сохранения и посекундное удержание.")
    elif job["platform"] == "vk":
        from .marketer_vk import video_comments, video_metrics
        values = video_metrics(job, token)
        comments_sample = video_comments(job["project_slug"], post_id, token, values.get("comments"))
        warnings.append("Показатели относятся к видео VK, не к записи на стене или сториз. Охват и сохранения не возвращены этим API.")
    else:
        raise MetricsUnavailable("Автосбор этой площадки пока не подключён. Это не означает нулевые просмотры.")
    return {**extra, "metrics": values, "metric_errors": metric_errors, "comments_sample": comments_sample, "warnings": warnings, "fetched_at": datetime.now(timezone.utc).isoformat()}


def age_hours(posted_at, now):
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt).total_seconds() / 3600)
    except (ValueError, TypeError, AttributeError):
        return None


def age_band(age):
    if age is None:
        return None
    return "0–2 дня" if age < 48 else "2–7 дней" if age < 168 else "7–30 дней" if age < 720 else "30+ дней"


def format_band(row):
    duration = row.get("duration_seconds") or (row.get("behaviour") or {}).get("duration_seconds")
    return ("long" if duration > 180 else "short") if duration else "unknown"


def diagnose(rows, now=None):
    """Transparent hypotheses, not causal claims from view counts."""
    now = now or datetime.now(timezone.utc)
    result = []
    for source in rows:
        row = dict(source)
        metrics = row.get("metrics") or {}
        views = number(metrics.get("views"))
        age = age_hours(row.get("posted_at"), now)
        band = age_band(age)
        row.update(age_band=band or "возраст неизвестен", advice=[], baseline=None, relative=None)
        cohort = [number((r.get("metrics") or {}).get("views")) for r in rows
                  if r.get("id") != row.get("id") and r.get("project_id") == row.get("project_id")
                  and (r.get("platform"), r.get("language"), r.get("account_key")) ==
                  (row.get("platform"), row.get("language"), row.get("account_key"))
                  and bool(r.get("instagram_trial")) == bool(row.get("instagram_trial"))
                  and format_band(r) == format_band(row)
                  and band is not None and age_band(age_hours(r.get("posted_at"), now)) == band]
        cohort = [v for v in cohort if v is not None]
        row["cohort_count"] = len(cohort)
        if views is None:
            row["advice"].append("Сначала получите просмотры: без них нельзя оценить распространение ролика.")
        elif age is not None and age < 48:
            row["advice"].append("Ролику меньше 48 часов: выводы предварительные. Снимите показатели повторно через 2–3 дня; не удаляйте по первым цифрам.")
        elif len(cohort) >= 3 and median(cohort) > 0:
            baseline = median(cohort)
            ratio = views / baseline
            row.update(baseline=baseline, relative=round(ratio, 2))
            if ratio >= 1.5:
                row["advice"].append("Просмотры выше медианы сопоставимой группы. Гипотеза: тема или подача заслуживает продолжения. Сделайте ещё один ролик на смежную тему, меняя только один элемент.")
            elif ratio <= .67:
                row["advice"].append("Просмотры ниже медианы группы. Это не доказывает плохой монтаж: протестируйте другое обещание в первых 2 секундах при той же теме и длительности.")
            else:
                row["advice"].append("Просмотры близки к медиане группы. Проведите один контролируемый тест: конкретный результат в первом кадре против вопроса.")
        else:
            row["advice"].append("Недостаточно сопоставимых роликов: нужно минимум 3 других на том же аккаунте, языке и площадке, близких по возрасту. Пока формируйте базовую выборку.")
        denominator = number(metrics.get("reach")) if row.get("platform") == "instagram" else views
        row["interaction_rate"] = None
        row["rate_label"] = "(лайки + комментарии) / охват" if row.get("platform") == "instagram" else "(лайки + комментарии) / просмотры"
        likes, comments = number(metrics.get("likes")), number(metrics.get("comments"))
        if denominator and likes is not None and comments is not None:
            row["interaction_rate"] = round(100 * (likes + comments) / denominator, 2)
        saves = number(metrics.get("saves"))
        if saves is not None and denominator:
            row["save_rate"] = round(100 * saves / denominator, 2)
            row["advice"].append("Тест на сохранения: добавьте короткий полезный итог/чек-лист. Сравните долю сохранений через одинаковое время после публикации.")
        if not (row.get("behaviour") or {}).get("retention"):
            row["advice"].append("Удержание не измерено: нельзя утверждать, что зритель уходит на конкретной секунде. Проверьте график удержания в приложении площадки.")
        row["experiment"] = ("Покажите готовое блюдо в первом кадре. Затем покажите один ключевой приём."
                             if row.get("project_slug") == "grandma_kitchen" else
                             "Откройте ролик знакомой ситуацией и покажите первое действие героя." if row.get("project_slug") == "discipline" else
                             "Проверьте два начала одной истории. Сохраните тему и длительность.")
        result.append(row)
    return result


def duration_seconds(value):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", str(value or ""))
    if match:
        return sum(float(v or 0) * factor for v, factor in zip(match.groups(), (3600, 60, 1)))


def youtube_comments(video_id, token, count=None):
    from .marketer_comments import summarize_comments
    if count == 0:
        return summarize_comments([])
    try:
        data = api_get("https://www.googleapis.com/youtube/v3/commentThreads", token, {
            "part": "snippet", "videoId": video_id, "maxResults": 30, "order": "relevance", "textFormat": "plainText",
        })
        comments = [thread.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                    for thread in data.get("items", [])]
        return summarize_comments([{"text": c.get("textDisplay"), "timestamp": c.get("publishedAt")}
                                   for c in comments if c.get("textDisplay")], bool(data.get("nextPageToken")))
    except MetricsUnavailable as exc:
        return {"warning": str(exc)}


def youtube_videos(ids, token):
    if not ids:
        return []
    videos = api_get("https://www.googleapis.com/youtube/v3/videos", token, {
        "part": "snippet,statistics,contentDetails", "id": ",".join(ids),
    }).get("items", [])
    result = []
    for item in videos:
        video_id = item.get("id")
        if video_id not in ids:
            continue
        snippet, stats = item.get("snippet", {}), item.get("statistics", {})
        views = number(stats.get("viewCount"))
        age = age_hours(snippet.get("publishedAt"), datetime.now(timezone.utc))
        result.append({
            "platform": "youtube", "video_id": video_id, "title": str(snippet.get("title") or "")[:300],
            "caption": str(snippet.get("description") or "")[:600], "channel": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId"), "language": (snippet.get("defaultAudioLanguage")
                         or snippet.get("defaultLanguage") or "").split("-")[0].lower(),
            "url": f"https://www.youtube.com/watch?v={video_id}", "views": views,
            "likes": number(stats.get("likeCount")), "comments": number(stats.get("commentCount")),
            "posted_at": snippet.get("publishedAt"), "duration_seconds": duration_seconds(item.get("contentDetails", {}).get("duration")),
            "views_per_day": round(views / max(1, age / 24)) if views is not None and age is not None else None,
        })
    return result


def discover_niche(query, language, token):
    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
    found = api_get("https://www.googleapis.com/youtube/v3/search", token, {
        "part": "id", "q": query[:160], "type": "video", "order": "viewCount",
        "maxResults": 12, "videoDuration": "short", "publishedAfter": since,
        "relevanceLanguage": language,
    }).get("items", [])
    ids = [r.get("id", {}).get("videoId") for r in found]
    return youtube_videos([i for i in ids if re.fullmatch(r"[\w-]{11}", str(i or ""))], token)
