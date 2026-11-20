"""Read-only discovery and reference library, scoped to a single project report."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import requests

from .marketer import MetricsUnavailable, age_hours, discover_niche, number, youtube_comments, youtube_videos
from .publication_config import project_env

# Search both the subject and a distinctive way of telling the story.
PROFILES = {
    "grandma_kitchen": {
        "queries": {"ru": ("бабушкины рецепты домашняя кухня", "бабушка кухонные хитрости"),
                    "en": ("grandma home cooking recipe", "animated grandma cooking")},
        "topic": ("рецепт", "кулинар", "кухн", "приготов", "готовит", "блюд", "салат", "пирог", "выпеч", "тесто",
                  "cook", "recipe", "kitchen", "baking", "salad", "dough", "kuchen", "cocin"),
        "style": ("бабуш", "бабул", "домашн", "семейн", "уютн", "мульт", "grandma", "granny", "nonna", "homemade", "family", "cozy", "animat"),
    },
    "discipline": {
        "queries": {"ru": ("прокрастинация привычки как начать", "история привычки прокрастинация анимация"),
                    "en": ("procrastination habits start small", "animated story procrastination habits")},
        "topic": ("прокрастин", "привыч", "откладыва", "дисциплин", "продуктивн", "тайм-менедж", "лень", "самоорганиз",
                  "procrastin", "disciplin", "habit", "productiv", "stop scrolling", "focus"),
        "style": ("истори", "персонаж", "диалог", "мульт", "сценк", "эксперимент", "маленьк", "animat", "story", "skit", "dialog", "experiment", "small step"),
    },
}
STOP = {"short", "shorts", "video", "reels", "проект", "ролик", "этот", "этого", "чтобы", "когда",
        "который", "просто", "блог", "that", "this", "your", "with", "from", "project", "about"}


def terms(text):
    return [w for w in re.findall(r"[a-zа-яё]{4,}", str(text or "").casefold()) if w not in STOP]


def reference_url(value):
    """Normalize public post permalinks. No user URL is followed server-side."""
    try:
        url = urlsplit(str(value).strip())
        if url.scheme != "https" or url.username or url.password or url.port is not None:
            return None
        host = (url.hostname or "").removeprefix("www.").removeprefix("m.")
    except ValueError:
        return None
    path = url.path.rstrip("/")
    if host in {"youtube.com", "youtu.be"}:
        if host == "youtu.be":
            video_id = path.removeprefix("/")
        elif path == "/watch":
            video_id = (parse_qs(url.query).get("v") or [""])[0]
        elif path.startswith("/shorts/"):
            video_id = path.removeprefix("/shorts/")
        else:
            return None
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return "youtube", video_id, f"https://www.youtube.com/watch?v={video_id}"
    patterns = {
        "instagram.com": ("instagram", r"/(reel|reels|p)/([A-Za-z0-9_-]+)"),
        "tiktok.com": ("tiktok", r"/(@[\w.-]+)/video/(\d+)"),
        "vk.com": ("vk", r"/(?:video|clip)(-?\d+_\d+)"),
        "vkvideo.ru": ("vk", r"/(?:video|clip)(-?\d+_\d+)"),
    }
    if host in patterns:
        platform, pattern = patterns[host]
        match = re.fullmatch(pattern, path)
        if match:
            ident = match.groups()[-1]
            if platform == "vk":
                return platform, ident, "https://vk.com/video" + ident
            if platform == "instagram":
                kind = "p" if match[1] == "p" else "reel"
                return platform, ident, f"https://www.instagram.com/{kind}/{ident}/"
            return platform, ident, "https://www.tiktok.com" + path
    return None


def parse_references(text):
    refs = {}
    for line_no, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip():
            continue
        url, _, note = line.partition("|")
        parsed = reference_url(url)
        if not parsed or len(note.strip()) > 250:
            raise ValueError(f"Строка {line_no}. Нужна полная HTTPS-ссылка на видео YouTube, Instagram, VK или TikTok. Заметка после | может содержать до 250 символов.")
        platform, ident, canonical = parsed
        refs.setdefault((platform, ident), {"url": canonical, "note": note.strip()})
    if len(refs) > 30:
        raise ValueError("В подборке помещается 30 разных ссылок.")
    return list(refs.values())


def _vk_item(item):
    ident = f"{item.get('owner_id')}_{item.get('id')}"
    if not re.fullmatch(r"-?\d+_\d+", ident):
        return None
    date = number(item.get("date"))
    return {"platform": "vk", "video_id": ident, "url": "https://vk.com/video" + ident,
            "channel_id": str(item["owner_id"]), "channel": f"VK {item['owner_id']}", "language": "",
            "title": str(item.get("title") or "")[:300], "caption": str(item.get("description") or "")[:600],
            "views": number(item.get("views")), "comments": number(item.get("comments")),
            "duration_seconds": number(item.get("duration")),
            "posted_at": datetime.fromtimestamp(date, timezone.utc).isoformat() if date else None}


def _favourites(refs, rows, token_for, slug, old, now):
    own = {}
    for row in rows:
        parsed = reference_url(row.get("url"))
        if parsed:
            own[parsed[:2]] = row
        own[(row.get("platform"), row.get("external_post_id"))] = row
    lookup = {r["url"]: r for r in old}
    result = []
    for ref in refs:
        platform, ident, url = reference_url(ref["url"])
        row = own.get((platform, ident))
        item = {"platform": platform, "video_id": ident, "url": url}
        if row:
            item.update(title=row.get("title"), caption=row.get("caption"), own=True,
                        language=row.get("language"), posted_at=row.get("posted_at"),
                        views=(row.get("metrics") or {}).get("views"), observed_at=row.get("fetched_at"))
        else:
            try:
                if platform == "youtube":
                    details = youtube_videos([ident], token_for("youtube", "ru"))
                elif platform == "vk":
                    from .marketer_vk import read_vk
                    raw = read_vk(slug, "video.get", token_for("vk", "ru"), videos=ident).get("items", [])
                    details = [v for v in map(_vk_item, raw) if v and v["video_id"] == ident]
                elif platform == "tiktok":
                    response = requests.get("https://www.tiktok.com/oembed", params={"url": url},
                                            timeout=(4, 8), allow_redirects=False)
                    response.raise_for_status()
                    title = response.json().get("title")
                    details = [{"title": str(title)[:300]}] if title else []
                else:
                    # Graph insights for other people's Reels are not available with a creator token.
                    details = []
                if details:
                    item.update(details[0], observed_at=now.isoformat())
                else:
                    item["warning"] = "Публичные данные недоступны. Ссылка сохранена как ориентир."
            except (MetricsUnavailable, requests.RequestException, ValueError):
                item = {**lookup.get(url, {}), **item, "warning": "Не удалось обновить сведения по ссылке."}
        result.append({**item, **ref})
    return result


def _searches(profile, rows, refs, day, query, language):
    if query:
        return [(language, query)]
    config = PROFILES.get(profile["slug"], {})
    titles = profile.get("series", [])
    by_lang = {}
    for lang in profile.get("languages") or [language]:
        defaults = config.get("queries", {}).get(lang, ())
        choices = [r.get("title") for r in rows if r.get("language") == lang and r.get("title")]
        fallback = next(iter(choices), None) or (titles[0] if titles else profile.get("description") or profile.get("title"))
        if not defaults and not fallback:
            continue
        # Keep the core query stable for measuring growth. Rotate the second angle.
        first = defaults[0] if defaults else " ".join(terms(fallback)[:6]) or str(fallback)
        angles = list(defaults[1:])
        for r in refs:
            if r.get("language") == lang and r.get("title"):
                angles.append(" ".join(terms(r["title"])[:6]))
        if choices:
            angles.append(" ".join(terms(choices[0])[:6]))
        if titles and lang == "ru":
            angles.append(titles[day % len(titles)])
        angles = list(dict.fromkeys(a for a in angles if a and a != first))
        by_lang[lang] = [first] + ([angles[day % len(angles)]] if angles else [])
    return [(lang, q[:160]) for lang, queries in by_lang.items() for q in queries]


def _fit(item, profile, query):
    config = PROFILES.get(profile["slug"], {})
    title = (item.get("title") or "").casefold()
    text = title + " " + (item.get("caption") or "").casefold()
    topics = config.get("topic") or terms(" ".join([profile.get("description", ""), query]))
    topic = next((word for word in topics if word in title), None)
    style = next((word for word in config.get("style", ()) if word in text), None)
    # A generic caption with hashtags alone is weak evidence. Require a second signal.
    if not topic and style:
        topic = next((word for word in topics if word in text), None)
    if not topic:
        topic = next((word for word in terms(query) if word in title), None)
    if not topic:
        return None
    return {"topic_match": topic, "style_match": style or "", "query": query}


def collect_market(profile, rows, refs, previous, token_for, *, query="", language="ru", now=None):
    now = now or datetime.now(timezone.utc)
    slug = profile["slug"]
    fav = _favourites(refs, rows, token_for, slug, previous.get("favorites", []), now)
    from .marketer_strategy import select_own
    leaders = select_own(rows, as_of=now)
    searches = _searches(profile, leaders, fav, now.date().toordinal(), query, language)
    own_ids = {(r.get("platform"), r.get("external_post_id")) for r in rows}
    own_channels = {("youtube", project_env(slug, "YOUTUBE_CHANNEL_ID_" + lang)) for lang in ("RU", "EN")}
    group = project_env(slug, "VK_GROUP_ID_RU")
    if group:
        own_channels.add(("vk", "-" + group))
    pooled = {i["url"]: dict(i) for i in previous.get("items", []) if
              i.get("observed_at") and (age_hours(i["observed_at"], now) or 0) < 7 * 24 and
              (age_hours(i.get("posted_at"), now) or 0) <= 90 * 24 and
              (i.get("platform"), i.get("channel_id")) not in own_channels and
              (i.get("platform"), i.get("video_id")) not in own_ids}
    runs = []
    for lang, phrase in searches:
        platforms = ["youtube", "vk"] if lang == "ru" and group else ["youtube"]
        for platform in platforms:
            entry = {"platform": platform, "language": lang, "query": phrase, "found": 0, "matched": 0}
            runs.append(entry)
            try:
                token = token_for(platform, lang)
                if platform == "youtube":
                    found = discover_niche(phrase, lang, token)
                else:
                    from .marketer_vk import read_vk
                    data = read_vk(slug, "video.search", token, q=phrase, sort=2, count=12, shorter=181, adult=0)
                    found = [v for v in map(_vk_item, data.get("items", [])) if v]
            except MetricsUnavailable as exc:
                entry["warning"] = str(exc)
                continue
            entry["found"] = len(found)
            for item in found:
                age = age_hours(item.get("posted_at"), now)
                fit = _fit(item, profile, phrase)
                if (not fit or age is None or age > 90 * 24 or not (0 < (item.get("duration_seconds") or 0) <= 180)
                        or (platform, item.get("channel_id")) in own_channels or (platform, item["video_id"]) in own_ids
                        or (item.get("language") and item["language"] != lang)):
                    continue
                old = pooled.get(item["url"], {})
                if old.get("observed_at") == now.isoformat():
                    entry["matched"] += 1
                    continue
                base_at, base_views = old.get("growth_base_at"), old.get("growth_base_views")
                hours = age_hours(old.get("observed_at"), now)
                if hours is not None and hours >= 12:
                    base_at, base_views = old["observed_at"], old.get("views")
                interval = age_hours(base_at, now)
                views = item.get("views")
                growth = None
                if interval and 12 <= interval <= 72 and views is not None and base_views is not None:
                    growth = round((views - base_views) * 24 / interval)
                pooled[item["url"]] = {**item, **fit, "search_language": lang, "observed_at": now.isoformat(),
                    "growth_base_at": base_at, "growth_base_views": base_views, "growth_per_day": growth,
                    "views_per_day": round(views / max(1, age / 24)) if views is not None else None}
                entry["matched"] += 1
    # Rank within platform and search language. Interleave groups without comparing raw platform counters.
    groups = {}
    for item in pooled.values():
        groups.setdefault((item["platform"], item["search_language"]), []).append(item)
    for group_items in groups.values():
        group_items.sort(key=lambda i: (bool(i.get("style_match")), i.get("growth_per_day") is not None,
                        i.get("growth_per_day") if i.get("growth_per_day") is not None else i.get("views_per_day") or 0),
                         reverse=True)
    ordered = [items[index] for index in range(max(map(len, groups.values()), default=0))
               for items in groups.values() if index < len(items)]
    sampled = set()
    for item in fav + ordered:
        if len(sampled) >= 6:
            break
        key = (item["platform"], item["video_id"])
        if item.get("own") or key in sampled or item["platform"] not in {"youtube", "vk"} or not item.get("comments"):
            continue
        sampled.add(key)
        try:
            lang = item.get("search_language") or language
            token = token_for(item["platform"], lang)
            if item["platform"] == "youtube":
                sample = youtube_comments(item["video_id"], token, item.get("comments"))
            else:
                from .marketer_vk import video_comments
                sample = video_comments(slug, item["video_id"], token, item.get("comments"))
            item["comments_sample"] = sample
        except MetricsUnavailable as exc:
            item["comments_sample"] = {"warning": str(exc)}
    return {"at": now.isoformat(), "searches": runs, "favorites": fav, "items": ordered[:120]}
