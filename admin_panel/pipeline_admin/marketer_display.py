"""Small, escaped Markdown subset and evidence-only behavioural presentation."""
import re

from django.utils.html import escape
from django.utils.safestring import mark_safe


def format_brief(value):
    # Escape first. No model-provided HTML, images, CSS or links are executable.
    output, items = [], []
    def flush():
        if items:
            output.append("<ul>" + "".join(items) + "</ul>")
            items.clear()
    for raw in str(value or "").splitlines():
        line = str(escape(raw.strip()))
        line = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        item = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+)$", line)
        if item:
            items.append("<li>" + item[1] + "</li>")
        else:
            flush()
            if heading:
                output.append("<h3>" + heading[1] + "</h3>")
            elif line:
                output.append("<p>" + line + "</p>")
    flush()
    return mark_safe("".join(output))


def behaviour_summary(row):
    """No inference of per-second likes/swipes from aggregate counters."""
    data = row.get("behaviour") or {}
    if row.get("platform") == "instagram":
        metrics = row.get("metrics") or {}
        data = {"subscribers_gained": metrics.get("subscribers_gained"),
                "average_view_seconds": round(metrics["average_watch_ms"] / 1000, 2) if metrics.get("average_watch_ms") is not None else None,
                "period": "За всё время (Instagram lifetime)", "fetched_at": row.get("fetched_at")}
    points = data.get("retention") or []
    if row.get("platform") == "youtube":
        subscriber_status = ("Ошибка обновления YouTube Analytics — см. подробности" if data.get("warning") else
                             "YouTube пока не вернул подписки за выбранный период" if data else "Ещё не запрошено — обновите данные")
    elif row.get("platform") == "instagram":
        subscriber_status = (row.get("metric_errors") or {}).get("subscribers_gained") or "Instagram пока не вернул подписки"
    else:
        subscriber_status = "Подписки с ролика не доступны в подключённом API"
    drops = sorted([{"start": a["position"], "end": b["position"],
                     "drop": round(a["watch_ratio"] - b["watch_ratio"], 2)}
                    for a, b in zip(points, points[1:]) if a["watch_ratio"] > b["watch_ratio"]],
                   key=lambda p: p["drop"], reverse=True)[:3]
    if data.get("duration_seconds") and row.get("platform") == "youtube" and re.fullmatch(r"[\w-]{11}", str(row.get("external_post_id", ""))):
        for drop in drops:
            drop["seconds"] = round(drop["start"] * data["duration_seconds"] / 100, 1)
            drop["url"] = f"https://www.youtube.com/watch?v={row['external_post_id']}&t={int(drop['seconds'])}s"
    return {
        "subscriber_status": subscriber_status,
        "subscribers_gained": data.get("subscribers_gained"),
        "subscribers_lost": data.get("subscribers_lost"),
        "average_view_seconds": data.get("average_view_seconds"),
        "average_view_percentage": data.get("average_view_percentage"),
        "period": data.get("period") or "Период ещё не получен",
        "fetched_at": data.get("fetched_at"),
        "retention_available": bool(points), "points": points, "drops": drops,
        "warning": data.get("warning") or data.get("retention_warning"),
        "subscribers_warning": (row.get("metric_errors") or {}).get("subscribers_gained"),
        "skip_rate": (row.get("metrics") or {}).get("skip_rate"),
        "total_watch_hours": round(row["metrics"]["total_watch_ms"] / 3600000, 2) if (row.get("metrics") or {}).get("total_watch_ms") is not None else None,
        "total_interactions": (row.get("metrics") or {}).get("total_interactions"),
    }
