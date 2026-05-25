"""Media/reference helper functions extracted from handlers (part 3)."""

from __future__ import annotations

from typing import Any

_SCENE_PLAN_PHASE_TITLES: dict[str, str] = {
    "initial_validation": "ответ модели прошёл, но не прошёл авто-проверку",
    "after_fix": "после авто-правки JSON снова не прошёл проверку (часто ругается поле «Фраза» для Veo3, а не отсутствие еды в действии)",
    "fix_exception": "авто-правка сцен завершилась с ошибкой",
    "soft_fail_final": "остались замечания — отдаю текущий вариант",
    "llm_error": "не удалось получить ответ от модели",
}


def _format_scenes_plan_attempt_notice(payload: dict[str, Any]) -> str:
    """
    Короткое сообщение в чат после каждой неудачной проверки в ``generate_scenes_plan``.
    """
    attempt = int(payload.get("attempt", 1))
    max_a = int(payload.get("max_attempts", 3))
    phase = str(payload.get("phase", ""))
    errs = payload.get("errors_human") or []
    if not isinstance(errs, list):
        errs = [str(errs)]
    scene = payload.get("problem_scene")
    continuing = bool(payload.get("continuing", True))
    title = _SCENE_PLAN_PHASE_TITLES.get(phase, "проверка сценария")
    lines = [
        f"Шаг адаптации {attempt}/{max_a}: {title}.",
        "",
        "Что не так:",
    ]
    for e in errs[:12]:
        lines.append(f"• {e}")
    lines.append("")
    if isinstance(scene, dict):
        anchors = scene.get("continuity_anchors") if isinstance(scene.get("continuity_anchors"), list) else []
        anchors_text = ", ".join(str(x) for x in anchors if str(x).strip()) or "—"
        lines.append("Пример сцены:")
        lines.append(
            f"№{scene.get('scene', '?')} | модель: {scene.get('model', '—')} | опоры: {anchors_text}"
        )
        lines.append(f"Действие: {scene.get('action', '—')}")
        lines.append(f"Фраза: {scene.get('text', '—')}")
        dd = scene.get("dish_description")
        if isinstance(dd, str) and dd.strip():
            lines.append(f"Описание блюда: {dd.strip()}")
    lines.append("")
    if continuing:
        lines.append("Продолжаю адаптировать сценарий…")
    else:
        lines.append("Дальше пришлю полный план — можно править текстом или нажать ОК.")
    return "\n".join(lines)


def _format_scenes_regeneration_report(events: list[dict]) -> str:
    if not events:
        return ""
    last = events[-1]
    errs = last.get("errors_human") or last.get("errors") or []
    if not isinstance(errs, list):
        errs = [str(errs)]
    if last.get("soft_fail_accepted"):
        lines = [
            "Авто-проверка нашла замечания, но сценарий всё равно отправлен — можете править текстом или нажать ОК.",
            "Замечания:",
        ]
    else:
        lines = ["Перегенерируем сценарий, были такие ошибки:"]
    for e in errs[:8]:
        lines.append(f"- {e}")
    scene = last.get("problem_scene")
    if isinstance(scene, dict):
        anchors = scene.get("continuity_anchors") if isinstance(scene.get("continuity_anchors"), list) else []
        anchors_text = ", ".join(str(x) for x in anchors if str(x).strip()) or "—"
        lines.append("")
        lines.append("Пример сцены с замечанием (для ориентира):")
        lines.append(
            f"Сцена {scene.get('scene', '?')}: длительность={scene.get('duration_sec', '?')}, "
            f"модель={scene.get('model', '?')}, опоры={anchors_text}"
        )
        lines.append(f"Что происходит: {scene.get('action', '—')}")
        lines.append(f"Фраза: {scene.get('text', '—')}")
        dd = scene.get("dish_description")
        if isinstance(dd, str) and dd.strip():
            lines.append(f"Описание блюда: {dd.strip()}")
    return "\n".join(lines)
