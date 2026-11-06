import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.generation_modes import (
    normalize_generation_mode, target_languages_for_generation_mode,
    aspect_ratio_for_generation_mode, syntax_provider_for_scene_generation, SYNTAX_PROVIDER_VEO,
)
from app.handlers import handlers_state_start as start


def test_advice_uses_normal_video_provider_and_only_russian():
    assert normalize_generation_mode('Совет') == 'advice'
    assert target_languages_for_generation_mode({'generation_mode': 'advice'}, ['ru', 'en']) == ['ru']
    assert aspect_ratio_for_generation_mode('advice') == '9:16'
    assert syntax_provider_for_scene_generation({}, 'advice') == SYNTAX_PROVIDER_VEO


def test_advice_direct_recipe_still_offers_questions_and_keeps_choice(monkeypatch):
    sessions = {987654: {'generation_mode': 'advice', 'target_languages': ['ru']}}
    monkeypatch.setattr(start, 'SESSIONS', sessions)
    monkeypatch.setattr(start, '_try_parse_direct_recipe_from_user_text', lambda _: ['Обжарить свёклу'])
    question = 'Ма, как ты делаешь такой яркий борщ?'
    concepts = AsyncMock(return_value={'concepts': [{'name': 'Борщ', 'hook': question, 'why': 'Сохраняем цвет свёклы'}], 'text_plan': question})
    monkeypatch.setattr(start, 'generate_recipe_concepts', concepts)
    async def call(update, context, chat_id, name, fn):
        return await fn()
    monkeypatch.setattr(start, '_call_with_503_retries', call)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()), effective_chat=SimpleNamespace(id=987654))
    asyncio.run(start._continue_generation_with_recipe(update, None, recipe='Свёкла, капуста. Обжарить свёклу, сварить борщ.', prefer_vertex_api=False))
    session = sessions[987654]
    assert session['state'] == start.STATE_WAITING_CONCEPT
    assert session['target_languages'] == ['ru']
    assert 'вопросов-проблем' in concepts.call_args.args[0]
    recipe = AsyncMock(return_value='Свёкла, капуста. Обжарить свёклу, сварить борщ.')
    monkeypatch.setattr(start, 'generate_recipe_full_text', recipe)
    asyncio.run(start._handle_waiting_concept_state(update, None, chat_id=987654, session=session, text='1'))
    assert question in session['selected_concept']
    assert 'speaker=daughter' in session['selected_concept']
    assert 'Обжарить свёклу' in recipe.call_args.args[0]
    assert session['state'] == start.STATE_WAITING_RECIPE_APPROVAL


def test_advice_scene_request_overrides_viral_hook_and_keeps_question(monkeypatch):
    import pytest
    from app import pipeline
    captured = {}

    def capture(payload):
        captured.update(payload)
        raise ValueError('captured before LLM')

    monkeypatch.setattr(pipeline.llm_steps, 'build_scenes_plan_prompt', capture)
    with pytest.raises(ValueError, match='captured before LLM'):
        asyncio.run(pipeline.generate_scenes_plan(
            'Борщ. Ма, как ты делаешь такой яркий борщ?',
            full_recipe_text='Обжарить свёклу. Сварить борщ.',
            generation_mode='advice',
            viral_concept_brief={'strongest_first_phrase': 'Другой хук'},
        ))
    assert 'Ма, как ты делаешь такой яркий борщ?' in captured['concept']
    assert 'speaker=daughter' in captured['generation_comment']
    assert 'в этой же сцене' in captured['generation_comment']
    assert 'приоритет' in captured['generation_comment']


def test_advice_not_enabled_for_other_projects():
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session = {start._PROJECT_KEY_SESSION: 'discipline', 'generation_mode': 'normal'}
    asyncio.run(start._handle_waiting_generation_mode_state(
        update, None, chat_id=987654, session=session, text='Совет',
    ))
    assert session['generation_mode'] == 'normal'
    assert 'проект' in update.message.reply_text.call_args.args[0]
