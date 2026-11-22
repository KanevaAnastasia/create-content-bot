"""Editorial constraints and text generation for the grandma advice format."""

from . import pipeline_llm_steps as llm_steps

ADVICE_CONCEPT_RULE = (
    "Формат «Совет» для русской страницы бабушки. Предложи 3–5 разных вопросов-проблем "
    "от взрослой дочери к маме по исходному материалу. В name оставь короткую тему совета, "
    "в hook запиши только точную короткую реплику дочери с обращением Мам/Мама/Ма, "
    "в why объясни конкретный способ решения. Например: «Мам, почему у меня курица сухая, "
    "а у тебя сочная?» Если дан один рецепт, не предлагай другие блюда, вопросы должны решаться "
    "его приёмами. Если исходник содержит список советов для нескольких блюд или продуктов, "
    "каждый вариант вопроса должен охватывать общую проблему всего списка. Не разбивай такой "
    "исходник на отдельный концепт для каждого продукта. Пользователь выбирает вопрос для первой "
    "сцены, а не сокращённую часть исходного материала."
)

ADVICE_SCENE_RULE = (
    "ФОРМАТ СОВЕТ: исходный сценарий на русском, весь ролик решает выбранную в concept проблему дочери. "
    "Для каждой выбранной языковой версии переводи реплики, сохраняя роли, смысл и порядок диалога. "
    "Первая сцена обязательно dialogue_mode=multi: сначала speaker=daughter произносит "
    "выбранный вопрос дословно, затем speaker=grandma коротко и естественно отвечает "
    "в этой же сцене, например «С курицей всё просто, сейчас покажу». "
    "characters первой сцены включают daughter, grandma, cat. Дочь взрослая, не внучка. "
    "text объединяет обе реплики в порядке произнесения. Диалог должен помещаться в длительность "
    "обычной сцены: ответ короткий, не разделяй вопрос и ответ на разные клипы. "
    "Далее бабушка наглядно показывает причины проблемы и конкретные приёмы её решения "
    "по исходному материалу. Если исходный текст содержит несколько самостоятельных советов, "
    "сохрани каждый совет с его продуктом, количеством, сроком и оговоркой; не превращай весь "
    "ролик в приготовление только одного блюда из вопроса. В финале заверши ответ на вопрос. "
    "Замечание пользователя «непонятно сколько», «непонятно когда» или «непонятно как» является "
    "обязательным условием приёмки: дай в произносимом тексте сцены конкретное количество, момент "
    "или способ, а не повторяй прежнюю расплывчатую формулировку. "
    "Хэштеги и служебные подписи из исходника не превращай в речь персонажей. "
    "Этот состав первой сцены имеет приоритет над автоматическим family_cast_plan "
    "и альтернативными хуками viral brief. Не заменяй выбранный вопрос новым хуком. "
    "Сохраняй обычную длительность, визуальный стиль и производственный процесс."
)


def advice_concept_input(source: str, mode: str) -> str:
    return f"{source}\n\n{ADVICE_CONCEPT_RULE}" if mode == "advice" else source


def format_advice_choices(concepts: list[dict]) -> str:
    return "Выберите вопрос дочери для первого клипа:\n\n" + "\n\n".join(
        f"{index}. {item.get('hook', '')}\nРешение: {item.get('why', '')}"
        for index, item in enumerate(concepts, 1)
    )


def format_advice_for_approval(advice_text: str) -> str:
    body = (advice_text or "").strip() or "—"
    return f"ТЕКСТ СОВЕТА:\n\n{body}"


def _strip_code_fence(text: str) -> str:
    result = (text or "").strip()
    if not result.startswith("```"):
        return result
    lines = result.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def generate_advice_full_text(
    selected_concept: str,
    source_text: str,
    *,
    user_feedback: str | None = None,
    previous_text: str | None = None,
) -> str:
    """Generate the approved advice script without converting it into a recipe."""
    concept = (selected_concept or "").strip()
    source = (source_text or "").strip()
    if not concept:
        raise ValueError("Empty advice concept.")
    if not source:
        raise ValueError("Empty advice source text.")
    prompt = llm_steps.build_advice_full_prompt(
        concept,
        source,
        (user_feedback or "").strip(),
        (previous_text or "").strip(),
    )
    text = _strip_code_fence(await llm_steps.gemini_text(prompt))
    if not text:
        raise RuntimeError("Модель не вернула текст совета.")
    return text
