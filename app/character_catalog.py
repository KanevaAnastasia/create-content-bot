"""Project character descriptions and lightweight story rules."""

from __future__ import annotations

import random
import re
from functools import lru_cache
from typing import Any, Callable


GRANDMA_KITCHEN_PROJECT_SLUG = "grandma_kitchen"
GRANDMA_KITCHEN_PROJECT_TITLE = "Grandma Kitchen"


CHARACTER_CATALOG: dict[str, dict[str, Any]] = {
    "grandma": {
        "name": "Бабушка",
        "short_description": "бабушка",
        "role": "главная хозяйка кухни",
        "description": (
            "Невысокая пожилая мультяшная женщина с мягкой округлой фигурой, крупной головой в стилизованных "
            "пропорциях, круглым лицом, маленьким подбородком, пухлыми румяными щеками, теплой улыбкой и добрыми "
            "карими глазами за тонкими круглыми очками в медной оправе. У нее аккуратные седые волосы с пробором, "
            "видимые у лба под светлым кремовым платком с красными розами, зелеными листочками и завязанным узлом "
            "под подбородком. Носит темно-синее домашнее платье с мелким цветочным узором и закатанными рукавами, "
            "поверх него светлый цветочный фартук с крупными розами, ягодами, зеленой листвой и красно-розовой окантовкой. "
            "На запястьях деревянные браслеты-бусины, на ногах мягкие коричневые тапочки. Нос круглый и красноватый, "
            "брови светло-седые дугой, руки мягкие и пухлые, поза уверенная и заботливая."
        ),
    },
    "cat": {
        "name": "Тимоша",
        "short_description": "рыжий кот",
        "role": "главный домашний кот",
        "description": (
            "Пушистый рыжевато-белый мультяшный домашний кот с округлой мордочкой, большими выразительными "
            "глазами, розовым носиком, светлой пушистой грудкой и мягкими белыми лапами. Основной окрас теплый "
            "рыжевато-кремовый с белой мордочкой, белым животом и светлыми пятнами на лапах; хвост пышный, слегка "
            "изогнутый, уши треугольные с розовой внутренней частью. Взгляд добрый и немного наивный, короткие "
            "белые усы, округлые щеки и мягкая мультяшная пластика. Часто стоит с вытянутой шеей к еде или "
            "прижимает лапы к краю стола."
        ),
    },
    "zaya": {
        "name": "Зая",
        "short_description": "белая кошка",
        "role": "соседская белая кошечка",
        "description": (
            "Маленькая изящная полностью белая мультяшная кошечка с шелковистой пушистой шерстью, компактным телом, "
            "тонкой шейкой, округлой мордочкой и большими бирюзово-голубыми глазами с темными зрачками и яркими бликами. "
            "Маленький розовый треугольный носик, нежные розовые внутренние ушки, тонкие светлые бровки, длинные "
            "белые усики и мягкая улыбка. Хвост очень пушистый, крупный и закручен полукольцом у лап; одна передняя лапка "
            "часто изящно приподнята, будто она аккуратно машет или кокетливо просит внимания. Шерсть должна оставаться "
            "чистой белой или слегка теплой кремовой от освещения, без темных пятен, полос и цветных отметин."
        ),
    },
    "markiz": {
        "name": "Маркиз",
        "short_description": "черно-белый кот",
        "role": "соседский черно-белый кот",
        "description": (
            "Пушистый черно-белый мультяшный кот с аккуратной сидячей осанкой, округлой мордочкой, большими "
            "круглыми темно-карими глазами с яркими бликами и маленьким розовым носом. Черная шерсть покрывает верх "
            "головы, уши, спину, бока и большую часть хвоста; по центру мордочки идет широкая белая вертикальная полоса, "
            "которая спускается к белой мордочке, белой пушистой грудке и животу. Лапы белые, как мягкие носочки; на теле "
            "видны контрастные белые участки на груди, боку и передних лапах. Уши крупные треугольные с розовой внутренней "
            "частью, хвост темный и пушистый, поза чуть самодовольная, но милая."
        ),
    },
    "dog": {
        "name": "Пес",
        "short_description": "пес",
        "role": "добродушный семейный пес",
        "description": (
            "Домашний мультяшный пес среднего размера: мягкая округлая морда, "
            "крупный темный нос, добрые блестящие глаза, висячие уши и слегка высунутый язык. Шерсть теплая коричнево-бежевая "
            "или рыжеватая, грудка светлее, лапы округлые и мягкие."
        ),
    },
    "daughter": {
        "name": "Дочка",
        "short_description": "дочка",
        "role": "взрослая дочь бабушки",
        "description": (
            "Мультяшная взрослая молодая женщина с мягкими семейными чертами, добрым открытым лицом, "
            "теплой улыбкой и аккуратной уютной одеждой. Она выглядит как дочь бабушки, заботливая взрослая "
            "помощница на кухне, а не маленький ребенок."
        ),
    },
    "granddaughter": {
        "name": "Внучка",
        "short_description": "внучка",
        "role": "юная помощница бабушки",
        "description": (
            "Мультяшная маленькая девочка с мягкими округлыми чертами лица, большими добрыми карими глазами "
            "за тонкими круглыми очками, веснушками, румяными щеками и аккуратной детской улыбкой. У нее светло-рыжие "
            "кудрявые волосы, собранные в два пышных хвостика с розовыми бантиками. Носит розовый сарафан поверх "
            "светлой блузки с мелким узором, светлые колготки с маленькими сердечками и звездочками, розовые ботинки "
            "и небольшую розовую сумочку через плечо."
        ),
    },
    "grandfather": {
        "name": "Дедушка",
        "short_description": "дедушка",
        "role": "спокойный семейный дегустатор",
        "description": (
            "Мультяшный пожилой мужчина с мягкой округлой фигурой, добрыми глазами, "
            "седыми бровями, небольшими седыми усами или щетиной, заметными морщинками улыбки и спокойной хитрой улыбкой. "
            "Носит простую домашнюю рубашку или жилет в приглушенных теплых цветах."
        ),
    },
    "grandson": {
        "name": "Внук",
        "short_description": "внук",
        "role": "маленький помощник и зритель",
        "description": (
            "Мультяшный маленький мальчик с крупными любопытными глазами, круглым детским лицом, мягкими щеками, "
            "короткими аккуратными волосами и живой улыбкой. Носит простую яркую домашнюю футболку или свитер и маленький "
            "фартук."
        ),
    },
}


def _static_character_prompt_catalog(keys: list[str] | None = None) -> dict[str, dict[str, str]]:
    """Return compact static character descriptions suitable for LLM payloads."""
    source_keys = keys or list(CHARACTER_CATALOG)
    out: dict[str, dict[str, str]] = {}
    for key in source_keys:
        item = CHARACTER_CATALOG.get(key)
        if not item:
            continue
        out[key] = {
            "name": str(item.get("name") or key),
            "short_description": str(item.get("short_description") or item.get("name") or key),
            "role": str(item.get("role") or ""),
            "description": str(item.get("description") or ""),
        }
    return out


@lru_cache(maxsize=8)
def _db_character_prompt_catalog(project_slug: str) -> dict[str, dict[str, str]]:
    """Load approved project character descriptions from DB, falling back silently on any DB issue."""
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import Character, Project, ProjectCharacter
    except Exception:
        return {}
    try:
        with SessionLocal() as session:
            rows = session.execute(
                select(
                    Character.canonical_key,
                    Character.name,
                    Character.role,
                    Character.description,
                    Character.meta_json,
                    ProjectCharacter.asset_uri,
                    ProjectCharacter.meta_json,
                )
                .join(ProjectCharacter, ProjectCharacter.character_id == Character.id)
                .join(Project, Project.id == ProjectCharacter.project_id)
                .where(Project.slug == project_slug, Character.is_active.is_(True))
                .order_by(ProjectCharacter.sort_order, Character.canonical_key)
            ).all()
    except Exception:
        return {}
    out: dict[str, dict[str, str]] = {}
    for canonical_key, name, role, description, character_meta, asset_uri, project_character_meta in rows:
        key = str(canonical_key or "").strip()
        if not key:
            continue
        char_meta = character_meta if isinstance(character_meta, dict) else {}
        project_meta = project_character_meta if isinstance(project_character_meta, dict) else {}
        short_description = (
            str(project_meta.get("short_description") or char_meta.get("short_description") or "").strip()
            or str(name or key)
        )
        out[key] = {
            "name": str(name or key),
            "short_description": short_description,
            "role": str(role or ""),
            "description": str(description or ""),
            "asset_uri": str(asset_uri or ""),
        }
    return out


def character_prompt_catalog(
    keys: list[str] | None = None,
    *,
    project_slug: str = GRANDMA_KITCHEN_PROJECT_SLUG,
) -> dict[str, dict[str, str]]:
    """Return approved DB character descriptions for prompts, with the code catalog as fallback."""
    db_catalog = _db_character_prompt_catalog(project_slug)
    source = db_catalog or _static_character_prompt_catalog()
    if keys is None:
        return dict(source)
    out: dict[str, dict[str, str]] = {}
    static_fallback = _static_character_prompt_catalog(keys)
    for key in keys:
        if key in source:
            out[key] = source[key]
        elif key in static_fallback:
            out[key] = static_fallback[key]
    return out


def character_identity_lines(keys: list[str]) -> list[str]:
    """Human-readable identity lines for image/video prompt consistency."""
    catalog = character_prompt_catalog(keys)
    lines: list[str] = []
    for key in keys:
        item = catalog.get(key)
        if not item:
            continue
        label = str(item.get("short_description") or item.get("name") or key)
        lines.append(f"{label}: {sanitize_character_names_and_keys(item['description'])}")
    return lines


def character_short_description(key: str) -> str:
    """DB-first short human label for a canonical character key."""
    normalized = str(key or "").strip().lower()
    if not normalized:
        return ""
    item = character_prompt_catalog([normalized]).get(normalized) or {}
    return str(item.get("short_description") or item.get("name") or normalized).strip() or normalized


def character_short_description_list(keys: list[str]) -> list[str]:
    """DB-first short labels for user-facing character lists."""
    out: list[str] = []
    for key in keys:
        label = character_short_description(str(key))
        if label and label not in out:
            out.append(label)
    return out


def format_character_short_descriptions(keys: list[str]) -> str:
    """Format canonical keys as user-facing short descriptions."""
    labels = character_short_description_list(keys)
    return ", ".join(labels)


def sanitize_character_names_and_keys(text: str) -> str:
    """Replace character names and non-human canonical keys in generated text with DB-first short labels."""
    value = str(text or "")
    if not value:
        return ""
    shorts = {
        key: character_short_description(key)
        for key in ("cat", "zaya", "markiz", "grandma", "daughter", "granddaughter", "grandfather", "grandson", "dog")
    }
    replacements = (
        (r"\bTimosha\b", shorts.get("cat", "рыжий кот")),
        (r"\bZaya\b", shorts.get("zaya", "белая кошка")),
        (r"\bMarkiz\b", shorts.get("markiz", "черно-белый кот")),
        (r"\bzaya\b", shorts.get("zaya", "белая кошка")),
        (r"\bmarkiz\b", shorts.get("markiz", "черно-белый кот")),
        (r"\bdaughter\b", shorts.get("daughter", "дочка")),
        (r"\bgranddaughter\b", shorts.get("granddaughter", "внучка")),
        (r"\bgrandfather\b", shorts.get("grandfather", "дедушка")),
        (r"\bgrandson\b", shorts.get("grandson", "внук")),
        (r"\bТимошей\b", "рыжим котом"),
        (r"\bТимошу\b", "рыжего кота"),
        (r"\bТимоши\b", "рыжего кота"),
        (r"\bТимоша\b", shorts.get("cat", "рыжий кот")),
        (r"\bЗаей\b", "белой кошкой"),
        (r"\bЗаю\b", "белую кошку"),
        (r"\bЗаи\b", "белой кошки"),
        (r"\bЗая\b", shorts.get("zaya", "белая кошка")),
        (r"\bМаркизом\b", "черно-белым котом"),
        (r"\bМаркиза\b", "черно-белого кота"),
        (r"\bМаркиз\b", shorts.get("markiz", "черно-белый кот")),
    )
    out = value
    for pattern, replacement in replacements:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    return out


def maybe_choose_romantic_subplot(
    *,
    random_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Choose the episode-level white-cat/black-and-white-cat background drama at code level."""
    draw = random_fn or random.random
    series_rule = (
        "Grandma Kitchen keeps stable relationship canon across the series: the orange cat and the white cat are a pair; "
        "the black-and-white cat is the white cat's admirer only in episodes where he is selected."
    )
    if draw() >= 0.50:
        return {
            "enabled": False,
            "characters": [],
            "brief": "",
            "decision_scope": "episode",
            "series_rule": series_rule,
        }
    include_markiz = draw() < 0.10
    characters = ["zaya", "markiz"] if include_markiz else ["zaya"]
    return {
        "enabled": True,
        "characters": characters,
        "decision_scope": "episode",
        "brief": "",
        "zaya_timosha_arc": "",
        "markiz_arc": "",
        "series_rule": series_rule,
        "llm_should_create_drama_case": True,
        "episode_rule": (
            "If enabled, the LLM must invent one recipe-aware drama case for the whole episode. It must be visible "
            "as a subtle background beat in every scene, while grandma's recipe remains the main story. The subplot "
            "may be romantic, emotional, situational, or comedic-dramatic, but it must have a clear happy ending in "
            "the final scene that viewers can understand from the visible action."
        ),
        "rules": (
            "This is a subtle background subplot, not the main recipe. Keep food and grandma's cooking action primary. "
            "Include selected subplot characters by canonical keys in every scene.characters and keep them on model. "
            "Avoid defaulting the white cat to a fixed position outside the window unless the specific beat calls for it."
        ),
    }
