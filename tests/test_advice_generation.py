import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.generation_modes import (
    normalize_generation_mode, target_languages_for_generation_mode,
    aspect_ratio_for_generation_mode, syntax_provider_for_scene_generation, SYNTAX_PROVIDER_VEO,
)
from app.handlers import handlers_state_start as start
from app.pipeline_llm_steps import build_advice_full_prompt


def test_advice_uses_normal_video_provider_and_configured_languages():
    assert normalize_generation_mode('Совет') == 'advice'
    assert target_languages_for_generation_mode({'generation_mode': 'advice'}, ['ru', 'en']) == ['ru', 'en']
    assert target_languages_for_generation_mode('advice', ['ru']) == ['ru']
    assert target_languages_for_generation_mode('advice', ['ru', 'en', 'es']) == ['ru', 'en', 'es']
    assert aspect_ratio_for_generation_mode('advice') == '9:16'
    assert syntax_provider_for_scene_generation({}, 'advice') == SYNTAX_PROVIDER_VEO


def test_advice_direct_recipe_still_offers_questions_and_keeps_choice(monkeypatch):
    monkeypatch.setattr(start, 'target_languages_from_env', lambda: ['ru', 'en'])
    sessions = {987654: {'generation_mode': 'advice', 'target_languages': ['ru', 'en']}}
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
    assert session['target_languages'] == ['ru', 'en']
    assert 'вопросов-проблем' in concepts.call_args.args[0]
    assert 'Не разбивай такой исходник на отдельный концепт' in concepts.call_args.args[0]
    advice = AsyncMock(return_value='Мам, как сохранить цвет борща? Обжарь свёклу отдельно.')
    recipe = AsyncMock(return_value='Не должен вызываться')
    monkeypatch.setattr(start, 'generate_advice_full_text', advice)
    monkeypatch.setattr(start, 'generate_recipe_full_text', recipe)
    asyncio.run(start._handle_waiting_concept_state(update, None, chat_id=987654, session=session, text='1'))
    assert question in session['selected_concept']
    assert 'speaker=daughter' in session['selected_concept']
    assert advice.await_args.args[1] == 'Свёкла, капуста. Обжарить свёклу, сварить борщ.'
    assert recipe.await_count == 0
    assert session['recipe_full_text'].startswith('Мам, как сохранить цвет борща?')
    assert session['state'] == start.STATE_WAITING_RECIPE_APPROVAL


def test_advice_refinement_keeps_full_source_and_does_not_use_recipe_generator(monkeypatch):
    session = {
        'generation_mode': 'advice',
        'state': start.STATE_WAITING_RECIPE_APPROVAL,
        'user_input': 'Суп: ложка соли. Рис: половина ложки. Картошку солить в конце.',
        'selected_concept': 'Мам, сколько соли нужно?',
        'selected_concept_name': 'Как солить разные блюда',
        'recipe_full_text': 'Старый текст только про рис.',
    }
    advice = AsyncMock(return_value='Суп: ложка соли. Рис: половина ложки. Картошку соли в конце.')
    recipe = AsyncMock(return_value='Не должен вызываться')
    monkeypatch.setattr(start, 'generate_advice_full_text', advice)
    monkeypatch.setattr(start, 'generate_recipe_full_text', recipe)

    async def call(update, context, chat_id, name, fn):
        return await fn()

    monkeypatch.setattr(start, '_call_with_503_retries', call)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)
    feedback = 'Оставь советы для всех блюд, не делай рецепт риса.'
    asyncio.run(start._handle_waiting_recipe_approval_state(
        update, None, chat_id=123, session=session, text=feedback,
    ))

    assert advice.await_args.args[:2] == (
        'Мам, сколько соли нужно?',
        'Суп: ложка соли. Рис: половина ложки. Картошку солить в конце.',
    )
    assert advice.await_args.kwargs['user_feedback'] == feedback
    assert advice.await_args.kwargs['previous_text'] == 'Старый текст только про рис.'
    assert recipe.await_count == 0
    assert session['recipe_full_text'].startswith('Суп:')
    assert session['state'] == start.STATE_WAITING_RECIPE_APPROVAL
    sent = '\n'.join(str(call.args[0]) for call in message.reply_text.await_args_list)
    assert 'ТЕКСТ СОВЕТА' in sent
    assert 'Если текст совета подходит' in sent


def test_normal_concept_still_uses_recipe_generator(monkeypatch):
    session = {
        'generation_mode': 'normal',
        'state': start.STATE_WAITING_CONCEPT,
        'user_input': 'Свёкла, капуста. Сварить борщ.',
        'concepts': [{'name': 'Борщ', 'hook': 'Яркий борщ', 'why': 'Сохраняем цвет'}],
    }
    recipe = AsyncMock(return_value='Свёкла, капуста. Сварить борщ.')
    advice = AsyncMock(return_value='Не должен вызываться')
    monkeypatch.setattr(start, 'generate_recipe_full_text', recipe)
    monkeypatch.setattr(start, 'generate_advice_full_text', advice)

    async def call(update, context, chat_id, name, fn):
        return await fn()

    monkeypatch.setattr(start, '_call_with_503_retries', call)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(start._handle_waiting_concept_state(
        update, None, chat_id=321, session=session, text='1',
    ))

    assert recipe.await_count == 1
    assert advice.await_count == 0
    assert session['recipe_full_text'].startswith('Свёкла')


def test_advice_prompt_preserves_every_tip_in_multi_product_source():
    prompt = build_advice_full_prompt(
        'Мама, сколько это соли?',
        'Суп: ложка на литр. Рис: половина ложки. Мясо: ложка на килограмм.',
        'Это советы, а не рецепт.',
        'Старый текст только про рис.',
    )
    assert 'текст совета, а не рецепт блюда' in prompt
    assert 'сохрани их все в исходном порядке' in prompt
    assert 'не сужай текст до одного блюда' in prompt
    assert 'Суп: ложка на литр' in prompt
    assert 'Мясо: ложка на килограмм' in prompt


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
    assert 'сохрани каждый совет' in captured['generation_comment']
    assert 'не превращай весь ролик' in captured['generation_comment']
    assert 'непонятно сколько' in captured['generation_comment']


def test_advice_not_enabled_for_other_projects():
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    session = {start._PROJECT_KEY_SESSION: 'discipline', 'generation_mode': 'normal'}
    asyncio.run(start._handle_waiting_generation_mode_state(
        update, None, chat_id=987654, session=session, text='Совет',
    ))
    assert session['generation_mode'] == 'normal'
    assert 'проект' in update.message.reply_text.call_args.args[0]


def test_advice_production_translates_both_speakers_and_following_scene(monkeypatch):
    import json
    from app import pipeline

    monkeypatch.setattr(pipeline, 'gemini_generate_sync', lambda *a, **kw: json.dumps({'prompts': [
        {'video_prompt_en': 'The daughter asks her mother.'},
        {'video_prompt_en': 'Grandma slices the chicken.'},
    ]}))
    translations = {'Мам, почему сухо?': 'Mom, why is it dry?',
                    'Сейчас покажу!': 'Let me show you!', 'Режем курицу.': 'Slice the chicken.'}
    async def translate(text, language):
        return translations.get(text, text) if language == 'en' else text
    monkeypatch.setattr(pipeline, '_translate_scene_text', translate)
    scenes = [
        {'scene': 1, 'model': 'veo3_fast', 'duration_sec': 8, 'text': 'Мам, почему сухо? Сейчас покажу!',
         'characters': ['daughter', 'grandma', 'cat'], 'dialogue_mode': 'multi',
         'dialogue': [{'speaker': 'daughter', 'text': 'Мам, почему сухо?'},
                      {'speaker': 'grandma', 'text': 'Сейчас покажу!'}]},
        {'scene': 2, 'model': 'veo3_fast', 'duration_sec': 8, 'text': 'Режем курицу.', 'characters': ['grandma']},
    ]
    frames = [{'start_frame_prompt_ru': 'Бабушка на кухне', 'end_frame_prompt_ru': 'пусто'}] * 2
    result = asyncio.run(pipeline.generate_full_production(scenes, frames, generation_mode='advice', target_languages=['ru', 'en']))
    first, second = result['json_plan']
    assert [x['speaker'] for x in first['dialogue_by_lang']['en']] == ['daughter', 'grandma']
    assert first['dialogue_by_lang']['en'][0]['text'] == 'Mom, why is it dry?'
    assert 'Let me show you!' in first['video_prompt_by_lang']['en']
    assert 'English language' in first['video_prompt_by_lang']['en']
    assert 'Russian language' not in first['video_prompt_by_lang']['en']
    assert second['speech_by_lang']['en'] == 'Slice the chicken.'
    assert set(second['video_prompt_by_lang']) == {'ru', 'en'}
