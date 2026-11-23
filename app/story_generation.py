"""Gemini + ElevenLabs planning helpers for the narrated ``story`` mode."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .discipline_modes import (
    ARCHIVE, TWO_LIVES, FLOW, LABELS, MODE_KEY, REFRAME, SHORT_FORMATS, opening_sentence, script_direction, visual_direction,
)
from .llm_utils import GEMINI_MODEL_PROFILE_CREATIVE, extract_json_object, gemini_generate_sync


STATE_WAITING_STORY_SCRIPT_APPROVAL = "waiting_story_script_approval"
STATE_REFINING_STORY_SCRIPT = "refining_story_script"
STATE_WAITING_STORY_AUDIO_APPROVAL = "waiting_story_audio_approval"
STATE_WAITING_STORY_ENGLISH_CHOICE = "waiting_story_english_choice"
STATE_WAITING_STORY_STYLE_CHOICE = "waiting_story_style_choice"
STATE_WAITING_STORY_ENGLISH_SCRIPT_APPROVAL = "waiting_story_english_script_approval"
STATE_WAITING_STORY_ENGLISH_AUDIO_APPROVAL = "waiting_story_english_audio_approval"

STORY_SOURCE_SESSION_KEY = "story_source_text"
STORY_TITLE_SESSION_KEY = "story_title"
STORY_VOICEOVER_SESSION_KEY = "story_voiceover_text"
STORY_AUDIO_PATH_SESSION_KEY = "story_audio_path"
STORY_AUDIO_DURATION_SESSION_KEY = "story_audio_duration_sec"
STORY_AUDIO_ALIGNMENT_SESSION_KEY = "story_audio_alignment"
STORY_AUDIO_VERSIONS_SESSION_KEY = "story_audio_versions"
STORY_VOICEOVER_VERSIONS_SESSION_KEY = "story_voiceover_versions"
STORY_ENGLISH_REQUESTED_SESSION_KEY = "story_english_requested"
STORY_VISUAL_STYLE_SESSION_KEY = "story_visual_style"
STORY_CUSTOM_SCRIPT_INPUT_PENDING_SESSION_KEY = "story_custom_script_input_pending"
STORY_TITLE_EN_SESSION_KEY = "story_title_en"
STORY_VOICEOVER_EN_SESSION_KEY = "story_voiceover_text_en"
STORY_VOICEOVER_VERSIONS_EN_SESSION_KEY = "story_voiceover_versions_en"
STORY_AUDIO_PATH_EN_SESSION_KEY = "story_audio_path_en"
STORY_AUDIO_DURATION_EN_SESSION_KEY = "story_audio_duration_sec_en"
STORY_AUDIO_ALIGNMENT_EN_SESSION_KEY = "story_audio_alignment_en"
STORY_AUDIO_VERSIONS_EN_SESSION_KEY = "story_audio_versions_en"
STORY_CHARACTER_BIBLE_SESSION_KEY = "story_character_bible"
STORY_CHARACTER_REFERENCES_SESSION_KEY = "story_character_references"
STORY_CHARACTER_INDEX_SESSION_KEY = "story_character_approval_index"
STATE_WAITING_STORY_CHARACTER_APPROVAL = "waiting_story_character_approval"
STORY_MAX_DURATION_SEC = 120.0
STORY_TARGET_DURATION_SEC = 110.0
STORY_CLIP_DURATION_SEC = 3.0
STORY_VOICEOVER_MAX_CHARS = 1520
STORY_VOICEOVER_MAX_WORDS = 230
STORY_SAFE_ZOOM_IN_DIRECTION = "slow optical zoom-in within the existing frame"
STORY_CAMERA_DIRECTION_SEQUENCE = (
    "tracking shot",
    "POV shot",
    STORY_SAFE_ZOOM_IN_DIRECTION,
    "over-the-shoulder shot",
    "gentle orbit shot",
)
STORY_PRESENT_ENVIRONMENT_SEQUENCE = (
    (
        "уютный домашний кабинет у большого окна",
        "мягкий прохладный утренний свет с тёплыми солнечными акцентами",
        "приглушённые синие, медовые и кремовые тона",
    ),
    (
        "просторный светлый холл или гостиная с высокими окнами",
        "рассеянный дневной свет и мягкие естественные тени",
        "шалфейно-зелёные, терракотовые и тёплые нейтральные тона",
    ),
    (
        "тихая библиотека или современный коворкинг",
        "ровный свет из высоких окон с локальными янтарными лампами",
        "глубокий бирюзовый, ореховый и янтарный",
    ),
    (
        "зелёный городской двор, парк или открытая терраса",
        "чистый естественный дневной свет с живыми бликами",
        "свежие зелёные, небесно-голубые и солнечно-жёлтые тона",
    ),
    (
        "просторный творческий зал с высокими окнами",
        "выразительный боковой свет с мягким градиентом",
        "коралловые, индиго и сливочные тона",
    ),
    (
        "тихая городская улица или площадь с глубиной перспективы",
        "золотой свет позднего дня",
        "золотистые, пыльно-голубые и кирпично-красные тона",
    ),
)
STORY_PAST_ENVIRONMENT_SEQUENCE = (
    (
        "исторически достоверный жилой интерьер соответствующей эпохи",
        "мягкий естественный свет из окна и тёплое практическое освещение эпохи",
        "натуральные охристые, пыльно-синие и приглушённо-зелёные тона",
    ),
    (
        "исторически достоверная улица или городской двор соответствующей эпохи",
        "естественный дневной свет с атмосферной глубиной",
        "каменные, терракотовые и небесно-голубые тона",
    ),
    (
        "просторный исторический зал, мастерская или библиотека соответствующей эпохи",
        "направленный свет из высоких окон и мягкие глубокие тени",
        "винные, тёмно-зелёные и тёплые кремовые тона",
    ),
    (
        "исторически достоверный сад, парк или загородный двор",
        "живой естественный свет с мягкими солнечными бликами",
        "оливковые, небесно-голубые и золотистые тона",
    ),
)
STORY_RETRO_VISUAL_STYLE = (
    "hand-drawn dry watercolor and fine ink linework on visibly textured off-white paper, "
    "clearly distinct yet muted dusty blue, olive green, warm ochre and faded terracotta accents with natural skin tones, "
    "delicate imperfect edges, sepia only as a subtle shadow wash, never monochrome beige, brown or gray, "
    "intimate editorial illustration, full-bleed artwork extending to all four image edges, "
    "no white border, no paper margin, no mat, no frame, never photorealistic, no readable text"
)
STORY_RETRO_STYLE_REFERENCE_PATH = "assets/story/retro_watercolor_reference.jpg"
STORY_STYLE_WATERCOLOR = "watercolor"
STORY_STYLE_PRIMARY = "primary"
STORY_STYLE_RETRO_CARTOON = "retro_cartoon"
STORY_STYLE_STORYBOOK_GOUACHE = "storybook_gouache"
STORY_STYLE_CLAYMATION = "claymation"
STORY_STYLE_FELT_STOP_MOTION = "felt_stop_motion"
STORY_STYLE_RETRO_FAIRYTALE = "retro_fairytale"
STORY_STYLE_TWILIGHT_WATERCOLOR = "twilight_watercolor"
STORY_STYLE_SOFT_ANIMATION = "soft_animation"
STORY_STYLE_CLASSIC_CEL_CARTOON = "classic_cel_cartoon"
STORY_STYLE_FELT_APPLIQUE = "felt_applique"
STORY_STYLE_FOLK_FAIRYTALE = "folk_fairytale"
STORY_STYLE_OIL_PASTEL = "oil_pastel"
STORY_STYLE_CROCHET = "crochet"
STORY_STYLE_MINIMAL_RETRO_CARTOON = "minimal_retro_cartoon"
STORY_DEFAULT_VISUAL_STYLE = STORY_STYLE_WATERCOLOR
STORY_RETRO_CARTOON_VISUAL_STYLE = (
    "hand-drawn mid-century 1950s–1960s retro cartoon editorial illustration, hand-painted gouache and ink on textured paper, "
    "bold angular silhouettes, exaggerated expressive proportions, simplified geometric anatomy, crisp charcoal contour lines, "
    "flat poster-like color blocks in muted olive green, mustard ochre, faded coral, warm cream and charcoal black, "
    "subtle vintage print grain, sparse period-accurate scenery, full-bleed artwork extending to all four image edges, "
    "no white border, no paper margin, no mat, no frame, never photorealistic, no 3D, no readable text, captions, page numbers, "
    "signatures, logos or watermarks; borrow only the graphic language of the style reference, never its people, room, objects or composition"
)
STORY_RETRO_CARTOON_REFERENCE_PATH = "assets/story/retro_cartoon_reference.jpg"
STORY_STORYBOOK_GOUACHE_VISUAL_STYLE = (
    "warm hand-painted children's storybook illustration in opaque gouache and dry-brush tempera on subtly textured paper, "
    "friendly expressive faces with believable anatomy, lively ink-and-brush contours, dense cozy domestic details, "
    "deep muted navy, warm cream, brick red, ochre and leafy green palette, softly uneven handmade paint coverage, "
    "nostalgic Eastern European picture-book mood, full-bleed artwork extending to all four image edges, "
    "no white border, no paper margin, no mat, no frame, no photorealism, no 3D, no readable text, page numbers, signatures, logos or watermarks; "
    "use only this painting language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_STORYBOOK_GOUACHE_REFERENCE_PATH = "assets/story/storybook_gouache_reference.jpg"
STORY_CLAYMATION_VISUAL_STYLE = (
    "meticulously handcrafted claymation stop-motion film frame, characters sculpted from matte polymer clay with visible fingerprints and tiny handmade imperfections, "
    "rounded playful facial features, expressive glassy eyes, miniature practical kitchen set, painted ceramic props, warm tungsten light and shallow depth of field, "
    "cozy saturated burgundy, teal, cream and orange palette, tactile physical materials, cinematic macro photography, "
    "full-bleed vertical frame extending to every edge, no border, no frame, no flat illustration, no glossy CGI or generic 3D render, "
    "no readable text, page numbers, signatures, logos or watermarks; use only the material and animation language, never copy the reference people, cat, room, food or composition"
)
STORY_CLAYMATION_REFERENCE_PATH = "assets/story/claymation_reference.jpg"
STORY_FELT_STOP_MOTION_VISUAL_STYLE = (
    "handmade puppet stop-motion film frame built from wool felt, needle-felted fibers, fabric, thread and miniature practical props, "
    "soft fuzzy surfaces and clearly tactile seams, gently caricatured but warm expressive faces, articulated cloth costumes, "
    "cozy old kitchen set under amber practical light, natural cinematic shadows and shallow depth of field, muted burgundy, linen, cream and ginger palette, "
    "full-bleed vertical frame extending to every edge, no border, no frame, no clay, no plastic sheen, no glossy CGI or generic 3D render, "
    "no readable text, page numbers, signatures, logos or watermarks; use only the tactile puppet language, never copy the reference people, cat, room, food or composition"
)
STORY_FELT_STOP_MOTION_REFERENCE_PATH = "assets/story/felt_stop_motion_reference.jpg"
STORY_RETRO_FAIRYTALE_VISUAL_STYLE = (
    "hand-painted Soviet-era 1960s–1980s children's fairytale animation cel and book illustration, opaque gouache and tempera, "
    "clean confident brush contours, simplified rounded shapes, charming expressive faces, layered painted backgrounds and gentle theatrical staging, "
    "warm cream, dark wood, muted burgundy, cobalt blue and ginger-orange palette with subtle paper grain, nostalgic winter-story atmosphere, "
    "full-bleed artwork extending to all four image edges, no white border, no paper margin, no mat, no frame, no photorealism, no 3D, "
    "no readable text, page numbers, signatures, logos or watermarks; use only this animation language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_RETRO_FAIRYTALE_REFERENCE_PATH = "assets/story/retro_fairytale_reference.jpg"
STORY_TWILIGHT_WATERCOLOR_VISUAL_STYLE = (
    "atmospheric hand-painted twilight watercolor picture-book illustration on rough cold-pressed paper, "
    "layered transparent washes with softly bleeding edges, restrained graphite and dry-brush contours, gentle expressive faces and natural proportions, "
    "deep indigo and smoky blue shadows balanced by amber lamplight, faded terracotta and warm biscuit tones, intimate quiet evening mood, "
    "subtle paper grain and imperfect pigment pooling, full-bleed artwork extending to all four image edges, "
    "no white border, no paper margin, no mat, no frame, no photorealism, no 3D, no readable text, page numbers, signatures, logos or watermarks; "
    "use only this watercolor language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_TWILIGHT_WATERCOLOR_REFERENCE_PATH = "assets/story/twilight_watercolor_reference.jpg"
STORY_SOFT_ANIMATION_VISUAL_STYLE = (
    "original soft hand-drawn contemporary 2D family-animation frame with fine clean linework, restrained cel shading and painterly pastel backgrounds, "
    "appealing rounded but believable character design, warm sincere expressions, graceful gestures and polished cinematic staging, "
    "sunlit cream, sage green, apricot, pale blue and honey-gold palette, luminous window light and delicate atmospheric depth, "
    "full-bleed artwork extending to all four image edges, no border, no frame, no photorealism, no 3D, no imitation of any recognizable studio, film or artist, "
    "no readable text, page numbers, signatures, logos or watermarks; use only the general visual language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_SOFT_ANIMATION_REFERENCE_PATH = "assets/story/soft_animation_reference.jpg"
STORY_CLASSIC_CEL_CARTOON_VISUAL_STYLE = (
    "classic hand-inked late-20th-century cel-animation frame, strong confident dark contour lines, flat opaque colors and minimal graphic shading, "
    "bold friendly caricature, lively elastic poses, expressive eyes and clear readable silhouettes, detailed painted domestic background, "
    "navy blue, warm cream, tomato red, muted teal and vivid ginger-orange palette with slight analog film grain, "
    "full-bleed artwork extending to all four image edges, no white border, no paper margin, no mat, no frame, no photorealism, no 3D, "
    "no readable text, page numbers, signatures, logos or watermarks; use only this classic cel language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_CLASSIC_CEL_CARTOON_REFERENCE_PATH = "assets/story/classic_cel_cartoon_reference.jpg"
STORY_FELT_APPLIQUE_VISUAL_STYLE = (
    "flat handcrafted felt-applique textile illustration in which every character, object and background is cut from colored wool felt, "
    "clearly visible blanket stitches, embroidery thread, layered fabric edges and softly fuzzy fibers, charming simplified shapes and graphic folk ornament, "
    "mustard yellow, deep teal, burgundy, linen cream and ginger-orange palette, evenly lit like a carefully photographed handmade fabric panel, "
    "full-bleed artwork extending to all four image edges, no border, no frame, no volumetric puppet set, no clay, no plastic, no glossy CGI, "
    "no readable text, page numbers, signatures, logos or watermarks; use only the textile technique, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_FELT_APPLIQUE_REFERENCE_PATH = "assets/story/felt_applique_reference.jpg"
STORY_FOLK_FAIRYTALE_VISUAL_STYLE = (
    "hand-painted Slavic folk-fairytale 2D animation and book illustration in opaque tempera and gouache, "
    "clean graceful contour drawing, warm expressive faces, richly painted wooden interiors, birch-and-pine landscapes and traditional floral textile ornament, "
    "deep forest green, dark teal, linen white, cinnabar red and warm ochre palette, layered theatrical background with subtle vintage cel texture, "
    "full-bleed artwork extending to all four image edges, no white border, no paper margin, no mat, no frame, no photorealism, no 3D, "
    "no readable text, page numbers, signatures, logos or watermarks; use only the folk visual language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_FOLK_FAIRYTALE_REFERENCE_PATH = "assets/story/folk_fairytale_reference.jpg"
STORY_OIL_PASTEL_VISUAL_STYLE = (
    "joyful hand-drawn oil-pastel and wax-crayon picture-book illustration on bright textured white paper, "
    "energetic broken strokes, visible overlapping color marks, loose navy sketch contours and deliberately imperfect hand coloring, "
    "sunny tangerine, violet, cornflower blue, leaf green and warm yellow palette, exuberant cozy mood and expressive rounded faces, "
    "full-bleed drawing extending to all four image edges, no border, no mat, no frame, no smooth digital airbrush, no photorealism, no 3D, "
    "no readable text, page numbers, signatures, logos or watermarks; use only the pastel drawing technique, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_OIL_PASTEL_REFERENCE_PATH = "assets/story/oil_pastel_reference.jpg"
STORY_CROCHET_VISUAL_STYLE = (
    "intricate handmade crochet-and-yarn textile illustration where every character, object, dish and background is constructed from crocheted thread, braided cord and woven burlap, "
    "clearly visible loops, stitches, knots and layered yarn fibers, softly sculpted relief rather than a flat painted surface, warm expressive textile faces and folk embroidery details, "
    "linen beige, brick red, indigo blue, cream, brown and ginger-orange palette, evenly lit like a museum-quality photographed fiber-art panel, "
    "full-bleed textile artwork extending to all four image edges, no border, no frame, no felt sheets, no clay, no plastic, no glossy CGI, "
    "no readable text, page numbers, signatures, logos or watermarks; use only the crochet material language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_CROCHET_REFERENCE_PATH = "assets/story/crochet_reference.jpg"
STORY_MINIMAL_RETRO_CARTOON_VISUAL_STYLE = (
    "minimalist mid-century hand-drawn 2D cartoon cel, very thin lively black ink contours, extremely elongated angular human proportions and a plump graphic cat, "
    "large areas of warm cream negative space, sparse geometric kitchen props, flat unshaded color fills and witty expressive posing, "
    "restricted burnt orange, teal, charcoal, warm white and pale peach palette with subtle analog print texture, clean poster-like composition, "
    "full-bleed artwork extending to all four image edges, no white border, no paper margin, no mat, no frame, no photorealism, no 3D, "
    "no readable text, page numbers, signatures, logos or watermarks; use only this minimal graphic language, never copy the reference people, cat, kitchen, food, props or composition"
)
STORY_MINIMAL_RETRO_CARTOON_REFERENCE_PATH = "assets/story/minimal_retro_cartoon_reference.jpg"
STORY_VISUAL_STYLES: dict[str, dict[str, str]] = {
    STORY_STYLE_PRIMARY: {
        "label": "Основной",
        "prompt": "",
        "reference_path": "",
    },
    STORY_STYLE_WATERCOLOR: {
        "label": "Сухая акварель",
        "prompt": STORY_RETRO_VISUAL_STYLE,
        "reference_path": STORY_RETRO_STYLE_REFERENCE_PATH,
    },
    STORY_STYLE_RETRO_CARTOON: {
        "label": "Ретро-мультфильм",
        "prompt": STORY_RETRO_CARTOON_VISUAL_STYLE,
        "reference_path": STORY_RETRO_CARTOON_REFERENCE_PATH,
    },
    STORY_STYLE_STORYBOOK_GOUACHE: {
        "label": "Книжная гуашь",
        "prompt": STORY_STORYBOOK_GOUACHE_VISUAL_STYLE,
        "reference_path": STORY_STORYBOOK_GOUACHE_REFERENCE_PATH,
    },
    STORY_STYLE_CLAYMATION: {
        "label": "Пластилиновый мультфильм",
        "prompt": STORY_CLAYMATION_VISUAL_STYLE,
        "reference_path": STORY_CLAYMATION_REFERENCE_PATH,
    },
    STORY_STYLE_FELT_STOP_MOTION: {
        "label": "Кукольный стоп-моушен",
        "prompt": STORY_FELT_STOP_MOTION_VISUAL_STYLE,
        "reference_path": STORY_FELT_STOP_MOTION_REFERENCE_PATH,
    },
    STORY_STYLE_RETRO_FAIRYTALE: {
        "label": "Рисованная ретро-сказка",
        "prompt": STORY_RETRO_FAIRYTALE_VISUAL_STYLE,
        "reference_path": STORY_RETRO_FAIRYTALE_REFERENCE_PATH,
    },
    STORY_STYLE_TWILIGHT_WATERCOLOR: {
        "label": "Сумеречная акварель",
        "prompt": STORY_TWILIGHT_WATERCOLOR_VISUAL_STYLE,
        "reference_path": STORY_TWILIGHT_WATERCOLOR_REFERENCE_PATH,
    },
    STORY_STYLE_SOFT_ANIMATION: {
        "label": "Светлая анимационная пастель",
        "prompt": STORY_SOFT_ANIMATION_VISUAL_STYLE,
        "reference_path": STORY_SOFT_ANIMATION_REFERENCE_PATH,
    },
    STORY_STYLE_CLASSIC_CEL_CARTOON: {
        "label": "Классический рисованный мультфильм",
        "prompt": STORY_CLASSIC_CEL_CARTOON_VISUAL_STYLE,
        "reference_path": STORY_CLASSIC_CEL_CARTOON_REFERENCE_PATH,
    },
    STORY_STYLE_FELT_APPLIQUE: {
        "label": "Фетровая аппликация",
        "prompt": STORY_FELT_APPLIQUE_VISUAL_STYLE,
        "reference_path": STORY_FELT_APPLIQUE_REFERENCE_PATH,
    },
    STORY_STYLE_FOLK_FAIRYTALE: {
        "label": "Народная рисованная сказка",
        "prompt": STORY_FOLK_FAIRYTALE_VISUAL_STYLE,
        "reference_path": STORY_FOLK_FAIRYTALE_REFERENCE_PATH,
    },
    STORY_STYLE_OIL_PASTEL: {
        "label": "Пастельные мелки",
        "prompt": STORY_OIL_PASTEL_VISUAL_STYLE,
        "reference_path": STORY_OIL_PASTEL_REFERENCE_PATH,
    },
    STORY_STYLE_CROCHET: {
        "label": "Вязаная иллюстрация",
        "prompt": STORY_CROCHET_VISUAL_STYLE,
        "reference_path": STORY_CROCHET_REFERENCE_PATH,
    },
    STORY_STYLE_MINIMAL_RETRO_CARTOON: {
        "label": "Минималистичный ретро-мультфильм",
        "prompt": STORY_MINIMAL_RETRO_CARTOON_VISUAL_STYLE,
        "reference_path": STORY_MINIMAL_RETRO_CARTOON_REFERENCE_PATH,
    },
}


from .history_visual_styles import HISTORY_STYLES
STORY_VISUAL_STYLES.update(HISTORY_STYLES)


def normalize_story_visual_style(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "primary": STORY_STYLE_PRIMARY,
        "main": STORY_STYLE_PRIMARY,
        "основной": STORY_STYLE_PRIMARY,
        "основная": STORY_STYLE_PRIMARY,
        "watercolor": STORY_STYLE_WATERCOLOR,
        "dry_watercolor": STORY_STYLE_WATERCOLOR,
        "сухая_акварель": STORY_STYLE_WATERCOLOR,
        "акварель": STORY_STYLE_WATERCOLOR,
        "retro_cartoon": STORY_STYLE_RETRO_CARTOON,
        "ретро_мультфильм": STORY_STYLE_RETRO_CARTOON,
        "мультфильм": STORY_STYLE_RETRO_CARTOON,
        "ретро": STORY_STYLE_RETRO_CARTOON,
        "storybook_gouache": STORY_STYLE_STORYBOOK_GOUACHE,
        "книжная_гуашь": STORY_STYLE_STORYBOOK_GOUACHE,
        "книжная_иллюстрация": STORY_STYLE_STORYBOOK_GOUACHE,
        "claymation": STORY_STYLE_CLAYMATION,
        "пластилиновый_мультфильм": STORY_STYLE_CLAYMATION,
        "пластилин": STORY_STYLE_CLAYMATION,
        "felt_stop_motion": STORY_STYLE_FELT_STOP_MOTION,
        "кукольный_стоп_моушен": STORY_STYLE_FELT_STOP_MOTION,
        "кукольный": STORY_STYLE_FELT_STOP_MOTION,
        "retro_fairytale": STORY_STYLE_RETRO_FAIRYTALE,
        "рисованная_ретро_сказка": STORY_STYLE_RETRO_FAIRYTALE,
        "ретро_сказка": STORY_STYLE_RETRO_FAIRYTALE,
        "twilight_watercolor": STORY_STYLE_TWILIGHT_WATERCOLOR,
        "сумеречная_акварель": STORY_STYLE_TWILIGHT_WATERCOLOR,
        "вечерняя_акварель": STORY_STYLE_TWILIGHT_WATERCOLOR,
        "soft_animation": STORY_STYLE_SOFT_ANIMATION,
        "светлая_анимационная_пастель": STORY_STYLE_SOFT_ANIMATION,
        "анимационная_пастель": STORY_STYLE_SOFT_ANIMATION,
        "classic_cel_cartoon": STORY_STYLE_CLASSIC_CEL_CARTOON,
        "классический_рисованный_мультфильм": STORY_STYLE_CLASSIC_CEL_CARTOON,
        "рисованный_мультфильм": STORY_STYLE_CLASSIC_CEL_CARTOON,
        "felt_applique": STORY_STYLE_FELT_APPLIQUE,
        "фетровая_аппликация": STORY_STYLE_FELT_APPLIQUE,
        "аппликация": STORY_STYLE_FELT_APPLIQUE,
        "folk_fairytale": STORY_STYLE_FOLK_FAIRYTALE,
        "народная_рисованная_сказка": STORY_STYLE_FOLK_FAIRYTALE,
        "народная_сказка": STORY_STYLE_FOLK_FAIRYTALE,
        "oil_pastel": STORY_STYLE_OIL_PASTEL,
        "пастельные_мелки": STORY_STYLE_OIL_PASTEL,
        "мелки": STORY_STYLE_OIL_PASTEL,
        "crochet": STORY_STYLE_CROCHET,
        "вязаная_иллюстрация": STORY_STYLE_CROCHET,
        "вязание": STORY_STYLE_CROCHET,
        "minimal_retro_cartoon": STORY_STYLE_MINIMAL_RETRO_CARTOON,
        "минималистичный_ретро_мультфильм": STORY_STYLE_MINIMAL_RETRO_CARTOON,
        "минималистичный_мультфильм": STORY_STYLE_MINIMAL_RETRO_CARTOON,
    }
    return raw if raw in STORY_VISUAL_STYLES else aliases.get(raw, STORY_DEFAULT_VISUAL_STYLE)


def story_visual_style_config(value: Any) -> dict[str, str]:
    return STORY_VISUAL_STYLES[normalize_story_visual_style(value)]


def story_style_uses_character_references_only(value: Any) -> bool:
    return normalize_story_visual_style(value) == STORY_STYLE_PRIMARY


@dataclass(frozen=True, slots=True)
class StoryAudioResult:
    audio_bytes: bytes
    content_type: str
    duration_sec: float
    alignment: dict[str, list[Any]]
    request_id: str | None = None
    history_item_id: str | None = None
    model_id: str = "eleven_v4"


def _clean_title(value: Any) -> str:
    return " ".join(str(value or "").strip().strip('"«»').split())[:90]


def _clean_voiceover(value: Any) -> str:
    from .directorial_notes import spoken_text
    text = " ".join(spoken_text(value).replace("[пауза]", " ").split())
    return text.strip()


def generate_story_voiceover(
    source_story: str,
    *,
    user_feedback: str | None = None,
    previous_text: str | None = None,
    discipline_mode: str | None = None,
) -> dict[str, Any]:
    """Turn a supplied story into an approvable narration of up to two minutes."""
    payload = {
        "source_story": str(source_story or "").strip(),
        "previous_voiceover": str(previous_text or "").strip(),
        "user_feedback": str(user_feedback or "").strip(),
    }
    length_direction = (
        "Целевая длительность 18–28 секунд спокойной речи, ориентир 38–60 русских слов. "
        if discipline_mode in SHORT_FORMATS and discipline_mode != ARCHIVE else
        "Озвучка должна занимать от 45 до 110 секунд естественной спокойной речи и точно укладываться в две минуты. "
    )
    prompt = (
        "Ты пишешь русскую закадровую озвучку для короткой визуальной истории. "
        "Верни только строгий JSON-объект с ключами title и voiceover_text.\n"
        f"{length_direction}"
        "Выбирай длину по объёму исходной истории: не растягивай короткую тему, но и не выбрасывай важные мысли из большой. "
        f"Не более {STORY_VOICEOVER_MAX_WORDS} слов и {STORY_VOICEOVER_MAX_CHARS} знаков с пробелами. "
        "Оба ограничения жёсткие. Один цельный голос, без реплик персонажей, ремарок, "
        "таймкодов, списков, Markdown и служебных пометок.\n"
        "Структура: сильный хук в первой фразе; ясная причинно-следственная история; поворот или цена решения; "
        "финальная фраза-вывод, которая переосмысливает начало. Не выдумывай факты, которых нет в источнике.\n"
        "Текст должен быть удобен для ElevenLabs: короткие предложения, числа записаны так, как их нужно произнести, "
        "без сложных аббревиатур. Если дан feedback, перепиши previous_voiceover по нему, сохранив факты источника.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    direction = script_direction(discipline_mode)
    if direction:
        prompt += "\nПриоритетная структура выбранного формата:\n" + direction
    from .directorial_notes import RULE
    prompt = RULE + prompt
    raw = gemini_generate_sync(prompt, model_profile=GEMINI_MODEL_PROFILE_CREATIVE)
    data = extract_json_object(raw)
    title = _clean_title(data.get("title"))
    voiceover = _clean_voiceover(data.get("voiceover_text"))
    if not voiceover:
        raise ValueError("Gemini не вернул текст озвучки для истории.")
    if discipline_mode == REFRAME:
        title = opening_sentence(voiceover)
    word_count = len(re.findall(r"[\wЁёА-Яа-я-]+", voiceover, flags=re.UNICODE))
    return {
        "title": title or "История",
        "voiceover_text": voiceover,
        "raw_response": str(raw or ""),
        "word_count": word_count,
        "character_count": len(voiceover),
        "within_target_limit": (
            len(voiceover) <= STORY_VOICEOVER_MAX_CHARS
            and word_count <= STORY_VOICEOVER_MAX_WORDS
        ),
    }


def generate_story_english_voiceover(
    russian_title: str,
    russian_voiceover: str,
    *,
    target_duration_sec: float,
    user_feedback: str | None = None,
    previous_text: str | None = None,
) -> dict[str, Any]:
    """Create a faithful, unabridged English translation of the approved RU narration."""
    target_duration = max(10.0, min(STORY_MAX_DURATION_SEC, float(target_duration_sec or STORY_TARGET_DURATION_SEC)))
    source_text = _clean_voiceover(russian_voiceover)
    source_sentence_count = len(re.findall(r"[^.!?…]+[.!?…]+|[^.!?…]+$", source_text))
    payload = {
        "russian_title": str(russian_title or "").strip(),
        "approved_russian_voiceover": source_text,
        "previous_english_voiceover": _clean_voiceover(previous_text),
        "user_feedback": str(user_feedback or "").strip(),
        "russian_sentence_count": source_sentence_count,
        "russian_audio_duration_seconds_for_reference_only": round(target_duration, 1),
    }
    prompt = (
        "Translate the approved Russian narration into English as faithfully and literally as English grammar allows. "
        "This is a complete translation, never an adaptation, summary, rewrite or localization. "
        "Return only a strict JSON object with keys title and voiceover_text. "
        "Translate every sentence and every meaningful clause. Preserve all facts, examples, questions, repetitions, contrasts, "
        "calls to action, the hook, semantic order, emotional turn and final conclusion. Do not summarize, condense, merge sentences, "
        "omit details, soften claims, add facts or replace a concrete example with a general statement. Keep one English sentence for "
        "each Russian sentence wherever English grammar permits, and keep the same sentence order. "
        "Use complete, natural English grammar, but stay semantically close to the Russian wording; do not creatively rewrite it. "
        "Never use telegraphic compression and never omit articles or verbs. The Russian audio duration is reference metadata only: "
        "do not shorten the translation to match it. The English recording may be longer and will receive its own timing. "
        "If a full translation conflicts with a duration or word target, always return the full translation. "
        "If feedback is present, apply only the explicitly requested wording change and keep every other source detail. "
        "Use one continuous narrator, short ElevenLabs-friendly sentences, no dialogue labels, stage directions, "
        "timestamps, lists, Markdown or service notes.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    from .directorial_notes import RULE
    prompt = RULE + prompt
    raw = gemini_generate_sync(prompt, model_profile=GEMINI_MODEL_PROFILE_CREATIVE)
    data = extract_json_object(raw)
    title = _clean_title(data.get("title"))
    voiceover = _clean_voiceover(data.get("voiceover_text"))
    if not voiceover:
        raise ValueError("Gemini не вернул английский текст озвучки для истории.")
    word_count = len(re.findall(r"[A-Za-z0-9'-]+", voiceover))
    return {
        "title": title or "Story",
        "voiceover_text": voiceover,
        "raw_response": str(raw or ""),
        "word_count": word_count,
        "character_count": len(voiceover),
        "within_target_limit": word_count <= STORY_VOICEOVER_MAX_WORDS,
        "target_word_count": STORY_VOICEOVER_MAX_WORDS,
        "source_sentence_count": source_sentence_count,
    }


def archive_story_voiceover_version(
    session: dict[str, Any],
    story: dict[str, Any],
    *,
    stage: str,
    feedback: str | None = None,
) -> dict[str, Any]:
    """Persist every usable Gemini script response before it is reviewed or replaced."""
    versions = session.get(STORY_VOICEOVER_VERSIONS_SESSION_KEY)
    if not isinstance(versions, list):
        versions = []
    voiceover_text = str(story.get("voiceover_text") or "")
    word_count = int(
        story.get("word_count")
        or len(re.findall(r"[\wЁёА-Яа-я-]+", voiceover_text, flags=re.UNICODE))
    )
    character_count = int(story.get("character_count") or len(voiceover_text))
    within_target_limit = story.get("within_target_limit")
    if within_target_limit is None:
        within_target_limit = (
            character_count <= STORY_VOICEOVER_MAX_CHARS
            and word_count <= STORY_VOICEOVER_MAX_WORDS
        )
    item = {
        "version_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stage": str(stage or "generation"),
        "title": str(story.get("title") or "История"),
        "voiceover_text": voiceover_text,
        "raw_response": str(story.get("raw_response") or ""),
        "word_count": word_count,
        "character_count": character_count,
        "within_target_limit": bool(within_target_limit),
        "feedback": str(feedback or ""),
    }
    versions.append(item)
    session[STORY_VOICEOVER_VERSIONS_SESSION_KEY] = versions
    return item


def archive_story_english_voiceover_version(
    session: dict[str, Any],
    story: dict[str, Any],
    *,
    stage: str,
    feedback: str | None = None,
) -> dict[str, Any]:
    """Persist every usable English adaptation before review or replacement."""
    versions = session.get(STORY_VOICEOVER_VERSIONS_EN_SESSION_KEY)
    if not isinstance(versions, list):
        versions = []
    voiceover_text = str(story.get("voiceover_text") or "")
    item = {
        "version_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stage": str(stage or "translation"),
        "title": str(story.get("title") or "Story"),
        "voiceover_text": voiceover_text,
        "raw_response": str(story.get("raw_response") or ""),
        "word_count": int(story.get("word_count") or len(re.findall(r"[A-Za-z0-9'-]+", voiceover_text))),
        "character_count": int(story.get("character_count") or len(voiceover_text)),
        "within_target_limit": bool(story.get("within_target_limit", True)),
        "feedback": str(feedback or ""),
    }
    versions.append(item)
    session[STORY_VOICEOVER_VERSIONS_EN_SESSION_KEY] = versions
    return item


def _timed_endpoint_result(
    text: str,
    *,
    language: str = "ru",
    voice_id: str | None = None,
) -> StoryAudioResult:
    """Call ElevenLabs v4 Text-to-Dialogue with timestamps for one narrator."""
    import os

    from .constants import ELEVENLABS_VOICE_ID_ENV_NAME

    effective_voice_id = str(voice_id or os.getenv(ELEVENLABS_VOICE_ID_ENV_NAME) or "").strip()
    if not effective_voice_id:
        raise RuntimeError(f"Не задан {ELEVENLABS_VOICE_ID_ENV_NAME}.")
    return _timed_dialogue_endpoint_result(
        [{"text": text, "voice_id": effective_voice_id}],
        language=language,
    )


def _timed_dialogue_endpoint_result(
    turns: list[dict[str, str]],
    *,
    language: str = "ru",
) -> StoryAudioResult:
    """Call ElevenLabs v4 Text-to-Dialogue with timestamps for multiple speakers."""
    import os
    import requests

    from .constants import ELEVENLABS_API_KEY_ENV_NAME
    from .elevenlabs_tts import DEFAULT_ELEVENLABS_OUTPUT_FORMAT, DEFAULT_ELEVENLABS_TTS_MODEL, check_elevenlabs_access

    api_key = str(os.getenv(ELEVENLABS_API_KEY_ENV_NAME) or "").strip()
    if not api_key:
        raise RuntimeError(f"Не задан {ELEVENLABS_API_KEY_ENV_NAME}.")
    inputs = [
        {
            "text": _clean_voiceover(turn.get("text")),
            "voice_id": str(turn.get("voice_id") or "").strip(),
        }
        for turn in turns
        if _clean_voiceover(turn.get("text")) and str(turn.get("voice_id") or "").strip()
    ]
    if not inputs:
        raise ValueError("Диалог для озвучки пустой.")
    response = requests.post(
        "https://api.elevenlabs.io/v1/text-to-dialogue/with-timestamps",
        params={"output_format": DEFAULT_ELEVENLABS_OUTPUT_FORMAT},
        headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        json={
            "inputs": inputs,
            "model_id": DEFAULT_ELEVENLABS_TTS_MODEL,
            "language_code": str(language or "ru").strip().lower() or "ru",
            "apply_text_normalization": "auto",
            "settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        },
        timeout=180,
        allow_redirects=False,
    )
    check_elevenlabs_access(response)
    if not response.ok:
        raise RuntimeError(f"ElevenLabs timed TTS failed: HTTP {response.status_code}: {response.text[:1000]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            'ElevenLabs вернул ответ без ожидаемых данных озвучки '
            f'(HTTP {getattr(response, "status_code", "unknown")}). '
            'Нужно проверить историю генераций перед повторной попыткой.'
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError('ElevenLabs вернул некорректные данные озвучки. Проверьте историю перед повтором.')
    audio_b64 = str(data.get("audio_base64") or "").strip()
    from .story_alignment import response_alignment
    alignment = response_alignment(data)
    if not audio_b64:
        raise RuntimeError("ElevenLabs timed TTS did not return audio_base64.")
    if not alignment:
        # Preserve the paid result and provider IDs for history recovery.
        result = StoryAudioResult(
            audio_bytes=base64.b64decode(audio_b64), content_type="audio/mpeg",
            duration_sec=0.0, alignment={},
            request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
            history_item_id=response.headers.get("history-item-id"),
            model_id=DEFAULT_ELEVENLABS_TTS_MODEL,
        )
        path = store_story_audio(0, result)
        raise RuntimeError(f"ElevenLabs returned audio without valid timestamps. Saved for recovery: {path}")
    ends = alignment.get("character_end_times_seconds")
    duration = max((float(value) for value in ends or []), default=0.0)
    return StoryAudioResult(
        audio_bytes=base64.b64decode(audio_b64),
        content_type="audio/mpeg",
        duration_sec=duration,
        alignment={
            "characters": list(alignment.get("characters") or []),
            "character_start_times_seconds": list(alignment.get("character_start_times_seconds") or []),
            "character_end_times_seconds": list(alignment.get("character_end_times_seconds") or []),
        },
        request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
        history_item_id=response.headers.get("history-item-id"),
        model_id=DEFAULT_ELEVENLABS_TTS_MODEL,
    )


def generate_story_dialogue_audio(
    turns: list[dict[str, str]],
    *,
    language: str = "ru",
) -> StoryAudioResult:
    """Generate one seamless, timestamped multi-speaker dialogue track."""
    return _timed_dialogue_endpoint_result(turns, language=language)


def _duration_from_audio_bytes(audio_bytes: bytes) -> float:
    try:
        import imageio_ffmpeg  # type: ignore

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 0.0
    duration_re = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
    with tempfile.TemporaryDirectory(prefix="story_audio_probe_") as tmp:
        path = Path(tmp) / "narration.mp3"
        path.write_bytes(audio_bytes)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path), "-f", "null", "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    match = duration_re.search(proc.stderr or proc.stdout or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def generate_story_audio(
    text: str,
    *,
    language: str = "ru",
    voice_id: str | None = None,
) -> StoryAudioResult:
    """Generate narration with exact timestamps; never silently synthesize twice."""
    return _timed_endpoint_result(text, language=language, voice_id=voice_id)


def store_story_audio(chat_id: int, result: StoryAudioResult) -> str:
    target_dir = Path.cwd() / "artifacts" / "story_audio" / str(int(chat_id))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4()}.mp3"
    target.write_bytes(result.audio_bytes)
    from .story_alignment import save_story_audio_metadata
    save_story_audio_metadata(target, {
        "alignment": result.alignment,
        "duration_sec": result.duration_sec,
        "request_id": result.request_id,
        "history_item_id": result.history_item_id,
        "model_id": result.model_id,
    })
    return str(target.resolve())


def _alignment_rows(alignment: dict[str, list[Any]] | None) -> list[tuple[str, float, float]]:
    if not isinstance(alignment, dict):
        return []
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    count = min(len(chars), len(starts), len(ends))
    rows: list[tuple[str, float, float]] = []
    for index in range(count):
        try:
            rows.append((str(chars[index]), float(starts[index]), float(ends[index])))
        except (TypeError, ValueError):
            continue
    return rows


def _word_timing_rows(
    text: str,
    duration_sec: float,
    alignment: dict[str, list[Any]] | None,
) -> list[tuple[str, float, float]]:
    """Return whole-word timing rows so a 3-second boundary never cuts a word."""
    char_rows = _alignment_rows(alignment)
    if char_rows:
        words: list[tuple[str, float, float]] = []
        token: list[str] = []
        token_start = 0.0
        token_end = 0.0
        for char, char_start, char_end in char_rows:
            if char.isspace():
                if token:
                    words.append(("".join(token), token_start, token_end))
                    token = []
                continue
            if not token:
                token_start = char_start
            token.append(char)
            token_end = char_end
        if token:
            words.append(("".join(token), token_start, token_end))
        if words:
            return words

    clean = _clean_voiceover(text)
    tokens = clean.split()
    if not tokens:
        return []
    total_weight = float(sum(max(1, len(token)) + 1 for token in tokens))
    duration = max(0.001, float(duration_sec or 0.0))
    cursor = 0.0
    words = []
    for token in tokens:
        weight = float(max(1, len(token)) + 1)
        start = duration * cursor / total_weight
        cursor += weight
        end = duration * cursor / total_weight
        words.append((token, start, end))
    return words


def build_narration_windows(
    text: str,
    duration_sec: float,
    alignment: dict[str, list[Any]] | None,
    *,
    clip_duration_sec: float = STORY_CLIP_DURATION_SEC,
) -> list[dict[str, Any]]:
    """Split approved narration into exact visual windows using ElevenLabs alignment."""
    # If the user explicitly approves a paid take over one minute, preserve its
    # full duration instead of silently dropping the tail of the story.
    duration = max(clip_duration_sec, float(duration_sec or 0.0))
    count = max(1, int(math.ceil(duration / clip_duration_sec)))
    rows = _word_timing_rows(text, duration, alignment)
    windows: list[dict[str, Any]] = []
    for index in range(count):
        start = index * clip_duration_sec
        end = min(duration, start + clip_duration_sec)
        excerpt = " ".join(
            word
            for word, word_start, word_end in rows
            if start <= ((word_start + word_end) / 2.0) < end
            or (index == count - 1 and ((word_start + word_end) / 2.0) == end)
        )
        excerpt = " ".join(excerpt.split()).strip(" ,.—–-")
        windows.append(
            {
                "scene": index + 1,
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "generation_duration_sec": STORY_CLIP_DURATION_SEC,
                "narration_excerpt": excerpt,
            }
        )
    return windows


def build_narration_windows_for_intervals(
    text: str,
    duration_sec: float,
    alignment: dict[str, list[Any]] | None,
    intervals: list[tuple[float, float]],
) -> list[dict[str, Any]]:
    """Map one approved narration onto caller-provided visual intervals.

    Story translations can have a different total duration from the Russian
    master.  Using fixed three-second windows makes the faster language drift
    ahead of the shared visuals.  These intervals let every visual clip be
    shortened proportionally while its adjacent narration excerpt is selected
    from the same exact, language-specific time span.
    """
    duration = max(0.001, float(duration_sec or 0.0))
    rows = _word_timing_rows(text, duration, alignment)
    windows: list[dict[str, Any]] = []
    for index, raw_interval in enumerate(intervals):
        raw_start, raw_end = raw_interval
        start = min(duration, max(0.0, float(raw_start or 0.0)))
        end = min(duration, max(start, float(raw_end or start)))
        excerpt = " ".join(
            word
            for word, word_start, word_end in rows
            if start <= ((word_start + word_end) / 2.0) < end
            or (index == len(intervals) - 1 and ((word_start + word_end) / 2.0) == end)
        )
        windows.append(
            {
                "scene": index + 1,
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "narration_excerpt": " ".join(excerpt.split()).strip(" ,.—–-"),
            }
        )
    return windows


def build_semantic_narration_beats(
    text: str,
    duration_sec: float,
    alignment: dict[str, list[Any]] | None,
) -> list[dict[str, Any]]:
    """Split narration at sentence boundaries, never at fixed three-second marks.

    The resulting beats are the units shown to the user and directed by Gemini.
    Each beat is expanded into one or more three-second Kling clips later.
    """
    duration = max(STORY_CLIP_DURATION_SEC, float(duration_sec or 0.0))
    rows = _word_timing_rows(text, duration, alignment)
    if not rows:
        return [
            {
                "beat": 1,
                "start_sec": 0.0,
                "end_sec": round(duration, 3),
                "narration_excerpt": _clean_voiceover(text),
            }
        ]

    groups: list[list[tuple[str, float, float]]] = []
    current: list[tuple[str, float, float]] = []
    for row in rows:
        current.append(row)
        token = row[0].rstrip('"»”’)]}')
        if re.search(r"[.!?…]$", token):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    # Avoid a tiny orphan sentence becoming its own visual episode.
    merged: list[list[tuple[str, float, float]]] = []
    group_index = 0
    while group_index < len(groups):
        group = list(groups[group_index])
        group_duration = float(group[-1][2]) - float(group[0][1])
        if group_duration < 2.2 and group_index + 1 < len(groups):
            group.extend(groups[group_index + 1])
            group_index += 2
            merged.append(group)
            continue
        if merged and group_duration < 2.2:
            merged[-1].extend(group)
        else:
            merged.append(group)
        group_index += 1

    beats: list[dict[str, Any]] = []
    for index, group in enumerate(merged):
        start = 0.0 if index == 0 else float(group[0][1])
        end = duration if index == len(merged) - 1 else float(merged[index + 1][0][1])
        excerpt = " ".join(row[0] for row in group).strip()
        beats.append(
            {
                "beat": index + 1,
                "start_sec": round(start, 3),
                "end_sec": round(max(start, end), 3),
                "narration_excerpt": excerpt,
            }
        )
    return beats


def _story_character_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9а-яё]+", "_", raw, flags=re.IGNORECASE).strip("_")
    return raw[:64]


def _contains_russian_first_person_pronoun(value: Any) -> bool:
    """Return whether the narration explicitly uses the standalone pronoun ``я``."""
    return bool(re.search(r"(?<![0-9a-zа-яё])я(?![0-9a-zа-яё])", str(value or ""), flags=re.IGNORECASE))


def _discipline_anastasia_character_id(characters: list[dict[str, Any]]) -> str:
    """Resolve Anastasia from the DB-backed character bible without hard-coding its key."""
    for character in characters:
        character_id = _story_character_id(character.get("id"))
        display_name = " ".join(str(character.get("display_name") or "").split()).casefold()
        if display_name == "анастасия" or "anastasia" in character_id or "анастасия" in character_id:
            return character_id
    return ""


def _discipline_knopka_character_id(characters: list[dict[str, Any]]) -> str:
    """Resolve Knopka from the DB-backed character bible."""
    for character in characters:
        character_id = _story_character_id(character.get("id"))
        display_name = " ".join(str(character.get("display_name") or "").split()).casefold()
        if display_name == "кнопка" or "knopka" in character_id or "кнопка" in character_id:
            return character_id
    return ""


def _discipline_supporting_character_id(
    characters: list[dict[str, Any]],
    *,
    anastasia_id: str,
) -> str:
    """Pick the non-Anastasia human used to demonstrate Discipline techniques."""
    for character in characters:
        character_id = _story_character_id(character.get("id"))
        display_name = " ".join(str(character.get("display_name") or "").split()).casefold()
        if character_id != anastasia_id and (display_name == "василий" or "vasily" in character_id):
            return character_id
    for character in characters:
        character_id = _story_character_id(character.get("id"))
        if character_id and character_id != anastasia_id and "knopka" not in character_id:
            return character_id
    return ""


def _apply_discipline_cast_policy(
    scenes: list[dict[str, Any]],
    *,
    project_characters: list[dict[str, Any]],
    primary_id: str,
    anastasia_id: str,
    character_descriptions: dict[str, str],
    character_labels: dict[str, str],
) -> None:
    """Keep every Discipline frame inhabited and make Vasily/Knopka a recurring pair."""
    if not scenes:
        return
    vasily_id = _discipline_supporting_character_id(project_characters, anastasia_id=anastasia_id)
    knopka_id = _discipline_knopka_character_id(project_characters)
    fallback_id = next(
        (
            character_id
            for character_id in (vasily_id, primary_id, knopka_id, anastasia_id)
            if character_id
        ),
        "",
    )
    if not fallback_id and project_characters:
        fallback_id = _story_character_id(project_characters[0].get("id"))

    for scene in scenes:
        character_ids = list(dict.fromkeys(str(value) for value in scene.get("characters") or [] if str(value)))
        if not character_ids and fallback_id:
            character_ids = [fallback_id]
        scene["characters"] = character_ids

    if vasily_id and knopka_id and vasily_id != knopka_id:
        vasily_scenes = [scene for scene in scenes if vasily_id in (scene.get("characters") or [])]
        required_pairs = int(math.ceil(len(vasily_scenes) * 0.60))
        paired_count = sum(knopka_id in (scene.get("characters") or []) for scene in vasily_scenes)
        if required_pairs > paired_count:
            target_positions = (
                [0]
                if required_pairs == 1
                else [round(index * (len(vasily_scenes) - 1) / (required_pairs - 1)) for index in range(required_pairs)]
            )
            candidates = [vasily_scenes[index] for index in target_positions]
            candidates.extend(scene for scene in vasily_scenes if scene not in candidates)
            for scene in candidates:
                if paired_count >= required_pairs:
                    break
                character_ids = scene.get("characters") or []
                if knopka_id in character_ids:
                    continue
                character_ids.append(knopka_id)
                paired_count += 1

    for scene in scenes:
        character_ids = [str(value) for value in scene.get("characters") or []]
        scene["story_character_descriptions"] = {
            character_id: character_descriptions[character_id]
            for character_id in character_ids
            if character_id in character_descriptions
        }
        scene["story_character_labels"] = {
            character_id: character_labels[character_id]
            for character_id in character_ids
            if character_id in character_labels
        }


def enforce_discipline_narrative_cast(scenes, project_characters):
    """Bind narrative heroes to approved project IDs, never invented model aliases."""
    historical = [scene for scene in scenes if scene.get(MODE_KEY) == ARCHIVE]
    if historical:
        known = {str(c["id"]): c for c in project_characters}
        for scene in historical:
            ids = [str(v) for v in scene.get("characters") or []]
            if any(i not in known for i in ids):
                raise ValueError("Историческая сцена содержит неизвестного исполнителя. Используйте утверждённые character_id проекта.")
            scene["characters"] = ids
            scene["story_character_descriptions"] = {i: known[i].get("description", "") for i in ids}
            scene["story_character_labels"] = {i: known[i].get("display_name", "") for i in ids}
        scenes = [scene for scene in scenes if scene.get(MODE_KEY) != ARCHIVE]
        if not scenes:
            return
    anastasia = _discipline_anastasia_character_id(project_characters)
    vasily = next((_story_character_id(c.get("id")) for c in project_characters
                   if str(c.get("display_name") or "").casefold() in {"василий", "вася", "vasily", "vasiliy"}
                   or str(c.get("id") or "").casefold() in {"vasily", "vasiliy", "discipline_vasily"}), "")
    knopka = _discipline_knopka_character_id(project_characters)
    if not vasily or not knopka:
        raise ValueError("Для этого формата нужны утверждённые персонажи Василий и Кнопка в проекте.")
    aliases = {name: vasily for name in ("vasiliy", "vasily", "vasilii", "vasya", "василий", "вася")}
    aliases.update({"knopka": knopka, "кнопка": knopka})
    cast_scenes = []
    for scene in scenes:
        # Explicit, per-scene user direction; other scenes retain the cast policy.
        if scene.get("discipline_cast_override") == "environment_only":
            scene["characters"] = []
            scene["references"] = {}
            scene["story_character_descriptions"] = {}
            scene["story_character_labels"] = {}
            continue
        cast_scenes.append(scene)
        ids = [aliases.get(str(value).casefold(), str(value)) for value in scene.get("characters") or []]
        scene["characters"] = [vasily, knopka] if scene.get(MODE_KEY) in SHORT_FORMATS else list(dict.fromkeys([vasily, *ids]))
    _apply_discipline_cast_policy(
        cast_scenes, project_characters=project_characters, primary_id=vasily, anastasia_id=anastasia,
        character_descriptions={c["id"]: c.get("description", "") for c in project_characters},
        character_labels={c["id"]: c.get("display_name", "") for c in project_characters},
    )


def _replace_anastasia_in_visual_text(value: Any, replacement: str) -> str:
    """Remove an accidental Anastasia direction from a non-first-person beat."""
    text = str(value or "")
    if not text or not replacement:
        return text
    text = re.sub(r"\bAnastasia's\b", f"{replacement}'s", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAnastasia\b", replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\bАнастаси(?:я|и|ю|ей|е)\b", replacement, text, flags=re.IGNORECASE)
    if replacement.casefold() in {"василий", "vasily"}:
        substitutions = (
            (r"\bHerself\b", "Himself"),
            (r"\bherself\b", "himself"),
            (r"\bShe\b", "He"),
            (r"\bshe\b", "he"),
            (r"\bHer\b", "His"),
            (r"\bher\b", "his"),
        )
        for pattern, substitute in substitutions:
            text = re.sub(pattern, substitute, text)
    return text


def _normalize_story_character_bible(value: Any) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in source:
        if not isinstance(item, dict):
            continue
        character_id = _story_character_id(item.get("id") or item.get("character_id"))
        if not character_id or character_id in {"grandma", "cat"} or character_id in seen:
            continue
        description = " ".join(str(item.get("description") or "").split())
        if not description:
            continue
        seen.add(character_id)
        out.append(
            {
                "id": character_id,
                "display_name": " ".join(str(item.get("display_name") or character_id).split()),
                "role": " ".join(str(item.get("role") or "исторический персонаж").split()),
                "age_stage": " ".join(str(item.get("age_stage") or "adult").split()),
                "same_person_group": _story_character_id(item.get("same_person_group") or character_id),
                "description": description,
                "reference_prompt": " ".join(str(item.get("reference_prompt") or description).split()),
                "requires_reference": bool(item.get("requires_reference", True)),
                "asset_uri": str(item.get("asset_uri") or "").strip(),
                "is_primary": bool(item.get("is_primary", False)),
            }
        )
    return out


def _history_beat_clips(beats):
    """Follow semantic boundaries with whole-second takes and <= 1s of trimming."""
    if not beats:
        return []
    total = max(float(b.get("end_sec") or 0) for b in beats)
    if total < 2:
        raise ValueError("История слишком короткая для экономного видеоклипа.")
    boundaries = [0.0]
    for beat in beats:
        end = min(total, float(beat.get("end_sec") or 0))
        if end - boundaries[-1] >= 3 and total - end >= 3:
            boundaries.append(end)
    boundaries.append(total)
    result = []
    for start, end in zip(boundaries, boundaries[1:]):
        count = max(1, math.ceil((end - start) / 8))
        for n in range(count):
            a = start + (end - start) * n / count
            b = start + (end - start) * (n + 1) / count
            overlaps = [v for v in beats if float(v.get("end_sec") or 0) > a and float(v.get("start_sec") or 0) < b]
            owner = overlaps[0] if overlaps else beats[0]
            result.append({"scene": len(result)+1, "start_sec": round(a, 3), "end_sec": round(b, 3),
                "generation_duration_sec": max(3, math.ceil(round(b-a, 3))),
                "narration_excerpt": " ".join(str(v.get("narration_excerpt") or "") for v in overlaps),
                "semantic_beat": int(owner.get("beat") or 1), "semantic_clip_index": n+1, "semantic_clip_count": count})
    return result


def _expand_semantic_beats_to_clips(
    beats: list[dict[str, Any]], *, continuous: bool = False,
) -> list[dict[str, Any]]:
    if not beats:
        return []
    duration = max(float(beat.get("end_sec") or 0.0) for beat in beats)
    # For one continuous shot, spread the narration evenly over whole four-second takes.
    # This avoids commissioning an extra take for a fraction of a second at the end.
    generated = 4.0 if continuous else STORY_CLIP_DURATION_SEC
    clip_count_total = max(1, round(duration / generated) if continuous else math.ceil(duration / generated))
    step = duration / clip_count_total if continuous else generated
    assignments: list[tuple[dict[str, Any], float, float]] = []
    for clip_index in range(clip_count_total):
        start = clip_index * step
        end = min(duration, (clip_index + 1) * step)
        beat = max(
            beats,
            key=lambda item: max(
                0.0,
                min(end, float(item.get("end_sec") or 0.0))
                - max(start, float(item.get("start_sec") or 0.0)),
            ),
        )
        assignments.append((beat, start, end))

    per_beat_counts: dict[int, int] = {}
    for beat, _start, _end in assignments:
        beat_number = int(beat.get("beat") or 1)
        per_beat_counts[beat_number] = per_beat_counts.get(beat_number, 0) + 1
    per_beat_cursor: dict[int, int] = {}
    clips: list[dict[str, Any]] = []
    for scene_number, (beat, start, end) in enumerate(assignments, start=1):
        beat_number = int(beat.get("beat") or 1)
        per_beat_cursor[beat_number] = per_beat_cursor.get(beat_number, 0) + 1
        clips.append(
            {
                "scene": scene_number,
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "generation_duration_sec": generated,
                "narration_excerpt": str(beat.get("narration_excerpt") or ""),
                "semantic_beat": beat_number,
                "semantic_clip_index": per_beat_cursor[beat_number],
                "semantic_clip_count": per_beat_counts[beat_number],
            }
        )
    return clips


def _apply_visualization_final_clip_policy(
    clips: list[dict[str, Any]],
    output_policy: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Preserve a short source tail by folding it into one four-second final clip."""
    policy = output_policy if isinstance(output_policy, dict) else {}
    if not bool(policy.get("preserve_short_tail_with_four_second_final_clip")) or len(clips) < 2:
        return clips
    try:
        base = float(policy.get("final_clip_base_sec") or STORY_CLIP_DURATION_SEC)
        max_tail = max(0.0, float(policy.get("max_preserved_tail_sec") or 0.5))
        generated_duration = float(policy.get("final_generation_duration_sec") or 4.0)
    except (TypeError, ValueError):
        return clips
    total_duration = float(clips[-1].get("end_sec") or 0.0)
    lower_multiple = math.floor((total_duration + 1e-9) / base) * base if base > 0 else 0.0
    tail = total_duration - lower_multiple
    previous = clips[-2]
    trailing = clips[-1]
    if not (
        base > 0
        and 1e-3 < tail <= max_tail + 1e-3
        and abs(float(previous.get("end_sec") or 0.0) - lower_multiple) <= 1e-3
        and abs(float(trailing.get("start_sec") or 0.0) - lower_multiple) <= 1e-3
    ):
        return clips
    merged = dict(previous)
    merged["end_sec"] = round(total_duration, 3)
    merged["generation_duration_sec"] = generated_duration
    excerpts = [
        " ".join(str(item.get("narration_excerpt") or "").split())
        for item in (previous, trailing)
    ]
    merged["narration_excerpt"] = " ".join(dict.fromkeys(text for text in excerpts if text))
    result = [dict(item) for item in clips[:-2]] + [merged]
    for index, item in enumerate(result, start=1):
        item["scene"] = index
    return result


def _normalize_grandma_age(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"young", "молодая", "retro_young", "young_retro"}:
        return "young"
    if raw in {"current", "today", "present", "сейчас", "пожилая"}:
        return "current"
    return "none"


def _normalize_story_time_period(value: Any, *, era_style: str, visual: str, grandma_age: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"past", "прошлое", "historical", "history", "retro"}:
        return "past"
    if raw in {"present", "настоящее", "modern", "current", "today"}:
        return "present"
    blob = f"{era_style} {visual}".lower()
    if grandma_age == "young" or any(
        token in blob
        for token in ("ретро", "histor", "century", "век", "архив", "revolution", "18th", "19th", "20th")
    ):
        return "past"
    return "present"


def _story_camera_direction(
    scene_index: int,
    requested: Any,
    *,
    previous: str = "",
    preserve_requested: bool = False,
) -> str:
    """Keep Story camera work explicit and varied even when the planner omits it."""
    if preserve_requested:
        return _safe_story_camera_direction(requested, default="locked-off camera")
    if scene_index == 0:
        return STORY_SAFE_ZOOM_IN_DIRECTION
    value = _safe_story_camera_direction(requested, default="")
    if value and value.casefold() != str(previous or "").casefold():
        return value
    fallback_index = (scene_index - 1) % len(STORY_CAMERA_DIRECTION_SEQUENCE)
    direction = STORY_CAMERA_DIRECTION_SEQUENCE[fallback_index]
    if direction.casefold() == str(previous or "").casefold():
        direction = STORY_CAMERA_DIRECTION_SEQUENCE[(fallback_index + 1) % len(STORY_CAMERA_DIRECTION_SEQUENCE)]
    return direction


_STORY_UNSAFE_VERTICAL_CAMERA_PATTERNS = (
    re.compile(r"\b(?:camera\s+)?tilt(?:s|ed|ing)?\s+(?:up|upward|upwards)\b", flags=re.IGNORECASE),
    re.compile(r"\btilt(?:s|ed|ing)?\s+the\s+camera\s+(?:up|upward|upwards)\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:camera\s+)?pan(?:s|ned|ning)?\s+(?:up|upward|upwards)\b", flags=re.IGNORECASE),
    re.compile(r"\bpan(?:s|ned|ning)?\s+the\s+camera\s+(?:up|upward|upwards)\b", flags=re.IGNORECASE),
    re.compile(r"\bbottom[\s-]+to[\s-]+top\b", flags=re.IGNORECASE),
    re.compile(r"\bснизу\s+вверх\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:наклон|панорамирование|движение)\s+камеры\s+вверх\b", flags=re.IGNORECASE),
)


def _story_camera_direction_reveals_outside_frame(value: Any) -> bool:
    text = " ".join(str(value or "").split())
    return any(pattern.search(text) for pattern in _STORY_UNSAFE_VERTICAL_CAMERA_PATTERNS)


def _safe_story_camera_direction(value: Any, *, default: str = STORY_SAFE_ZOOM_IN_DIRECTION) -> str:
    """Replace upward camera travel that can force image-to-video outpainting."""
    direction = " ".join(str(value or "").split())
    if not direction:
        return default
    if _story_camera_direction_reveals_outside_frame(direction):
        return STORY_SAFE_ZOOM_IN_DIRECTION
    return direction


def ensure_story_video_camera_safety(prompt: Any, *, allow_reveal: bool = False) -> str:
    """Keep Story video motion inside the approved source frame.

    Existing runs may already contain ``Tilt up`` in persisted prompts, so this
    guard is also applied immediately before video generation.
    """
    text = str(prompt or "").strip()
    if not text:
        return text
    guard = (
        "Keep the camera strictly inside the existing source-image boundaries; "
        "do not reveal or generate any area outside the frame, especially above it or the ceiling."
    )
    if allow_reveal:
        return re.sub(re.escape(guard), "", text, flags=re.IGNORECASE).strip()
    for pattern in _STORY_UNSAFE_VERTICAL_CAMERA_PATTERNS:
        text = pattern.sub(STORY_SAFE_ZOOM_IN_DIRECTION, text)
    if guard.casefold() not in text.casefold():
        text = f"{text.rstrip()} {guard}"
    return text


def _story_scene_environment(
    candidate: dict[str, Any],
    *,
    historical_scene: bool,
    source_story: str,
    scenario_environment: dict[str, Any] | None = None,
    locked_environment: tuple[str, str, str] | None = None,
    allow_transition: bool = False,
) -> tuple[str, str, str]:
    """Keep the current setting unless the plan explicitly motivates a move."""
    if locked_environment and not allow_transition:
        return locked_environment
    sequence = STORY_PAST_ENVIRONMENT_SEQUENCE if historical_scene else STORY_PRESENT_ENVIRONMENT_SEQUENCE
    stable_key = f"{'past' if historical_scene else 'present'}:{source_story}".encode("utf-8")
    fallback_index = int.from_bytes(hashlib.sha256(stable_key).digest()[:4], "big") % len(sequence)
    fallback_location, fallback_lighting, fallback_palette = sequence[fallback_index]
    approved = scenario_environment if isinstance(scenario_environment, dict) else {}
    primary = candidate if allow_transition else approved
    secondary = approved if allow_transition else candidate
    location = " ".join(
        str(
            primary.get("location")
            or primary.get("setting")
            or secondary.get("location")
            or secondary.get("setting")
            or fallback_location
        ).split()
    )
    lighting = " ".join(
        str(
            primary.get("lighting")
            or primary.get("light")
            or secondary.get("lighting")
            or secondary.get("light")
            or fallback_lighting
        ).split()
    )
    color_palette = " ".join(
        str(
            primary.get("color_palette")
            or primary.get("palette")
            or secondary.get("color_palette")
            or secondary.get("palette")
            or fallback_palette
        ).split()
    )
    return location, lighting, color_palette


def _story_location_transition(candidate: dict[str, Any]) -> bool:
    """Accept a setting change only when the planner marks and explains it."""
    raw = candidate.get("location_transition")
    enabled = raw is True or str(raw or "").strip().lower() in {"true", "yes", "да", "1"}
    transition_note = " ".join(str(candidate.get("transition_note") or "").split())
    return enabled and len(transition_note) >= 8


def _normalize_story_continuity_anchors(value: Any) -> list[str]:
    if isinstance(value, str):
        clean = " ".join(value.split())
        return [clean] if clean else []
    if not isinstance(value, (list, tuple)):
        return []
    return [" ".join(str(item).split()) for item in value if str(item).strip()]


def _ensure_story_environment_in_start_prompt(prompt: str, scene: dict[str, Any]) -> str:
    """Make the approved environment survive even if the prompt model omits it."""
    clean = str(prompt or "").strip()
    parts: list[str] = []
    location = " ".join(str(scene.get("location") or "").split())
    lighting = " ".join(str(scene.get("lighting") or "").split())
    color_palette = " ".join(str(scene.get("color_palette") or "").split())
    folded = clean.casefold()
    if location and location.casefold() not in folded:
        parts.append(f"Локация: {location}")
    if lighting and lighting.casefold() not in folded:
        parts.append(f"Свет: {lighting}")
    if color_palette and color_palette.casefold() not in folded:
        parts.append(f"Цветовая гамма: {color_palette}")
    if not parts:
        return clean
    suffix = ". ".join(parts).rstrip(". ") + "."
    return f"{clean.rstrip()} {suffix}".strip()


def generate_story_scene_plan(
    *,
    source_story: str,
    voiceover_text: str,
    duration_sec: float,
    alignment: dict[str, list[Any]] | None,
    user_feedback: str | None = None,
    previous_scenes: list[dict[str, Any]] | None = None,
    story_visual_style: str | None = None,
    project_characters: list[dict[str, Any]] | None = None,
    primary_character_id: str | None = None,
    project_slug: str | None = None,
    generation_mode: str | None = None,
    discipline_mode: str | None = None,
) -> dict[str, Any]:
    normalized_project_slug = str(project_slug or "").strip().lower()
    discipline_project = normalized_project_slug == "discipline"
    narrative_format = discipline_mode if discipline_project and discipline_mode in LABELS else ""
    short_format = SHORT_FORMATS.get(narrative_format)
    continuous = narrative_format == FLOW
    style_key = normalize_story_visual_style(short_format.style_key if short_format and narrative_format != ARCHIVE else story_visual_style)
    style_config = story_visual_style_config(style_key)
    selected_visual_style = style_config["prompt"]
    reference_style_only = story_style_uses_character_references_only(style_key)
    normalized_generation_mode = str(generation_mode or "").strip().lower()
    from .project_profiles import resolve_project_final_scene_prompt, resolve_project_title_preset

    project_final_scene_rule = (
        resolve_project_final_scene_prompt(normalized_project_slug)
        if normalized_project_slug and normalized_generation_mode == "visualization"
        else ""
    )
    project_title_preset = (
        resolve_project_title_preset(normalized_project_slug)
        if normalized_project_slug and normalized_generation_mode == "visualization"
        else {}
    )
    visualization_output_policy = (
        project_title_preset
        if normalized_generation_mode == "visualization"
        and str(project_title_preset.get("type") or "").strip() == "localized_end_title"
        else {}
    )
    grandma_project = normalized_project_slug in {"grandma_kitchen", "grandma_cat_cooking"}
    fixed_project_characters = _normalize_story_character_bible(project_characters or [])
    fixed_character_ids = {item["id"] for item in fixed_project_characters}
    requested_primary_id = _story_character_id(primary_character_id)
    primary_ids = [item["id"] for item in fixed_project_characters if item.get("is_primary")]
    if requested_primary_id in fixed_character_ids and requested_primary_id not in primary_ids:
        primary_ids.insert(0, requested_primary_id)
    primary_id = primary_ids[0] if primary_ids else ""
    discipline_anastasia_id = (
        _discipline_anastasia_character_id(fixed_project_characters) if discipline_project else ""
    )
    legacy_grandma_cast = not fixed_project_characters and not discipline_project
    grandma_story = legacy_grandma_cast or grandma_project
    semantic_beats = build_semantic_narration_beats(voiceover_text, duration_sec, alignment)
    windows = (_history_beat_clips(semantic_beats) if narrative_format == ARCHIVE else
               _expand_semantic_beats_to_clips(semantic_beats, continuous=bool(short_format)))
    if normalized_generation_mode == "visualization":
        windows = _apply_visualization_final_clip_policy(windows, visualization_output_policy)
    clips_key = "semantic_clips" if narrative_format == ARCHIVE else "continuous_clips" if continuous else ("four_second_clips" if short_format else "three_second_clips")
    payload = {
        "source_story": source_story,
        "approved_voiceover": voiceover_text,
        "semantic_beats": semantic_beats,
        clips_key: windows,
        "previous_scenes": previous_scenes or [],
        "user_feedback": str(user_feedback or "").strip(),
        "project_characters": fixed_project_characters,
        "primary_project_character_id": primary_id or None,
        "primary_project_character_ids": primary_ids,
        "project_slug": normalized_project_slug or None,
        "generation_mode": normalized_generation_mode or None,
        "project_final_scene_rule": project_final_scene_rule or None,
        "visualization_output_policy": visualization_output_policy or None,
    }
    if not reference_style_only:
        payload["selected_visual_style"] = {
            "key": style_key,
            "label": style_config["label"],
            "visual_language": selected_visual_style,
        }
    style_direction = ""
    if not reference_style_only:
        style_direction = (
            f"Пользователь выбрал единый стиль ВСЕХ кадров и всех персонажей — "
            f"{style_config['label']}: {selected_visual_style}. "
            "Применяй одинаковый материал, фактуру, рендер и палитру и к прошлому, и к настоящему; "
            "эпоху передавай одеждой, причёсками, архитектурой и реквизитом, а не сменой художественного стиля. "
        )
    if discipline_project and primary_ids:
        primary_ids_text = ", ".join(primary_ids)
        project_character_direction = (
            f"Это проект «Дисциплина». Доступные основные современные герои: {primary_ids_text}. "
            "В каждом clip_visual обязательно должен быть хотя бы один утверждённый герой проекта; предметные кадры без героев запрещены. "
            "Не добавляй всех героев автоматически: выбирай состав, нужный действию. Анастасия может быть как одна, так и вместе с Василием "
            "или Кнопкой. Василия часто показывай вместе с Кнопкой — минимум в 60% кадров с Василием. "
            "Каждый метод из narration_excerpt показывай конкретным наблюдаемым действием, "
            "а не абстрактным символом. Современные сцены складываются в ясную дугу: выбранный герой применяет названные методы, "
            "становится последовательнее, а в последней современной сцене обязательно доводит начатое дело до конца и фиксирует результат. "
            "Если нужен исторический эпизод, всё равно оставь хотя бы одного утверждённого героя проекта в кадре как наблюдателя или визуального рассказчика. "
        )
    elif primary_id:
        project_character_direction = (
            f"Главный герой проекта — {primary_id}; показывай его естественно минимум в 60% сцен, когда это не противоречит фактам. "
        )
    else:
        project_character_direction = ""
    if discipline_project and discipline_anastasia_id and not narrative_format:
        project_character_direction += (
            f"Особое правило первого лица: если narration_excerpt содержит отдельное русское местоимение «я», "
            f"во всех clip_visuals этого смыслового эпизода показывай Анастасию ({discipline_anastasia_id}) как главную героиню-рассказчицу. "
            "Она может быть одна или вместе с Василием и/или Кнопкой и остаётся заметной в кадре до конца всей фразы, "
            "даже если фраза занимает несколько клипов. "
            "Если первый смысловой эпизод содержит «я», покажи Анастасию крупным поясным планом: она держит в обеих руках веер из узнаваемых популярных книг по продуктивности, а обложки направлены к камере. "
            "Книги обязательно находятся у неё в руках, а не только на полках или столе. В видео она поднимает книги, раскрывает их веером и поворачивает обложки к зрителю. "
            "например визуальными отсылками к Atomic Habits, Getting Things Done, Deep Work и The 7 Habits; "
            "обложки узнаются по цвету и композиции, но без читаемых названий, имён авторов и логотипов. "
        )
    if narrative_format:
        project_character_direction = visual_direction(narrative_format)
        project_character_direction += (" Используй точные id исполнителей исторических ролей из project_characters. Василий играет главного героя, Анастасия женскую роль. Кнопку не добавляй автоматически. Лица остаются утверждёнными, меняются только согласованные костюмы эпохи." if narrative_format == ARCHIVE else " Используй только точные id Василия и Кнопки из project_characters, не придумывай им новые id.")
    if short_format:
        project_character_direction += " В этих выпусках весь актёрский состав ограничен project_characters."
    if continuous:
        project_character_direction += " Начни с Василия и Кнопки вместе и держи их в одной декорации до конца."
    if project_final_scene_rule:
        project_character_direction += (
            " Обязательное постоянное правило финальной сцены этого проекта: "
            f"{project_final_scene_rule} "
        )
    camera_instruction = (
        "Всю историю снимай в одной декорации неподвижной камерой с одной точки. "
        "В camera_direction каждого клипа указывай locked-off camera. Положение, свет и палитра постоянны. "
        "Варьируй действие и позу, сохраняя один главный визуальный факт в кадре. "
        if continuous else
        "Чередуй общий, средний, крупный и макро-планы; сохраняй один главный визуальный факт на кадр. "
        "В camera_direction обязательно укажи конкретное движение или способ съёмки на английском. "
        f"Для самого первого clip_visual всегда используй {STORY_SAFE_ZOOM_IN_DIRECTION}. "
        "Никогда не используй tilt up, pan up или движение снизу вверх: они заставляют image-to-video дорисовывать область "
        "за исходным кадром, особенно потолок. Любое такое движение заменяй оптическим приближением строго внутри границ исходного изображения. "
        "В следующих клипах осмысленно чередуй tracking shot, POV shot, optical zoom-in within the existing frame, "
        "over-the-shoulder shot, gentle orbit shot и другие подходящие кинематографичные варианты. "
        "Не повторяй одно и то же движение камеры в соседних клипах и не выбирай приём, который противоречит действию сцены."
    )
    if short_format and not continuous and narrative_format != ARCHIVE:
        camera_instruction = (
            "Выбери крупность по действию и ремаркам, внутри клипа camera_direction=locked-off camera. "
            "Не чередуй случайные движения камеры. Смена плана между клипами только по смыслу. "
        )
    if narrative_format == ARCHIVE:
        camera_instruction = (
            "Крупный план реакции и предметная деталь приветствуются. Выбирай крупность по смыслу. "
            "Внутри клипа максимум одно мотивированное движение камеры, допустима неподвижная камера. "
            "Сохраняй направление взгляда и ось действия, не навязывай случайное вращение или смену движения. "
            "Опиши camera_direction на английском; оптический наезд внутри кадра безопаснее раскрытия неизвестного пространства. "
        )
    if narrative_format == TWO_LIVES:
        camera_instruction = (
            "Две фиксированные горизонтальные панели. Во всех клипах camera_direction=locked-off camera. "
            "Ракурс и граница панелей неизменны. Время сверху и снизу может расходиться по сценарию. "
            "Переходы по совпадающей позе, без случайных приближений и перемещения камеры. "
        )
    location_instruction = (
        "В каждом клипе сохраняй исходную локацию и выставляй location_transition=false. "
        if continuous else
        "Сменить место внутри ролика можно только как ясное сюжетное событие: герои вышли из дома, пришли в ресторан, спортивный зал, "
        "парк, офис или другое нужное для действия место. Для первого кадра новой локации поставь location_transition=true и в "
        "transition_note кратко объясни переход и его связь с narration_excerpt. После перехода зафиксируй новую локацию и сохраняй её "
        "в последующих клипах до следующего так же обоснованного перехода. Не меняй фон только ради визуального разнообразия и не делай "
        "необъяснимых скачков между местами. Если источник действительно требует "
        "исторического перехода, зафиксируй отдельно одну historical_environment для всего исторического слоя; не добавляй такой переход "
        "без сюжетной необходимости. Для location всегда называй конкретное место, для lighting — характер света, для color_palette — "
        "2–4 ведущих цвета. Не меняй время суток или цветовую основу внутри одного временного слоя. "
    )
    if narrative_format == TWO_LIVES:
        location_instruction = (
            "Фиксированные панели. Локации менять только по явно заданным действиям сценария; при таком переходе location_transition=true и transition_note объясняет причину смены места. "
            "Время в панелях может расходиться. Смену дней, сезонов и лет показывай только согласно "
            "ремаркам, сохраняя стороны панелей и модели героев. "
        )
    prompt = (
        "Ты режиссёр короткой немой визуальной истории под уже утверждённую закадровую дорожку. "
        "Верни только JSON-объект с ключами characters, scenario_environment, historical_environment и beats. "
        "Один beat строго на каждый semantic_beats. "
        "Не дроби и не переписывай фразу озвучки внутри смыслового эпизода.\n"
        "characters — библия всех повторяющихся или сюжетно важных людей. Персонажи из project_characters уже утверждены: "
        "не меняй их id, внешность и роль, включи их в characters и используй подходящих из них в сценах. "
        "Для одного героя в разных возрастах создай отдельные id (например antonin_young и antonin_adult), но одинаковый "
        "same_person_group. Поля: id, display_name, role, age_stage, same_person_group, description, reference_prompt, "
        "requires_reference. Description фиксирует лицо, волосы, телосложение и отличительные признаки; одежда исторически точна.\n"
        f"Каждый beat: beat, visual_concept, clip_visuals. Число clip_visuals равно числу {clips_key} "
        "этого semantic_beat. Каждый clip_visual: visual, shot_type, camera_direction, location, lighting, color_palette, "
        "grandma_age (young/current/none), cat_present, time_period (present/past), "
        "characters (grandma, cat и/или id из characters), continuity_anchors, location_segment, "
        "location_transition (true/false), transition_note.\n"
        "Все clip_visuals одного beat подтверждают ПОЛНЫЙ смысл narration_excerpt, а не отдельные обрывки слов. "
        "Первый clip_visual сразу устанавливает весь смысловой тезис; следующие развивают его визуально. "
        f"{camera_instruction}"
        "Выбери основную начальную физическую локацию современного слоя и верни её в scenario_environment с полями location, "
        "lighting и color_palette. Сохраняй архитектуру, ключевой реквизит, основу света и палитру. "
        f"{location_instruction}"
        + (
            "Кот должен быть естественным повторяющимся свидетелем минимум в 60% сцен, но не закрывать главный объект. "
            "В первую очередь помещай его в иначе статичные кадры с картами, вывесками, фасадами, полками "
            "и предметными композициями: его моргание, поворот ушей, хвоста или шаг добавляют живое движение. "
            "Бабушка — повторяющаяся героиня проекта: в её семейных воспоминаниях она young в правдоподобной ретро-эпохе, "
            "в современных сценах current. Не помещай современную пожилую бабушку в прошлое. "
            if grandma_story
            else "Не добавляй бабушку или кота автоматически. Используй только героев, нужных исходной истории, и утверждённых project_characters. "
        )
        + project_character_direction
        +
        "Исторические окна должны быть достоверны по эпохе: одежда, интерьер и транспорт соответствуют времени. "
        + style_direction
        +
        "Никакого текста, логотипов, субтитров или говорящих губ в кадре. Вертикальная композиция 9:16.\n"
        "Если дан feedback, измени previous_scenes по нему, не меняя утверждённый voiceover и тайминг окон.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    from .directorial_notes import RULE
    prompt = RULE + prompt
    raw = gemini_generate_sync(prompt, model_profile=GEMINI_MODEL_PROFILE_CREATIVE)
    data = extract_json_object(raw)
    generated_character_bible = _normalize_story_character_bible(data.get("characters"))
    character_bible = [*fixed_project_characters]
    if not short_format:
        character_bible.extend(item for item in generated_character_bible if item["id"] not in fixed_character_ids)
    character_ids = {item["id"] for item in character_bible}
    character_descriptions = {item["id"]: item["description"] for item in character_bible}
    character_labels = {item["id"]: item["display_name"] for item in character_bible}
    source_beats = data.get("beats") if isinstance(data.get("beats"), list) else []
    legacy_scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
    scenario_environment = data.get("scenario_environment") if isinstance(data.get("scenario_environment"), dict) else {}
    historical_environment = data.get("historical_environment") if isinstance(data.get("historical_environment"), dict) else {}
    environment_locks: dict[str, tuple[str, str, str]] = {}
    normalized: list[dict[str, Any]] = []
    previous_camera_direction = ""
    for index, window in enumerate(windows):
        beat_index = int(window.get("semantic_beat") or 1) - 1
        clip_index = int(window.get("semantic_clip_index") or 1) - 1
        beat_candidate = source_beats[beat_index] if beat_index < len(source_beats) and isinstance(source_beats[beat_index], dict) else {}
        beat_clips = beat_candidate.get("clip_visuals") if isinstance(beat_candidate.get("clip_visuals"), list) else []
        if clip_index < len(beat_clips) and isinstance(beat_clips[clip_index], dict):
            candidate = beat_clips[clip_index]
        else:
            candidate = legacy_scenes[index] if index < len(legacy_scenes) and isinstance(legacy_scenes[index], dict) else {}
        visual = " ".join(str(candidate.get("visual") or window["narration_excerpt"] or "Переходный атмосферный кадр").split())
        age = _normalize_grandma_age(candidate.get("grandma_age"))
        cat_present = bool(candidate.get("cat_present"))
        chars = [_story_character_id(item) for item in candidate.get("characters") or []]
        if narrative_format == ARCHIVE and any(item not in character_ids for item in chars):
            raise ValueError("Историческая сцена содержит неизвестного исполнителя. Используйте утверждённые character_id проекта.")
        chars = [
            item
            for item in chars
            if item in character_ids or (grandma_story and item in {"grandma", "cat"})
        ]
        if grandma_story and age == "none" and "grandma" in chars:
            era_blob = f"{candidate.get('era_style') or ''} {visual}".lower()
            age = (
                "young"
                if any(token in era_blob for token in ("ретро", "истор", "прошл", "архив", "retro", "histor", "past"))
                else "current"
            )
        if grandma_story and age != "none" and "grandma" not in chars:
            chars.append("grandma")
        if grandma_story and cat_present and "cat" not in chars:
            chars.append("cat")
        if not grandma_story:
            age = "none"
            cat_present = False
        era_style_raw = str(candidate.get("era_style") or "").strip()
        time_period = _normalize_story_time_period(
            candidate.get("time_period"),
            era_style=era_style_raw,
            visual=visual,
            grandma_age=age,
        )
        if continuous and normalized:
            time_period = normalized[0]["time_period"]
        historical_scene = time_period == "past"
        location_transition = not continuous and _story_location_transition(candidate)
        location, lighting, color_palette = _story_scene_environment(
            candidate,
            historical_scene=historical_scene,
            source_story=source_story,
            scenario_environment=historical_environment if historical_scene else scenario_environment,
            locked_environment=environment_locks.get(time_period),
            allow_transition=location_transition,
        )
        environment_locks[time_period] = (location, lighting, color_palette)
        camera_direction = "locked-off camera" if short_format and narrative_format != ARCHIVE else _story_camera_direction(
            index,
            candidate.get("camera_direction") or candidate.get("camera_movement"),
            previous=previous_camera_direction,
            preserve_requested=narrative_format == ARCHIVE,
        )
        previous_camera_direction = camera_direction
        if reference_style_only:
            era_style = ""
        elif historical_scene:
            # Gemini occasionally leaves its generic modern default on a beat
            # which is unambiguously historical.  Do not pass contradictory
            # directions such as "cinematic contemporary; historical sepia"
            # to the image model.
            generic_modern_styles = {
                "cinematic contemporary",
                "contemporary",
                "modern",
                "present",
                "настоящее",
                "современность",
            }
            historical_context = "" if era_style_raw.lower() in generic_modern_styles else era_style_raw
            era_style = "; ".join(
                part
                for part in (historical_context, selected_visual_style)
                if part
            )
        else:
            era_style = selected_visual_style
            age = "current" if grandma_story else "none"
            if grandma_story and "grandma" not in chars:
                chars.insert(0, "grandma")
        normalized.append(
            {
                **window,
                **({MODE_KEY: narrative_format} if short_format else {}),
                "duration_sec": float(window.get("generation_duration_sec") or STORY_CLIP_DURATION_SEC),
                "timeline_duration_sec": round(float(window["end_sec"]) - float(window["start_sec"]), 3),
                "model": "kling_3_0",
                "generation_mode": "story",
                "aspect_ratio": "9:16",
                "voice_provider": "elevenlabs",
                "voice_needed": False,
                "has_lipsync": False,
                "text": str(window["narration_excerpt"]),
                "recipe_step": f"{window['start_sec']:.0f}–{window['end_sec']:.0f} сек: {window['narration_excerpt']}",
                "action": visual,
                "display_action": visual,
                "shot_type": normalized[0]["shot_type"] if continuous and normalized else str(candidate.get("shot_type") or "medium").strip(),
                "camera_direction": camera_direction,
                "location": location,
                "lighting": lighting,
                "color_palette": color_palette,
                "location_segment": str(candidate.get("location_segment") or "").strip(),
                "location_transition": location_transition,
                "era_style": era_style,
                "time_period": time_period,
                "story_visual_style_key": style_key,
                "story_visual_style_label": style_config["label"],
                "style_reference_paths": (
                    [style_config["reference_path"]]
                    if style_config["reference_path"] and not reference_style_only
                    else []
                ),
                "grandma_age": age,
                "cat_present": cat_present,
                "characters": chars,
                "story_character_descriptions": {
                    key: character_descriptions[key] for key in chars if key in character_descriptions
                },
                "story_character_labels": {
                    key: character_labels[key] for key in chars if key in character_labels
                },
                "continuity_anchors": _normalize_story_continuity_anchors(candidate.get("continuity_anchors")),
                "transition_note": str(candidate.get("transition_note") or "").strip(),
                "sound_design": "Нет: Kling-клип генерируется полностью без звука.",
                "silence_base_rules": (
                    f"{float(window.get('generation_duration_sec') or STORY_CLIP_DURATION_SEC):g}-second silent Kling clip; "
                    "the external approved ElevenLabs narration is added only in final assembly."
                ),
            }
        )
    if normalized and visualization_output_policy:
        final_scene = normalized[-1]
        title_by_lang = visualization_output_policy.get("end_title_by_lang")
        title_style = visualization_output_policy.get("style")
        if isinstance(title_by_lang, dict) and title_by_lang:
            final_scene["end_title_by_lang"] = {
                str(key).strip().lower(): " ".join(str(value or "").split())
                for key, value in title_by_lang.items()
                if str(key).strip() and str(value or "").strip()
            }
        if isinstance(title_style, dict) and title_style:
            final_scene["end_title_style"] = dict(title_style)
        final_scene["end_title_duration_sec"] = float(
            visualization_output_policy.get("end_title_duration_sec")
            or final_scene.get("timeline_duration_sec")
            or STORY_CLIP_DURATION_SEC
        )
        final_scene["visualization_output_policy"] = dict(visualization_output_policy)
    if narrative_format:
        enforce_discipline_narrative_cast(normalized, fixed_project_characters)
    if discipline_project and not narrative_format:
        present_scenes = [item for item in normalized if str(item.get("time_period") or "present") == "present"]
        first_person_scenes = [
            item
            for item in normalized
            if discipline_anastasia_id
            and _contains_russian_first_person_pronoun(item.get("narration_excerpt"))
        ]
        managed_scenes = list(present_scenes)
        managed_scenes.extend(item for item in first_person_scenes if item not in present_scenes)
        for item in managed_scenes:
            first_person_scene = bool(
                discipline_anastasia_id
                and _contains_russian_first_person_pronoun(item.get("narration_excerpt"))
            )
            existing_character_ids = list(
                dict.fromkeys(str(value) for value in item.get("characters") or [] if str(value))
            )
            if first_person_scene:
                scene_character_ids = [discipline_anastasia_id]
                scene_character_ids.extend(
                    character_id
                    for character_id in existing_character_ids
                    if character_id != discipline_anastasia_id
                )
            else:
                scene_character_ids = existing_character_ids
            item["characters"] = scene_character_ids
            item["story_character_descriptions"] = {
                character_id: character_descriptions[character_id] for character_id in scene_character_ids
            }
            item["story_character_labels"] = {
                character_id: character_labels[character_id] for character_id in scene_character_ids
            }
            anchors = [str(value).strip() for value in item.get("continuity_anchors") or [] if str(value).strip()]
            if first_person_scene:
                first_person_anchor = (
                    "Фраза от первого лица: Анастасия — главная рассказчица и остаётся заметной до конца фразы; "
                    "рядом могут быть Василий и Кнопка"
                )
                if first_person_anchor not in anchors:
                    anchors.append(first_person_anchor)
                action_suffix = (
                    "Анастасия — главная героиня-рассказчица и остаётся заметной в кадре до конца фразы; "
                    "другие герои могут находиться рядом, не перекрывая её."
                )
                if int(item.get("semantic_beat") or 1) == 1:
                    books_anchor = (
                        "Вся первая фраза: Анастасия крупно в кадре, держит популярные книги по продуктивности в обеих руках и показывает их зрителю; "
                        "без читаемого текста и логотипов"
                    )
                    if books_anchor not in anchors:
                        anchors.append(books_anchor)
                    item["shot_type"] = "tight medium close-up"
                    item["action"] = (
                        "Tight medium close-up in vertical 9:16: Anastasia fills most of the frame from the waist up and looks directly at the viewer. "
                        "She holds a fan of several physical productivity books securely in both hands at chest level, with the front covers facing the camera; "
                        "the books must be in her hands, not merely on shelves or on the desk. Their cover colors and compositions visually evoke Atomic Habits, "
                        "Getting Things Done, Deep Work and The 7 Habits without readable titles, author names or logos. During the video Anastasia actively lifts "
                        "the books, fans them one by one and turns their covers toward the viewer as if proudly showing the collection. She never puts the books down. "
                        "Anastasia remains the prominent narrator for the entire phrase; Vasily and/or Knopka may be nearby without obscuring her or the books. "
                        "No speaking or lip movement."
                    )
                else:
                    item["action"] = f"{str(item.get('action') or '').rstrip('. ')}. {action_suffix}".strip()
                item["display_action"] = item["action"]
            else:
                anchors = [anchor for anchor in anchors if "Анастаси" not in anchor and "Anastasia" not in anchor]
                if "Выбранный герой применяет метод дисциплины из текущей фразы" not in anchors:
                    anchors.append("Выбранный герой применяет метод дисциплины из текущей фразы")
            item["continuity_anchors"] = anchors
        _apply_discipline_cast_policy(
            normalized,
            project_characters=fixed_project_characters,
            primary_id=primary_id,
            anastasia_id=discipline_anastasia_id,
            character_descriptions=character_descriptions,
            character_labels=character_labels,
        )
        if present_scenes:
            final_scene = present_scenes[-1]
            final_character_ids = [str(value) for value in final_scene.get("characters") or []]
            final_subject = (
                "Анастасия, последовательно применив описанные в озвучке методы, доводит"
                if discipline_anastasia_id and final_character_ids == [discipline_anastasia_id]
                else "Основные персонажи, последовательно применив описанные в озвучке методы, доводят"
            )
            final_verb = "фиксирует" if final_subject.startswith("Анастасия") else "фиксируют"
            final_scene["action"] = (
                f"{str(final_scene.get('action') or '').rstrip('. ')}. "
                f"{final_subject} начатые дела до конца и явно {final_verb} выполненный результат."
            ).strip()
            final_scene["display_action"] = final_scene["action"]
            final_scene["continuity_anchors"].append(
                "Финал: начатое дело завершено, результат явно отмечен как выполненный"
            )
    recurring_id = "cat" if grandma_story else primary_id
    if recurring_id and not discipline_project:
        required_count = int(math.ceil(len(normalized) * 0.60))
        current_count = sum(1 for item in normalized if recurring_id in (item.get("characters") or []))
        # Prefer otherwise static shots without people (maps, signs, facades,
        # shelves and object inserts): the cat adds a natural living motion and
        # keeps those clips from feeling like still illustrations.  Fill the
        # remaining quota with character scenes.
        missing = [item for item in normalized if recurring_id not in (item.get("characters") or [])]
        candidates = [item for item in missing if not item.get("characters")]
        candidates.extend(item for item in missing if item.get("characters"))
        for item in candidates:
            if current_count >= required_count:
                break
            item["characters"].append(recurring_id)
            if recurring_id == "cat":
                item["cat_present"] = True
            current_count += 1
    character_usage: dict[str, int] = {item["id"]: 0 for item in character_bible}
    group_members: dict[str, set[str]] = {}
    for character in character_bible:
        group = str(character.get("same_person_group") or character["id"])
        group_members.setdefault(group, set()).add(str(character["id"]))
    for scene in normalized:
        for character_id in set(str(x) for x in scene.get("characters") or []):
            if character_id in character_usage:
                character_usage[character_id] += 1
    for character in character_bible:
        character_id = str(character["id"])
        group = str(character.get("same_person_group") or character_id)
        # Reference approval is valuable for recurring people and for every age
        # variant of the same protagonist. One-off background people do not
        # trigger extra paid image generations.
        character["requires_reference"] = bool(
            not str(character.get("asset_uri") or "").strip()
            and (character_usage.get(character_id, 0) >= 2 or len(group_members.get(group, set())) >= 2)
        )
    return {
        "json_plan": normalized,
        "text_plan": format_story_scene_plan(normalized),
        "character_bible": character_bible,
        "semantic_beats": semantic_beats,
    }


def format_story_scene_plan(scenes: list[dict[str, Any]]) -> str:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for scene in scenes:
        grouped.setdefault(int(scene.get("semantic_beat") or scene.get("scene") or 1), []).append(scene)
    blocks: list[str] = []
    for beat_number, beat_scenes in grouped.items():
        first = beat_scenes[0]
        last = beat_scenes[-1]
        start = float(first.get("start_sec") or 0.0)
        end = float(last.get("end_sec") or 0.0)
        clip_lines: list[str] = []
        cast: list[str] = []
        for scene in beat_scenes:
            labels = scene.get("story_character_labels") if isinstance(scene.get("story_character_labels"), dict) else {}
            for key in scene.get("characters") or []:
                label = {"grandma": "бабушка", "cat": "кот"}.get(str(key), str(labels.get(str(key)) or key))
                if label and label not in cast:
                    cast.append(label)
        periods = {str(scene.get("time_period") or "present") for scene in beat_scenes}
        period_label = "прошлое" if periods == {"past"} else "настоящее" if periods == {"present"} else "переход: прошлое → настоящее"
        for scene in beat_scenes:
            clip_start = float(scene.get("start_sec") or 0.0)
            clip_end = float(scene.get("end_sec") or 0.0)
            age = {"young": "молодая в ретро", "current": "в наши дни", "none": "нет в кадре"}.get(
                str(scene.get("grandma_age") or "none"), "нет в кадре"
            )
            labels = scene.get("story_character_labels") if isinstance(scene.get("story_character_labels"), dict) else {}
            clip_cast: list[str] = []
            for key in scene.get("characters") or []:
                label = {"grandma": "бабушка", "cat": "кот"}.get(str(key), str(labels.get(str(key)) or key))
                if label and label not in clip_cast:
                    clip_cast.append(label)
            clip_period = "прошлое" if str(scene.get("time_period") or "present") == "past" else "настоящее"
            clip_lines.append(
                f"  КЛИП {beat_number}.{scene.get('semantic_clip_index') or 1} · {clip_start:.0f}–{clip_end:.0f} сек\n"
                f"  Персонажи: {', '.join(clip_cast) if clip_cast else 'без персонажей'}\n"
                f"  Время: {clip_period}\n"
                f"  Локация: {scene.get('location') or '—'}; свет: {scene.get('lighting') or '—'}; "
                f"палитра: {scene.get('color_palette') or '—'}\n"
                f"  Видеоряд: {scene.get('action') or '—'}\n"
                f"  План: {scene.get('shot_type') or '—'}; камера: {scene.get('camera_direction') or '—'}; "
                f"стиль/эпоха: {scene.get('era_style') or '—'}\n"
                f"  Бабушка: {age}; кот: {'да' if scene.get('cat_present') else 'нет'}"
            )
        blocks.append(
            f"СЦЕНА {beat_number} · {start:.0f}–{end:.0f} сек · смысловой эпизод\n"
            f"Персонажи: {', '.join(cast) if cast else 'без персонажей'}\n"
            f"Время: {period_label}\n"
            f"Озвучка рядом: {first.get('narration_excerpt') or '—'}\n"
            + "\n".join(clip_lines)
        )
    return "\n\n---\n\n".join(blocks)


def _fallback_start_prompt(scene: dict[str, Any]) -> str:
    age = str(scene.get("grandma_age") or "none")
    grandma = ""
    if age == "young":
        grandma = "Та же бабушка проекта показана молодой женщиной в исторически точной ретро-одежде; узнаваемые черты лица сохранены. "
    elif age == "current":
        grandma = "Бабушка проекта в её современном возрасте. "
    cat = "Рыжий кот проекта естественно присутствует рядом. " if scene.get("cat_present") else ""
    era_blob = f"{scene.get('era_style') or ''} {scene.get('action') or ''}".lower()
    reference_style_only = story_style_uses_character_references_only(scene.get("story_visual_style_key"))
    selected_style = story_visual_style_config(scene.get("story_visual_style_key"))["prompt"]
    retro_style = f"Единый визуальный стиль всего ролика: {selected_style}. " if not reference_style_only else ""
    era_line = f"Эпоха и стиль: {scene.get('era_style')}. " if str(scene.get("era_style") or "").strip() else ""
    prompt = (
        "Вертикальный кадр 9:16, кинематографичная композиция без текста. "
        f"{scene.get('action')}. {grandma}{cat}{retro_style}"
        f"Локация: {scene.get('location') or 'пространство, логично следующее из действия'}. "
        f"Свет: {scene.get('lighting') or 'кинематографичный естественный свет'}. "
        f"Цветовая гамма: {scene.get('color_palette') or 'гармоничная цветная палитра проекта'}. "
        f"{era_line}План: {scene.get('shot_type')}. Подготовка движения камеры: {scene.get('camera_direction') or 'static shot'}. "
        "Без логотипов, водяных знаков, субтитров и читаемых надписей."
    )
    return _ensure_story_environment_in_start_prompt(prompt, scene)


def _fallback_video_prompt(scene: dict[str, Any]) -> str:
    camera_direction = str(scene.get("camera_direction") or "subtle cinematic camera motion").strip()
    duration = int(round(float(scene.get("generation_duration_sec") or scene.get("duration_sec") or 3.0)))
    duration = max(3, min(15, duration)) if scene.get(MODE_KEY) == ARCHIVE else (4 if duration >= 4 else 3)
    return (
        f"{duration}-second Kling 3.0 image-to-video shot. No audio. No dialogue, no speech, no lip sync, no music, "
        "no ambient sound, no on-screen text, no subtitles, no logos, no watermark. "
        f"Camera direction: {camera_direction}. "
        f"Animate only this visual beat: {scene.get('action')}. "
        "Use subtle natural body, fabric and environmental motion; use only the specified camera direction; preserve every character identity, object, era, "
        "location, lighting, color palette and composition from the start frame."
    )


def _ensure_story_camera_direction(video_prompt: str, camera_direction: Any, *, duration_sec: float = 3.0) -> str:
    direction = " ".join(str(camera_direction or "subtle cinematic camera motion").split())
    if direction.casefold() in video_prompt.casefold():
        return video_prompt
    duration = max(3, min(15, int(round(float(duration_sec or 3.0)))))
    required_prefix = f"{duration}-second Kling 3.0 image-to-video shot. No audio."
    if video_prompt[: len(required_prefix)].casefold() == required_prefix.casefold():
        return f"{video_prompt[:len(required_prefix)]} Camera direction: {direction}.{video_prompt[len(required_prefix):]}"
    return f"Camera direction: {direction}. {video_prompt}".strip()


def generate_story_production(
    scenes_plan: list[dict[str, Any]],
    *,
    user_feedback: str | None = None,
    previous_enriched_scenes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    safe_scenes_plan: list[dict[str, Any]] = []
    previous_camera_direction = ""
    for index, source_scene in enumerate(scenes_plan):
        scene = dict(source_scene)
        scene["camera_direction"] = "locked-off camera" if scene.get(MODE_KEY) in SHORT_FORMATS and scene.get(MODE_KEY) != ARCHIVE else _story_camera_direction(
            index,
            scene.get("camera_direction"),
            previous=previous_camera_direction,
            preserve_requested=scene.get(MODE_KEY) == ARCHIVE,
        )
        previous_camera_direction = str(scene["camera_direction"])
        safe_scenes_plan.append(scene)
    style_key = normalize_story_visual_style(
        next(
            (
                scene.get("story_visual_style_key")
                for scene in safe_scenes_plan
                if isinstance(scene, dict) and scene.get("story_visual_style_key")
            ),
            STORY_DEFAULT_VISUAL_STYLE,
        )
    )
    style_config = story_visual_style_config(style_key)
    reference_style_only = story_style_uses_character_references_only(style_key)
    payload = {
        "scenes": safe_scenes_plan,
        "previous_prompts": previous_enriched_scenes or [],
        "user_feedback": str(user_feedback or "").strip(),
    }
    style_instruction = (
        "Every scene and every character, both past and present, must use this exact visual language: "
        f"{style_config['prompt']}. Keep the selected material, rendering technique, texture and palette identical across the whole video. "
        "Convey historical period through clothing, hair, architecture and props without switching art style. "
        if not reference_style_only
        else ""
    )
    continuous = any(scene.get(MODE_KEY) == FLOW for scene in safe_scenes_plan)
    camera_instruction = (
        "One continuous locked-off cel-animation shot. Begin each clip in the exact previous ending pose, "
        "carry the prop's movement across the boundary and preserve its direction, pace and screen position. "
        "No reframing, cuts or dissolves. Both project characters stay on model. "
        if continuous else
        f"Use each scene's camera_direction literally in its video prompt: the first clip is {STORY_SAFE_ZOOM_IN_DIRECTION}. "
        "Never use tilt up, pan up or any bottom-to-top camera travel. Replace it with a slow optical zoom-in that remains strictly "
        "inside the existing source-image boundaries and never reveals or generates space outside the frame, especially the ceiling. "
        "Later clips may use the approved varied tracking shot, POV shot, optical zoom-in, over-the-shoulder shot, gentle orbit shot "
        "or another planned camera technique that stays within the source frame. "
    )
    if any(scene.get(MODE_KEY) == ARCHIVE for scene in safe_scenes_plan):
        camera_instruction = "Use each approved camera_direction literally, including a locked-off first shot. Do not add an unplanned zoom or camera move. Stay inside the source-image boundaries. "
    prompt = (
        "Create production prompts for an approved narrated story timeline. Return only a JSON object with key prompts, "
        "one item per input scene in exactly the same order. Each item has start_frame_prompt_ru and video_prompt_en.\n"
        "START: self-contained Russian vertical 9:16 still-frame prompt, precise composition/camera, action not yet completed, "
        "correct era, project character identity and age, and only the requested characters. Explicitly include each scene's "
        "location, lighting and color_palette. Preserve the current physical setting across adjacent semantic beats: the same architecture, "
        "key props, lighting basis and palette remain recognizable. Change it only when the approved scene has location_transition=true; "
        "then visually establish the motivated arrival described in transition_note and keep that new setting continuous in following clips. "
        "Never invent an unapproved change of setting. Follow the planned camera direction. "
        + style_instruction
        +
        "No readable text, subtitles, logos, brands or watermarks.\n"
        "VIDEO: use each scene's generation_duration_sec exactly as planned for the Kling 3.0 image-to-video motion prompt in English. "
        "It must begin with the matching '<duration>-second Kling 3.0 image-to-video shot. No audio.' prefix "
        "and explicitly forbid dialogue, speech, lip sync, music, ambient sound, on-screen text, subtitles, logos and watermark. "
        f"{camera_instruction}"
        "Animate only the approved visual beat; preserve the start-frame character identity, age, era, objects and composition. "
        "The clip itself is silent because one external ElevenLabs narration track is added after all clips are approved.\n"
        "If feedback is present, revise previous_prompts literally without changing scene count, order, generation_duration_sec or narration excerpts.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    from .directorial_notes import RULE
    prompt = RULE + prompt
    raw = gemini_generate_sync(prompt, model_profile=GEMINI_MODEL_PROFILE_CREATIVE)
    data = extract_json_object(raw)
    prompts = data.get("prompts") if isinstance(data.get("prompts"), list) else []
    enriched: list[dict[str, Any]] = []
    for index, scene in enumerate(safe_scenes_plan):
        generation_duration = float(scene.get("generation_duration_sec") or scene.get("duration_sec") or STORY_CLIP_DURATION_SEC)
        generation_duration = (float(max(3, min(15, math.ceil(generation_duration)))) if scene.get(MODE_KEY) == ARCHIVE
                               else (4.0 if int(round(generation_duration)) >= 4 else 3.0))
        required_prefix = f"{int(generation_duration)}-second Kling 3.0 image-to-video shot. No audio."
        candidate = prompts[index] if index < len(prompts) and isinstance(prompts[index], dict) else {}
        start_prompt = (
            _fallback_start_prompt(scene)
            if reference_style_only
            else str(candidate.get("start_frame_prompt_ru") or "").strip() or _fallback_start_prompt(scene)
        )
        start_prompt = _ensure_story_environment_in_start_prompt(start_prompt, scene)
        # Historical motion is compiled from the accepted action and camera; a second
        # free-form camera interpretation can contradict the approved blocking.
        video_prompt = (_fallback_video_prompt(scene) if scene.get(MODE_KEY) == ARCHIVE else
                        str(candidate.get("video_prompt_en") or "").strip() or _fallback_video_prompt(scene))
        if not video_prompt.lower().startswith(required_prefix.lower()):
            video_prompt = _fallback_video_prompt(scene)
        short_format = SHORT_FORMATS.get(scene.get(MODE_KEY))
        if short_format:
            # The model may omit production constraints even when its JSON is valid.
            start_prompt = f"{start_prompt} {short_format.frame}"
            from .discipline_craft import MOTION_RULES
            video_prompt = f"{video_prompt} {short_format.motion} {MOTION_RULES}"
        video_prompt = _ensure_story_camera_direction(
            video_prompt,
            scene.get("camera_direction"),
            duration_sec=generation_duration,
        )
        video_prompt = ensure_story_video_camera_safety(video_prompt, allow_reveal=bool(scene.get("story_camera_reveal_requested")))
        excerpt = str(scene.get("narration_excerpt") or scene.get("text") or "").strip()
        scene_out = dict(scene)
        scene_out.update(
            {
                "scene": index + 1,
                "duration_sec": generation_duration,
                "generation_duration_sec": generation_duration,
                "model": "kling_3_0",
                "generation_mode": "story",
                "aspect_ratio": "9:16",
                "start_frame_prompt": start_prompt,
                "end_frame_prompt": "пусто",
                "end_frame_requested": False,
                "video_prompt": video_prompt,
                "video_prompt_by_lang": {"ru": video_prompt},
                "speech_by_lang": {"ru": excerpt},
                "speech_plan_by_lang": {"ru": []},
                "speech_cues_by_lang": {
                    "ru": ([{"start_sec": 0.0, "end_sec": generation_duration, "text": excerpt}] if excerpt else [])
                },
                "speech_budget_by_lang": {},
                "video_results_by_lang": {},
                "video_approvals_by_lang": {"ru": False},
                "voice_provider": "elevenlabs",
                "tts": {"provider": "elevenlabs", "mode": "external_story_master", "text_ru": excerpt},
            }
        )
        enriched.append(scene_out)
    return {"json_plan": enriched}
