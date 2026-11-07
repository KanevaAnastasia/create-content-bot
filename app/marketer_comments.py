"""Bounded comment sample: transparent rules, not sentiment or causal claims."""
import re


def summarize_comments(items, more=False):
    comments = [{"text": str(item.get("text") or "")[:2000], "timestamp": str(item.get("timestamp") or "")[:50]} for item in items[:30]]
    rules = [
        ("Вопросы", r"\?|\bкак\b|\bчем\b|\bсколько\b|\bhow\b|\bwhy\b", "Ответьте на конкретный вопрос; повторяющиеся вопросы — тема короткого объяснения."),
        ("Просьбы и идеи", r"покаж|снимите|рецепт|хочу|please|recipe|show", "Проверьте, просят ли зрители новый рецепт, пример или продолжение. Выберите одну просьбу для следующего ролика."),
        ("Возможные затруднения", r"не понял|не получ|не работает|непонят|doesn.t work|confus", "Уточните проблему у автора и проверьте, нужен ли поясняющий шаг в следующем видео."),
    ]
    groups = []
    for title, pattern, action in rules:
        matches = [c for c in comments if re.search(pattern, c["text"], re.I)]
        if matches:
            groups.append({"title": title, "count": len(matches), "examples": matches[:3], "action": action})
    return {"count": len(comments), "more": more, "groups": groups, "items": comments,
            "method": "Подсказки по словам и знаку вопроса, не AI-оценка настроения. Категории могут пересекаться; проверьте цитаты."}
