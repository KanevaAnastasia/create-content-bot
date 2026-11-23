"""Per-episode camera and prop continuity for grandmother scenes."""

SINGLE_TAKE_RULE = (
    "veo3_framing: one continuous locked-camera shot, fixed angle and framing; "
    "no cuts, montage, time jumps, zoom, pan, tracking or reframing; one simple main action; "
    "frame grandma waist-up with her face intact, hands and food large in the foreground; "
    "grandma's entire face and head remain visible in EVERY frame, including silent moments; "
    "hands, utensils and food never obscure her face; no face-free inserts or rear views; "
    "keep other listed characters recognizable; lower bodies may be outside the frame; "
    "no need to show floor, legs, furniture legs or the whole room; accurate lip sync; "
    "strongly stylized rounded cartoon grandma and cartoon cat, never photorealistic; "
    "the cat never eats, licks or touches human food."
)


def lock_episode_props(scenes: list[dict]) -> list[dict]:
    """Use the first declared design consistently without adding invisible props."""
    catalog = {}
    for scene in scenes:
        props = scene.get("prop_descriptions")
        if isinstance(props, dict):
            for key, value in props.items():
                if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
                    catalog.setdefault(key.strip(), value.strip())
    for scene in scenes:
        props = scene.get("prop_descriptions")
        props = props if isinstance(props, dict) else {}
        scene["single_take"] = True
        scene["episode_prop_catalog"] = dict(catalog)
        scene["prop_descriptions"] = {
            key.strip(): catalog[key.strip()] for key in props
            if isinstance(key, str) and key.strip() in catalog
        }
    return scenes


def append_episode_prop_locks(prompt: str, scene: dict) -> str:
    props = scene.get("prop_descriptions")
    if not isinstance(props, dict) or not props or "ПРЕДМЕТЫ ЭТОГО РОЛИКА:" in prompt:
        return prompt
    lines = [f"{key}: {value}" for key, value in props.items() if isinstance(value, str)]
    return prompt + "\n\nПРЕДМЕТЫ ЭТОГО РОЛИКА:\n" + "\n".join(lines) + (
        "\nЭти описания фиксируют внешность перечисленных предметов, но не отменяют "
        "постоянный советский интерьер, скатерть и оформление комнаты. "
        "Сохранять внешность и количество между сценами; не добавлять отсутствующие предметы."
    )
