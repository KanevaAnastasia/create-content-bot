from app.writing_style import HUMAN_WRITING_RULES, with_writing_style


def test_saved_prompt_and_exact_source_survive_wrapping():
    prompt = 'Return JSON: {"speech": "Это не суп, а борщ!", "duration": 8}\nКопируй дословно.'
    wrapped = with_writing_style(prompt)
    assert wrapped == HUMAN_WRITING_RULES + "\n\n" + prompt
    assert with_writing_style(wrapped) == wrapped


def test_marker_in_user_material_does_not_disable_guidance():
    prompt = 'Source mentions [CONTENT_FABRIC_WRITING_STYLE_V1] but contains no rules.'
    assert with_writing_style(prompt).startswith(HUMAN_WRITING_RULES + "\n\n")
