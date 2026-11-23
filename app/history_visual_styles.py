"""Selectable history looks; preview screenshots never become generation references."""
HISTORY_STYLES = {
    "history_scratchboard": {"label": "Процарапанная графика", "preview_path": "assets/story/history_styles/board_6_10.png", "description": "Светлые процарапанные штрихи на тёмном фоне. Пример № 8 на листе.", "prompt": "Scratchboard illustration, sharp white incised hatching on deep ink-blue, expressive silhouettes, fixed scratch texture."},
    "history_woodcut": {"label": "Древесная графика", "preview_path": "assets/story/history_styles/board_11_15.png", "description": "Плоские цветовые формы и резная фактура. Пример № 13 на листе.", "prompt": "Flat woodcut illustration, carved contours, restrained ochre teal and charcoal palette, wood grain, angular scenery."},
    "history_comic": {"label": "Печатный комикс", "preview_path": "assets/story/history_styles/board_1_5.png", "description": "Контрастный контур и печатный растр. Пример № 3 на листе.", "prompt": "Printed comic illustration, bold ink contours, restrained halftone dot screens, expressive faces, clear cinematic staging."},
    "history_charcoal": {"label": "Уголь", "preview_path": "assets/story/history_styles/board_1_5.png", "description": "Угольный рисунок с мягкими растушёвками. Пример № 4 на листе.", "prompt": "Charcoal on rough paper, hand drawn monochrome marks, soft rubbed shading and eraser traces, stable recognizable faces."},
    "history_yarn_puppet": {"label": "Кукольная миниатюра с пряжей", "preview_path": "assets/story/history_styles/puppet.jpg", "description": "Кукольные пропорции, пряжа и ткань, мягкий свет.", "prompt": "Expressive stop-motion storybook puppet, strongly caricatured proportions: oversized head, very large glossy eyes with visible eyelids, slim long neck, small narrow shoulders and slender body. Smooth matte painted clay/resin skin with subtle blush, NOT knitted skin, NOT woven facial texture. Hair is made of thick distinct wool strands, while clothes are sewn from lightweight real fabric, NOT an all-knit doll. Exaggerate the person's distinctive nose, brows and face silhouette within these puppet proportions. Warm cinematic side light, soft shallow-focus miniature setting. Not a realistic human bust, not a photoreal face, not a crochet or amigurumi figure."},
    "history_fairytale_book": {"label": "Сказочная книжная иллюстрация", "preview_path": "assets/story/history_styles/storybook.jpg", "description": "Мягкая книжная живопись и глубокое сказочное пространство.", "prompt": "Delicate storybook gouache and watercolor illustration, muted luminous palette, tall slender architecture, soft atmospheric depth, expressive stylized characters. Include close views when narratively needed."},
    "history_vintage_editorial": {"label": "Винтажная журнальная иллюстрация", "preview_path": "assets/story/history_styles/vintage.jpg", "description": "Выразительные лица, бумажная фактура и крупные цветовые формы.", "prompt": "Vintage editorial fashion illustration, expressive stylized faces and elegant elongated proportions, gouache dry brush on paper, clear color shapes, rich cloth texture, cinematic city or interior backgrounds. Preserve approved character identities."},
}
for _style in HISTORY_STYLES.values():
    _style["reference_path"] = ""
    _style["prompt"] += " Full bleed artwork. No interface, text, signatures or watermarks. Depict the actual historical people with recognizable individual facial features at the age of the events, using their manually approved stylized references. Never substitute project mascots or copy the demo people. Keep one material, palette and character model across all shots."

# Props share the miniature world, but must not inherit face/eye instructions.
HISTORY_STYLES["history_yarn_puppet"]["prop_prompt"] = (
    "Handcrafted prop from an expressive storybook stop-motion puppet set. "
    "Sculpted painted wood or matte resin miniature, simplified chunky shapes, softly rounded edges, "
    "slightly exaggerated large functional details, subtle handmade irregularity. "
    "Warm soft light, miniature scale. The object must look made for a puppet character, "
    "not a photograph of a real manufactured object, not a museum replica or product catalogue render. "
    "No realistic machining, metallic micro-scratches or intricate tiny mechanisms. "
    "Preserve function and silhouette; use tactile paper for paper props and sewn cloth only where appropriate. "
    "Do not make every object knitted. No people, faces, eyes, hands, letters or logos. "
    "Selected miniature style takes priority over photoreal material wording in the object description."
)
