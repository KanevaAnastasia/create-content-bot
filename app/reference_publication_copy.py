"""Publication copy for reference videos, driven by the user's editorial idea."""
import asyncio
import json

from .llm_utils import gemini_generate_sync, extract_json_object, GEMINI_MODEL_PROFILE_CREATIVE
from .publication_copy_store import save_bot_publication_copy
from .reference_generation import STATE_WAITING_REFERENCE_DESCRIPTION_APPROVAL
from .writing_style import with_writing_style

FIELDS = (
    'youtube_ru_title', 'youtube_ru_description', 'instagram_ru', 'vk_ru',
    'tiktok_ru', 'instagram_en', 'youtube_en_title', 'youtube_en_description',
)
COPY_KEY = 'reference_publication_copy'
FEEDBACK_KEY = 'reference_description_feedback'


def generate_copy(*, image_prompt: str, video_prompt: str, comment: str = '',
                  feedback: str = '', current: dict | None = None, project: str = '') -> dict[str, str]:
    prompt = with_writing_style(
        'Напиши публикационные тексты к короткому ролику для разных платформ. '
        'Они должны заинтересовать зрителя и дать повод посмотреть ролик. '
        'Не пересказывай движения персонажа, ракурс, одежду и техническое описание кадра. '
        'Фидбэк пользователя имеет высший редакционный приоритет: если он прислал мысль или фразу, '
        'это главная мысль новых описаний, а не добавка к старому пересказу. '
        'Передай эту мысль естественными словами, сохрани обращение к зрителю и смысл. '
        'Если пользователь дал инструкцию по правке, выполни её, не цитируй инструкцию в публикации. '
        'Комментарий к кадру используй только для понимания видео, не превращай его в подпись. '
        'Если фидбэка ещё нет, найди понятный зрителю смысл ситуации без выдуманных фактов. '
        'Не придумывай биографию героя, точное число лет, обещания результата или товар. '
        'Избегай пафоса, клише про успех, пустых призывов и механической замены слов синонимами. '
        'Каждый вариант должен быть понятен сам по себе. Не обязан называть имя героя. '
        'Пиши коротко, обычно от одного до трёх предложений, без искусственного лимита в одно предложение. '
        'YouTube: отдельный цепляющий заголовок и описание, которое развивает его мысль. '
        'Instagram: сильная первая строка, затем при необходимости короткое развитие. '
        'VK: самостоятельный небольшой текст с понятной мыслью. TikTok: самая короткая ёмкая формулировка. '
        'Английские YouTube и Instagram: естественная адаптация той же мысли для англоязычного читателя, '
        'не буквальный перевод и не новые факты. Различай варианты по подаче без смены смысла. '
        'Не добавляй хэштеги, эмодзи, рекламные обещания и просьбы подписаться без запроса. '
        'Не переносить персонажей или темы из других проектов. '
        'Верни строго JSON со строковыми непустыми значениями всех ключей: '
        + ', '.join(FIELDS)
        + '\n\nПРОЕКТ: ' + project
        + '\nМАТЕРИАЛЫ РОЛИКА:\n' + json.dumps({
            'image_prompt': image_prompt, 'video_prompt': video_prompt, 'comment': comment,
        }, ensure_ascii=False)
        + '\nТЕКУЩИЕ ВАРИАНТЫ:\n' + json.dumps(current or {}, ensure_ascii=False)
        + '\nФИДБЭК ПОЛЬЗОВАТЕЛЯ:\n' + (feedback or 'нет')
    )
    result = extract_json_object(gemini_generate_sync(prompt, model_profile=GEMINI_MODEL_PROFILE_CREATIVE))
    if any(not isinstance(result.get(key), str) or not result[key].strip() for key in FIELDS):
        raise ValueError('Не получен полный набор описаний для платформ. Повторите генерацию.')
    return {key: result[key].strip() for key in FIELDS}


async def prepare_copy(update, session: dict, *, feedback: str = '') -> None:
    # Keep a submitted brief even when the provider fails, so retry is faithful.
    if feedback.strip():
        previous = str(session.get(FEEDBACK_KEY) or '').strip()
        session[FEEDBACK_KEY] = '\n'.join(part for part in (previous, feedback.strip()) if part)
    session['reference_publication_copy_error'] = True
    session['reference_description_approved'] = False
    session['reference_description_feedback_pending'] = False
    session['state'] = STATE_WAITING_REFERENCE_DESCRIPTION_APPROVAL
    scenes = (session.get('production') or {}).get('json_plan') or []
    scene = scenes[0] if scenes else {}
    old = session.get(COPY_KEY) or {}
    await update.message.reply_text('Готовлю описания для платформ на русском и английском.')
    result = await asyncio.to_thread(
        generate_copy, image_prompt=str(scene.get('start_frame_prompt') or ''),
        video_prompt=str(scene.get('video_prompt') or ''),
        comment=str(session.get('reference_comment') or ''),
        feedback=str(session.get(FEEDBACK_KEY) or ''), current=old.get('copy') or {},
        project=str(session.get('selected_project_key') or ''),
    )
    pending = {
        'copy': result, 'origin': 'production_final',
        'project_slug': session.get('selected_project_key', ''),
        'recipe': str(scene.get('start_frame_prompt') or ''),
        'last_feedback': session.get(FEEDBACK_KEY, ''),
        'copy_revision_id': old.get('copy_revision_id'),
    }
    save_bot_publication_copy(session, pending)
    session['reference_publication_copy_error'] = False
    session[COPY_KEY] = pending
    session['reference_short_description'] = result['instagram_ru']
    from .handlers.handlers_state_media import _social_copy_messages, _send_copyable_message
    for title, body in _social_copy_messages(result):
        await _send_copyable_message(update, title, body)
