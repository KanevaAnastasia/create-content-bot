from app.writing_style import HUMAN_WRITING_RULES, with_writing_style


def test_saved_prompt_and_exact_source_survive_wrapping():
    prompt = 'Return JSON: {"speech": "Это не суп, а борщ!", "duration": 8}\nКопируй дословно.'
    wrapped = with_writing_style(prompt)
    assert wrapped == HUMAN_WRITING_RULES + "\n\n" + prompt
    assert with_writing_style(wrapped) == wrapped


def test_marker_in_user_material_does_not_disable_guidance():
    prompt = 'Source mentions [CONTENT_FABRIC_WRITING_STYLE_V1] but contains no rules.'
    assert with_writing_style(prompt).startswith(HUMAN_WRITING_RULES + "\n\n")


def test_previous_rules_upgrade_once_without_touching_payload():
    from app.writing_style import PREVIOUS_HUMAN_WRITING_RULES
    payload = 'Preserve approved: Это — цитата. image_prompt: 3D style.'
    updated = with_writing_style(PREVIOUS_HUMAN_WRITING_RULES + '\n\n' + payload)
    assert updated == HUMAN_WRITING_RULES + '\n\n' + payload
    assert 'WRITING_STYLE_V3' in updated
    assert with_writing_style(updated) == updated
