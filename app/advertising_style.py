"""Video presentation within advertising; independent of clip/post output."""
ADVICE_BUTTON = "Клип — совет"

ADVICE_RULE = (
    "ВЫБРАНА ПОДАЧА СОВЕТ. Это исключение из правила единственного рассказчика: "
    "первая сцена — живой разговор взрослой дочери с бабушкой, обе видны в кадре вместе с Тимошей. "
    "Дочь (daughter, не внучка) задаёт короткий вопрос с обращением Мам о конкретной проблеме кампании, "
    "бабушка (grandma) коротко отвечает и обещает показать приём. Не пересказывай вопрос словами 'моя дочка спросила'. "
    "В сцене 1 верни dialogue_mode='multi' и dialogue=[{speaker:'daughter',text:'вопрос'},"
    "{speaker:'grandma',text:'ответ'}]; speech_ru объединяет эти две реплики. "
    "Дальше бабушка от первого лица наглядно решает именно эту проблему, товар участвует в решении. "
    "Не превращай бытовой совет в рецепт. Товар остаётся видимым во всех кадрах. "
    "В production сохрани обе утверждённые реплики дословно и раздельные голоса; "
    "обычная проверка длительности и сокращение выполняются позже.\n"
)


def advice_dialogue(candidate: dict) -> list[dict]:
    raw = candidate.get("dialogue") if isinstance(candidate, dict) else None
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("Для подачи «Совет» нужен вопрос дочери и ответ бабушки в первой сцене.")
    result = []
    for item, speaker in zip(raw, ("daughter", "grandma")):
        if not isinstance(item, dict) or item.get("speaker") != speaker or not str(item.get("text") or "").strip():
            raise ValueError("В первой сцене сначала говорит взрослая дочь, затем бабушка.")
        result.append({"speaker": speaker, "text": " ".join(str(item["text"]).split())})
    return result


def apply_advice_dialogue(scene: dict, dialogue: list[dict]) -> None:
    """Preserve speaker ownership through production and persistence."""
    from .pipeline import _append_dialogue_video_prompt_metadata
    text = " ".join(item["text"] for item in dialogue)
    scene.update(dialogue_mode="multi", dialogue=dialogue, dialogue_by_lang={"ru": dialogue},
                 text=text, speech_by_lang={"ru": text})
    scene["characters"] = list(dict.fromkeys(["daughter", "grandma", "cat", *(scene.get("characters") or [])]))
    scene["speech_plan_by_lang"] = {"ru": [dict(item, order=i, language="ru") for i, item in enumerate(dialogue, 1)]}
    # Rebuild spoken prompt from approved action to remove any model-invented monologue.
    action = scene.get("display_action") or scene.get("recipe_step") or "A daughter asks her mother for advice."
    from .advertising_generation import advertising_video_continuity_en
    prompt = f"8-second vertical 9:16 shot. {action} {advertising_video_continuity_en()} No subtitles, no music."
    scene["video_prompt"] = _append_dialogue_video_prompt_metadata(prompt, dialogue, "ru", scene=scene)
    scene["video_prompt_by_lang"] = {"ru": scene["video_prompt"]}
    scene["speech_cues_by_lang"] = {}
