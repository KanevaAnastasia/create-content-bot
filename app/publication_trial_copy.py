"""Generate a distinct RU Trial caption for automatic Telegram copy bundles."""
from .publications import strip_instagram_ru_attribution


async def generate_trial_copy(*, recipe, normal_copy, feedback="", project_slug=None, refine=None):
    if not str(recipe or "").strip():
        raise ValueError("Для пробного описания нужен исходный текст.")
    if refine is None:
        from .handlers.handlers_state_media import _refine_social_copy_from_feedback
        refine = _refine_social_copy_from_feedback
    instruction = (
        "Создай отдельное НОВОЕ описание Instagram RU Trial Reel для новой аудитории: "
        "другой сильный хук, другая структура и формулировки, но тот же рецепт/сюжет. "
        "Сохрани факты и добавь релевантные хэштеги. Не упоминай Syntx или @syntx_ai. "
        "Верни новое описание в instagram_ru, остальные поля оставь без изменений. "
        + str(feedback or "")
    )
    normal = strip_instagram_ru_attribution(str(normal_copy.get("instagram_ru") or "")).casefold()
    for attempt in range(2):
        result = await refine(recipe=recipe, previous_copy=dict(normal_copy),
                              feedback=instruction + (" Полностью перепиши совпавший вариант." if attempt else ""),
                              project_slug=project_slug)
        trial = strip_instagram_ru_attribution(str(result.get("instagram_ru") or ""))
        if trial.strip() and trial.casefold() != normal:
            return trial
    raise ValueError("Пробное описание совпало с основным. Повторите генерацию.")
