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
    assert 'WRITING_STYLE_V5' in updated
    assert with_writing_style(updated) == updated


def test_v3_saved_prompt_upgrades_without_rewriting_user_script():
    from app.writing_style import V3_HUMAN_WRITING_RULES
    payload = 'Approved voice: Сначала путался. Это — точная цитата. {"duration": 8}'
    updated = with_writing_style(V3_HUMAN_WRITING_RULES + '\n\n' + payload)
    assert updated == HUMAN_WRITING_RULES + '\n\n' + payload
    assert updated.count('[CONTENT_FABRIC_WRITING_STYLE_V5]') == 1
    assert with_writing_style(updated) == updated


def test_v4_saved_prompt_upgrades_without_touching_approved_words():
    from app.writing_style import V4_HUMAN_WRITING_RULES
    payload = 'Approved: "Это — точная цитата."\n{"video_prompt":"keep  spaces"}'
    assert with_writing_style(V4_HUMAN_WRITING_RULES + '\n\n' + payload) == HUMAN_WRITING_RULES + '\n\n' + payload
