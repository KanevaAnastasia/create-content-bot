"""Visual prop descriptions appended to prompts for frame-to-frame consistency."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import select

from app.character_catalog import GRANDMA_KITCHEN_PROJECT_SLUG
from app.db import SessionLocal
from app.models import Project, VisualProp

DEFAULT_PROJECT_SLUG = GRANDMA_KITCHEN_PROJECT_SLUG

logger = logging.getLogger("bot.visual_props")


@dataclass(frozen=True, slots=True)
class VisualPropSpec:
    canonical_key: str
    name_ru: str
    description_ru: str
    aliases: tuple[str, ...]


DEFAULT_VISUAL_PROPS: tuple[VisualPropSpec, ...] = (
    VisualPropSpec(
        "stove",
        "Плита",
        "Плита выглядит как аккуратная светлая варочная поверхность с четырьмя круглыми конфорками; передняя левая, передняя правая, задняя левая и задняя правая конфорки читаются по расположению, без логотипов и надписей.",
        ("плита", "плите", "плиту", "плитой", "конфорка", "конфорке", "конфорку", "stove", "burner", "hob"),
    ),
    VisualPropSpec(
        "pot",
        "Кастрюля",
        "Кастрюля выглядит как средняя светлая эмалированная кастрюля с округлыми боками, короткими боковыми ручками и тёмным ободком; она всегда узнаётся по форме и мягкому глянцу.",
        ("кастрюля", "кастрюле", "кастрюлю", "кастрюлей", "сотейник", "pot", "saucepan"),
    ),
    VisualPropSpec(
        "cauldron",
        "Казан",
        "Казан выглядит как глубокая тёмная чугунная посуда с округлым дном, толстыми стенками и двумя небольшими ушками-ручками; внутри хорошо видна масса блюда.",
        ("казан", "казане", "казаном", "cauldron", "dutch oven"),
    ),
    VisualPropSpec(
        "mixing_bowl",
        "Миска",
        "Миска выглядит как глубокая керамическая миска тёплого молочного цвета с мягко скруглённым краем; внутри хорошо видны ингредиенты и следы перемешивания.",
        ("миска", "миске", "миску", "миской", "чаша", "чаше", "bowl", "mixing bowl"),
    ),
    VisualPropSpec(
        "plate",
        "Тарелка",
        "Тарелка выглядит как круглая белая керамическая тарелка с лёгким бортиком и чистой глянцевой поверхностью; готовое блюдо лежит на ней аккуратно и аппетитно.",
        ("тарелка", "тарелке", "тарелку", "тарелкой", "блюдо", "plate", "dish"),
    ),
    VisualPropSpec(
        "grater",
        "Тёрка",
        "Тёрка выглядит как металлическая четырёхгранная тёрка с крупными и мелкими отверстиями, устойчиво стоящая на рабочей поверхности; металл матовый, без брендов.",
        ("тёрка", "терка", "тёрке", "терке", "тёрку", "терку", "grater"),
    ),
    VisualPropSpec(
        "knife",
        "Нож",
        "Нож выглядит как небольшой кухонный нож с серебристым лезвием и простой деревянной ручкой; лезвие чистое, без крови, логотипов и опасного акцента.",
        ("нож", "ножом", "ножа", "ножик", "knife"),
    ),
    VisualPropSpec(
        "spoon",
        "Ложка",
        "Ложка выглядит как обычная столовая ложка из матового серебристого металла с округлой чашей и простой ручкой; она остаётся тем же предметом между кадрами.",
        ("ложка", "ложке", "ложку", "ложкой", "spoon"),
    ),
    VisualPropSpec(
        "oven",
        "Духовка",
        "Духовка выглядит как встроенная светлая духовка с прозрачным тёмным стеклом дверцы и простой металлической ручкой; внутри тёплый оранжевый свет, без цифр и надписей.",
        ("духовка", "духовке", "духовку", "духовкой", "oven"),
    ),
    VisualPropSpec(
        "baking_tray",
        "Противень",
        "Противень выглядит как плоский тёмный металлический противень с низким бортиком и листом светлой пергаментной бумаги; продукты разложены на нём ровно и читаемо.",
        ("противень", "противне", "противнем", "baking tray", "sheet pan", "tray"),
    ),
    VisualPropSpec(
        "cutting_board",
        "Разделочная доска",
        "Разделочная доска выглядит как прямоугольная деревянная доска светло-медового цвета с мягкими округлыми углами и видимой текстурой дерева.",
        ("доска", "доске", "доску", "разделочная доска", "cutting board", "board"),
    ),
    VisualPropSpec(
        "tabletop",
        "Стол",
        "Стол выглядит как тёплая деревянная рабочая поверхность медового оттенка с мягкой текстурой дерева; это постоянная поверхность для посуды и ингредиентов.",
        ("стол", "столе", "стола", "столешница", "столешнице", "table", "tabletop", "countertop"),
    ),
    VisualPropSpec(
        "window",
        "Окно",
        "Окно выглядит как простое светлое окно с мягким дневным светом и без заметного вида наружу; оно даёт ровный уютный свет без отвлекающих деталей.",
        ("окно", "окна", "окне", "window"),
    ),
    VisualPropSpec(
        "mug",
        "Кружка",
        "Кружка выглядит как небольшая керамическая кружка тёплого цвета с простой ручкой, без надписей и логотипов; она стоит рядом как уютная деталь кадра.",
        ("кружка", "кружке", "кружку", "кружкой", "mug", "cup"),
    ),
    VisualPropSpec(
        "whisk",
        "Венчик",
        "Венчик выглядит как металлический кухонный венчик с тонкими петлями и простой ручкой; на петлях могут быть следы теста или крема.",
        ("венчик", "венчиком", "whisk"),
    ),
    VisualPropSpec(
        "blender",
        "Блендер",
        "Блендер выглядит как полноценный настольный блендер: прозрачная чаша-кувшин стоит и защёлкнута на устойчивой моторной базе с простыми кнопками; перед взбиванием чаша закрыта крышкой, продукты видны внутри, база стоит на рабочей поверхности и не висит в воздухе.",
        (
            "блендер",
            "блендере",
            "блендером",
            "блендерную чашу",
            "чаша блендера",
            "blender",
            "blender jar",
        ),
    ),
    VisualPropSpec(
        "mixer",
        "Миксер",
        "Миксер выглядит как ручной электрический миксер с моторным корпусом и двумя металлическими насадками-венчиками, опущенными в миску; если это стационарный миксер, чаша закреплена на базе под головкой миксера, всё стоит устойчиво на столе.",
        (
            "миксер",
            "миксером",
            "миксере",
            "ручной миксер",
            "стационарный миксер",
            "mixer",
            "hand mixer",
            "stand mixer",
        ),
    ),
    VisualPropSpec(
        "rolling_pin",
        "Скалка",
        "Скалка выглядит как гладкая деревянная скалка светлого цвета с округлыми ручками; она лежит на рабочей поверхности рядом с тестом.",
        ("скалка", "скалкой", "rolling pin"),
    ),
)


def seed_visual_props_for_project(session: Any, project: Project) -> int:
    """Idempotently seeds default visual prop descriptions for a project."""
    count = 0
    for idx, spec in enumerate(DEFAULT_VISUAL_PROPS):
        prop = session.scalar(
            select(VisualProp).where(
                VisualProp.project_id == project.id,
                VisualProp.canonical_key == spec.canonical_key,
            )
        )
        if prop is None:
            session.add(
                VisualProp(
                    project_id=project.id,
                    canonical_key=spec.canonical_key,
                    name_ru=spec.name_ru,
                    description_ru=spec.description_ru,
                    aliases=list(spec.aliases),
                    sort_order=idx,
                    is_active=True,
                    meta_json={},
                )
            )
        else:
            prop.name_ru = spec.name_ru
            prop.description_ru = spec.description_ru
            prop.aliases = list(spec.aliases)
            prop.sort_order = idx
            prop.is_active = True
        count += 1
    return count


def _word_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias.strip().lower())
    if re.search(r"[a-zа-яё0-9]$", escaped, flags=re.IGNORECASE):
        return re.compile(rf"(?<![a-zа-яё0-9]){escaped}(?![a-zа-яё0-9])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def _matches_alias(text: str, aliases: list[str]) -> bool:
    low = text.lower()
    for alias in aliases:
        a = str(alias).strip().lower()
        if not a:
            continue
        if _word_pattern(a).search(low):
            return True
    return False


@lru_cache(maxsize=1)
def load_visual_props() -> tuple[VisualPropSpec, ...]:
    """Loads active visual props from DB, falling back to defaults if DB is unavailable."""
    if not (os.getenv("DATABASE_URL") or "").strip():
        return DEFAULT_VISUAL_PROPS
    try:
        with SessionLocal() as session:
            project = session.scalar(select(Project).where(Project.slug == DEFAULT_PROJECT_SLUG))
            if project is None:
                return DEFAULT_VISUAL_PROPS
            rows = session.scalars(
                select(VisualProp)
                .where(VisualProp.project_id == project.id, VisualProp.is_active.is_(True))
                .order_by(VisualProp.sort_order, VisualProp.name_ru)
            ).all()
            if not rows:
                return DEFAULT_VISUAL_PROPS
            return tuple(
                VisualPropSpec(
                    canonical_key=row.canonical_key,
                    name_ru=row.name_ru,
                    description_ru=row.description_ru,
                    aliases=tuple(str(x) for x in (row.aliases or []) if str(x).strip()),
                )
                for row in rows
            )
    except Exception as e:
        logger.debug("visual props DB unavailable, using defaults: %s", e)
        return DEFAULT_VISUAL_PROPS


def matched_visual_props(prompt: str, *, limit: int = 6) -> list[VisualPropSpec]:
    """Returns visual props whose aliases are mentioned in prompt."""
    text = prompt or ""
    matches: list[VisualPropSpec] = []
    for prop in load_visual_props():
        if _matches_alias(text, list(prop.aliases)):
            matches.append(prop)
        if len(matches) >= limit:
            break
    return matches


def append_visual_prop_descriptions(prompt: str) -> str:
    """Appends fixed descriptions for mentioned visual props to the prompt."""
    base = (prompt or "").strip()
    if not base or "Визуальные константы предметов:" in base:
        return base
    props = matched_visual_props(base)
    if not props:
        return base
    lines = ["Визуальные константы предметов:"]
    for prop in props:
        lines.append(f"{prop.name_ru} выглядит так: {prop.description_ru}")
    lines.append("Если предмет появляется в нескольких кадрах, сохраняй его форму, материал, цвет и положение узнаваемыми.")
    return f"{base}\n\n" + "\n".join(lines)
