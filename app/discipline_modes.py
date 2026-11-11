"""Narrative formats for Discipline; the existing Story pipeline handles media."""
import re

MODE_KEY = 'discipline_narrative_mode'
VASYA = 'about_vasya'
REFRAME = 'what_if'
MALE_VOICE_ID = 'P1bg08DkjqiVEzOn76yG'
LABELS = {VASYA: 'О Васе', REFRAME: 'А что если…'}


def active_mode(session):
    if session.get('selected_project_key') == 'discipline':
        value = session.get(MODE_KEY)
        return value if value in LABELS else ''
    return ''


def script_direction(mode):
    common = (
        'Это универсальный формат проекта «Дисциплина». Исходник может быть готовым текстом или проблемой '
        'о питании, чтении, работе, времени, энергии, привычках или любой другой повседневной теме. '
        'Развивай именно тему пользователя, не подставляй сюжет про понедельник автоматически. '
        'Раскрой узнаваемое поведение на конкретных примерах, затем причину и выполнимое решение: '
        'маленький шаг сейчас, пример его выполнения и пересмотр приоритетов, когда это уместно. '
        'Не стыди человека, не ставь диагнозы и не объявляй одну причину универсальной истиной. '
    )
    if mode == VASYA:
        return common + (
            'Начни дословно «Это Вася, и он…», продолжив предложение по теме без буквального многоточия. '
            'Это рассказ мужским голосом О Васе от третьего лица, а не монолог Васи. '
            'Покажи противоречие между его сегодняшним поведением и ожиданиями; связующий вопрос должен '
            'раскрыть это противоречие. Затем решение и конкретные небольшие действия. '
            'Title — короткий заголовок по теме.'
        )
    if mode == REFRAME:
        return common + (
            'Первое предложение — короткая проблема от первого лица, например «У меня нет энергии», '
            '«Я ленивый», «У меня нет времени». Верни title ТОЧНО равным первому предложению voiceover_text. '
            'Сначала раскрой проблему, затем отдельной фразой «А что если…» предложи возможное новое объяснение '
            '(перегрузка, неподходящая задача или приоритеты — по смыслу исходника), затем практическое решение. '
            'Не используй одно и то же объяснение для всех тем.'
        )
    return ''


def visual_direction(mode):
    if mode not in LABELS:
        return ''
    vasya_style = (
        'Формат «О Васе» делай как схематичную редакционную 2D-анимацию: плоские геометрические формы, '
        'чёткий тёмный контур, ограниченная благородная палитра и крупные понятные визуальные метафоры. '
        'Показывай привычки, выбор и прогресс через простые схемы, шкалы, карточки, стрелки и предметы в кадре; '
        'вся графика должна быть частью мира, без читаемого текста. Не используй фотореализм, 3D-рендер или живописную фактуру. '
    ) if mode == VASYA else ''
    return vasya_style + (
        'Видеоряд должен объяснять каждую мысль озвучки и удерживать внимание: конкретное действие, '
        'наглядное сравнение ожидания и реальности, изменение количества, шкалы и простые графические схемы '
        'без выдуманной статистики и без читаемого текста внутри генерации. Предметные и графические кадры допустимы. '
        'В каждом кадре присутствует утверждённый Василий, его внешность сохраняется по референсу проекта. '
        'Кнопка — постоянный персонаж рядом с Василием минимум в 60% кадров; сохраняй её референс. '
        'Кнопка действует естественно по смыслу сцены, без случайных прыжков. '
        'В предметных вставках, видах двора и ленты сохрани Василия в композиции: плечо, профиль, отражение или передний план. '
        'Не создавай другого героя вместо Василия. Первое лицо не требует Анастасию с книгами. '
        'Первый кадр уже содержит действие; без пустого вступления. '
        + ('Первые слова и «А что если?» (EN: «What if?») оформляются крупными субтитрами только на финальном рендере по таймингу речи. '
           'На фразе-повороте видеоряд и звук продолжаются: не создавай ради титра отдельную сцену, стоп-кадр, склейку или музыкальную паузу. '
           'Все остальные субтитры стандартные. '
           if mode == REFRAME else '')
        + 'Финал — реалистичный первый шаг или прогресс по озвучке; не приписывай мгновенное решение всей проблемы.'
    )


def opening_sentence(text):
    return re.split(r'(?<=[.!?…])\s+', str(text).strip(), maxsplit=1)[0]


def title_word_timing(title, text, duration, alignment):
    """Only animate against real alignment when the title is the spoken opening."""
    from .story_generation import _word_timing_rows
    clean = lambda s: re.sub(r'[^\w]+', '', s).casefold()
    tokens = title.split()
    if not alignment or not tokens:
        return []
    rows = _word_timing_rows(text, duration, alignment)
    if len(rows) < len(tokens) or [clean(w) for w in tokens] != [clean(r[0]) for r in rows[:len(tokens)]]:
        return []
    return [{'text': word, 'start': start, 'end': end} for word, start, end in rows[:len(tokens)]]


def reframe_title_cues(text, duration, alignment, language='ru'):
    """Locate the spoken turn using real alignment; never guess its timestamp."""
    from .story_generation import _word_timing_rows
    if not alignment:
        return []
    phrase = ['What', 'if?'] if language == 'en' else ['А', 'что', 'если?']
    clean = lambda value: re.sub(r'[^\w]+', '', value).casefold()
    rows = _word_timing_rows(text, duration, alignment)
    target = list(map(clean, phrase))
    for i in range(len(rows) - len(phrase) + 1):
        match = rows[i:i+len(phrase)]
        if [clean(row[0]) for row in match] != target:
            continue
        timing = [{'text': word, 'start': row[1], 'end': row[2]} for word, row in zip(phrase, match)]
        return [{'text': ' '.join(phrase), 'language': language,
                 'start_sec': timing[0]['start'], 'end_sec': min(duration, timing[-1]['end'] + .6),
                 'word_timing': timing}]
    return []
