"""Keep parenthetical visual direction separate from spoken narration."""
import re

SESSION_KEY = 'directorial_notes'
RULE = (
    'РЕЖИССЁРСКИЕ РЕМАРКИ: пояснения пользователя в круглых скобках () и квадратных [] '
    'не произносятся голосом и не становятся субтитрами. Они обязательны для действия, камеры, '
    'композиции, перехода или монтажного титра возле соответствующей фразы. '
    'Сохраняй смысл и последовательность. Не размазывай локальную ремарку по всем сценам. '
    'Сложный пролёт через окно или экран разбей на согласованные кадры: подход/наезд, '
    'стык через заполнение кадра окном/экраном, раскрытие новой локации. Не заменяй его случайным зумом. '
    'Предусмотри содержание следующего кадра и совпадающие направление и композицию стыка. '
    'Читаемые титры добавляет финальный рендер, не рисуй буквы внутри генерации. '
    'Числа и пояснения о предметах в скобках учитывай как факты для визуала/рецепта. '
    '[пауза] и [pause] — отдельные управляющие маркеры речи, не визуальные ремарки.\n'
)


def split_notes(value):
    text = str(value or '')
    spoken, notes = [], []
    i = 0
    while i < len(text):
        opener = text[i]
        if opener not in '([':
            spoken.append(opener); i += 1; continue
        stack = [')' if opener == '(' else ']']
        j = i + 1
        while j < len(text) and stack:
            c = text[j]
            if c in '([': stack.append(')' if c == '(' else ']')
            elif c == stack[-1]: stack.pop()
            j += 1
        if stack:  # Incomplete user input is preserved instead of silently discarded.
            spoken.append(opener); i += 1; continue
        body = text[i+1:j-1].strip()
        if opener == '[' and body.casefold() in {'пауза', 'pause'}:
            spoken.append(text[i:j])
        else:
            if body:
                anchor = ''.join(spoken).strip()[-240:]
                notes.append({'after': anchor, 'direction': body})
            spoken.append(' ')
        i = j
    clean = re.sub(r'\s+([,.!?;:])', r'\1', ' '.join(''.join(spoken).split()))
    return clean, notes


def spoken_text(value):
    return split_notes(value)[0]


def remember_notes(session, text):
    notes = split_notes(text)[1]
    saved = list(session.get(SESSION_KEY) or [])
    for note in notes:
        if note not in saved: saved.append(note)
    if saved: session[SESSION_KEY] = saved


def direction_for_session(session):
    import json
    notes = list(session.get(SESSION_KEY) or [])
    for key in ('user_input', 'story_source_text', 'advertising_source_text'):
        for note in split_notes(session.get(key))[1]:
            if note not in notes: notes.append(note)
    return RULE + json.dumps(notes, ensure_ascii=False) if notes else ''
