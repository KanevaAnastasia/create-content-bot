"""Read-only Display API analytics, independent of the publishing provider."""
import re

import requests

from .publication_config import project_env


def read(endpoint, token, fields, body=None):
    from .marketer import MetricsUnavailable
    if endpoint not in {"user/info", "video/query"}:
        raise ValueError("Unsupported analytics endpoint")
    if not token:
        raise MetricsUnavailable("TikTok: подключите доступ к статистике.")
    kwargs = dict(headers={"Authorization": f"Bearer {token}"},
                  params={"fields": fields}, timeout=(5, 20), allow_redirects=False)
    try:
        url = f"https://open.tiktokapis.com/v2/{endpoint}/"
        response = requests.get(url, **kwargs) if body is None else requests.post(url, json=body, **kwargs)
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise MetricsUnavailable("TikTok: сервис статистики не ответил.") from None
    if not isinstance(payload, dict):
        raise MetricsUnavailable("TikTok: некорректный ответ статистики.")
    error = payload.get("error") or {}
    code = error.get("code") if isinstance(error, dict) else None
    if code == "scope_not_authorized":
        raise MetricsUnavailable("TikTok: нужны разрешения video.list и user.info.stats. Переподключите аккаунт.")
    if not response.ok or code != "ok" or not isinstance(payload.get("data"), dict):
        raise MetricsUnavailable("TikTok: статистика недоступна; проверьте подключение.")
    return payload["data"]


def account(slug, token, stats=False):
    from .marketer import MetricsUnavailable
    expected = project_env(slug, "TIKTOK_OPEN_ID")
    if not expected:
        raise MetricsUnavailable("TikTok: не задан аккаунт проекта.")
    fields = "open_id,follower_count" if stats else "open_id"
    user = read("user/info", token, fields).get("user") or {}
    if user.get("open_id") != expected:
        raise MetricsUnavailable("TikTok: аккаунт не совпадает с проектом.")
    return user


def video_metrics(job, token):
    from .marketer import METRICS, MetricsUnavailable, number
    post_id = str(job.get("external_post_id") or "")
    if not re.fullmatch(r"\d{1,30}", post_id):
        raise MetricsUnavailable("TikTok: нет публичного ID ролика.")
    account(job["project_slug"], token)
    rows = read("video/query", token, "id,view_count,like_count,comment_count,share_count",
                {"filters": {"video_ids": [post_id]}}).get("videos") or []
    item = next((r for r in rows if str(r.get("id")) == post_id), None)
    if item is None:
        raise MetricsUnavailable("TikTok: ролик недоступен для этого аккаунта.")
    values = dict.fromkeys(METRICS)
    for local, remote in (("views", "view_count"), ("likes", "like_count"),
                          ("comments", "comment_count"), ("shares", "share_count")):
        values[local] = number(item.get(remote))
    return values
