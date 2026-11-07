"""Daily observations, not provider-attributed daily analytics."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from copy import deepcopy

MOSCOW = ZoneInfo("Europe/Moscow")


def day_key(timestamp):
    return datetime.fromtimestamp(timestamp, MOSCOW).date().isoformat()


def snapshot_history(history, accounts, rows, timestamp):
    # Last observation of each day. Failed values remain missing, never zero.
    day = day_key(timestamp)
    point = {"day": day, "timestamp": timestamp,
             "accounts": deepcopy(accounts),
             "videos": [{"id": r["id"], "platform": r["platform"], "language": r["language"],
                         "external_post_id": r.get("external_post_id"),
                         "metrics": deepcopy(r.get("metrics", {}))} for r in rows]}
    return sorted([p for p in history if p["day"] != day] + [point], key=lambda p: p["day"])


def dashboard(report, rows):
    stamp = report.get("fetched_at")
    history = report.get("daily_snapshots", [])
    today = day_key(stamp) if stamp else None
    yesterday = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat() if today else None
    previous = next((p for p in history if p["day"] == yesterday), {})
    accounts = []
    def key(a):
        return a.get("platform"), a.get("language"), a.get("account_id")
    old = {key(a): a for a in previous.get("accounts", [])}
    for source in report.get("accounts", []):
        a = dict(source)
        current, before = a.get("followers"), old.get(key(a), {}).get("followers")
        a["daily_delta"] = current - before if current is not None and before is not None else None
        a["trend"] = []
        for p in history[-14:]:
            value = next((v.get("followers") for v in p["accounts"] if key(v) == key(a)), None)
            a["trend"].append({"day": p["day"], "value": value})
        accounts.append(a)
    old_rows = {r["id"]: r for r in previous.get("videos", [])}
    for row in rows:
        before = old_rows.get(row["id"], {}).get("metrics", {}).get("views")
        current = row.get("metrics", {}).get("views")
        row["daily_views"] = current - before if before is not None and current is not None else None
        row["daily_history"] = []
        for point in history[-14:]:
            observation = next((r for r in point.get("videos", []) if r["id"] == row["id"]), None)
            if observation is not None:
                row["daily_history"].append({"day": point["day"], **observation.get("metrics", {})})
    available = sum(r.get("metrics", {}).get("views") is not None for r in rows)
    actions = []
    missing = len(rows) - available
    if missing:
        actions.append({"title": "Проверить недоступные публикации", "detail": f"У {missing} публикаций нет просмотров. Откройте детали и проверьте предупреждение: это не нулевой результат.", "anchor": "videos"})
    if not previous:
        actions.append({"title": "Накопить первый дневной интервал", "detail": "Вчерашнего среза нет. Сегодня фиксируем базу; после следующего дневного сбора появится изменение. Старые просмотры не считаем новыми за день.", "anchor": "audience"})
    leaders = sorted([r for r in rows if r.get("relative") is not None and r["relative"] >= 1.5], key=lambda r: r["relative"], reverse=True)
    if leaders:
        r = leaders[0]
        actions.append({"title": "Проверить продолжение сильной темы", "detail": f"«{r['title'][:100]}» · {r['platform']} {r['language'].upper()}: {r['relative']}× медианы сопоставимых роликов. Сделайте смежную тему, изменив один элемент; сравните через одинаковое время. Это гипотеза, не доказанная причина успеха.", "anchor": "videos"})
    else:
        actions.append({"title": "Один понятный эксперимент", "detail": "В следующем ролике покажите результат в первые 2 секунды. Сохраните тему и длительность; сравните с похожими роликами на той же площадке через 7 дней. Без сопоставимой выборки победителя не объявляем.", "anchor": "videos"})
    return {"accounts": accounts, "actions": actions[:3], "available": available,
            "missing": missing, "days": len(history), "day": today,
            "comparison_day": yesterday if previous else None,
            "updated": datetime.fromtimestamp(stamp, MOSCOW) if stamp else None,
            "previous_time": datetime.fromtimestamp(previous["timestamp"], MOSCOW) if previous else None}
