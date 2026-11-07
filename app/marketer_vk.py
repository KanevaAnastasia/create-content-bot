"""Read-only VK metrics, scoped to the configured community."""
import re
import time
import threading
from .publication_config import project_env, shared_env

_read_lock = threading.Lock()

def read_vk(slug, method, token, **params):
    from .social_publishers import _vk_call
    from .marketer import MetricsUnavailable
    if method not in {"groups.getById", "video.get", "wall.getById"}:
        raise ValueError("Read-only VK method required")
    # Both projects can share one VK user: avoid parallel request bursts.
    with _read_lock:
        for attempt in range(3):
            try:
                return _vk_call(method, token, shared_env("VK_API_VERSION", "5.199"), **params)
            except Exception as exc:
                if "(code 6)" in str(exc) and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise MetricsUnavailable("VK: не удалось прочитать статистику; проверьте доступ и существование публикации.") from None
            finally:
                time.sleep(0.4)


def group_id(slug):
    from .marketer import MetricsUnavailable
    value = project_env(slug, "VK_GROUP_ID_RU")
    if not re.fullmatch(r"[1-9]\d*", value):
        raise MetricsUnavailable("Не настроено VK-сообщество проекта.")
    return value


def followers(slug, token):
    from .marketer import MetricsUnavailable, number
    group = group_id(slug)
    rows = read_vk(slug, "groups.getById", token, group_ids=group, fields="members_count").get("groups", [])
    item = next((r for r in rows if str(r.get("id")) == group), None)
    if item is None:
        raise MetricsUnavailable("VK вернул другое сообщество.")
    return group, number(item.get("members_count"))


def video_metrics(job, token):
    from .marketer import METRICS, MetricsUnavailable, number
    slug = job["project_slug"]
    group = group_id(slug)
    post_id = str(job.get("external_post_id") or "")
    if not re.fullmatch(r"-" + re.escape(group) + r"_\d+", post_id):
        raise MetricsUnavailable("VK: владелец видео не совпадает с сообществом проекта.")
    rows = read_vk(slug, "video.get", token, videos=post_id, extended=1).get("items", [])
    item = next((r for r in rows if f"{r.get('owner_id')}_{r.get('id')}" == post_id), None)
    if item is None:
        raise MetricsUnavailable("VK-видео недоступно или удалено.")
    values = dict.fromkeys(METRICS)
    values.update(views=number(item.get("views")), comments=number(item.get("comments")),
                  likes=number((item.get("likes") or {}).get("count")),
                  shares=number((item.get("reposts") or {}).get("count")))
    return values
