"""Keep approved story prop files in sync with visual prompt edits."""
import re


def prop_patterns(prop_id, entry):
    patterns = [re.escape(entry['display_name'])] if entry.get('display_name') else []
    for alias in entry.get('aliases') or []:
        patterns.append(re.escape(alias))
    tokens = set(prop_id.lower().split('_'))
    if 'book' in tokens:
        patterns.append(r'\b(?:книг\w*|перепл[её]т\w*|book|hardcover)\b')
    if 'manuscript' in tokens:
        patterns.append(r'\b(?:рукопис\w*|manuscript)\b')
    if 'typewriter' in tokens:
        patterns.append(r'\b(?:машинк\w*|typewriter)\b')
    return patterns


def reconcile_story_prop_references(scene, *, prompt_keys=None, explicit_edit=False):
    catalog = scene.get('story_prop_reference_catalog')
    if not isinstance(catalog, dict) or scene.get('generation_mode') != 'story':
        return
    keys = prompt_keys or ('start_frame_prompt', 'start_frame_prompt_ru', 'end_frame_prompt', 'action')
    text = '\n'.join(str(scene.get(k) or '') for k in keys)
    # Remove descriptions of the medium, not qualifiers on a physical object
    # ("книга без надписей" must still attach the book).
    text = re.sub(r'(?:в\s+)?стил\w*\s+(?:иллюстрированн\w*\s+)?книг\w*|book\s+(?:illustration\s+)?style', '', text, flags=re.I)
    clauses = re.split(r'[.!?,;\n]+|\s+(?:и|а|но)\s+(?=(?:остав|добав|полож|убер|убра|remove|keep|add))', text, flags=re.I)
    ids = list(scene.get('story_prop_ids') or [])
    excluded = set(scene.get('story_excluded_prop_ids') or [])
    for prop_id, entry in catalog.items():
        if not isinstance(entry, dict):
            continue
        mentioned = False
        rejected = False
        for pattern in prop_patterns(prop_id, entry):
            for clause in clauses:
                for match in re.finditer(pattern, clause, re.I):
                    prefix, suffix = clause[:match.start()], clause[match.end():]
                    negative = (re.search(r'(?:без|нет|no|without)\s+(?:этой\s+|the\s+)?$', prefix, re.I)
                                or re.search(r'убери|убрать|не показыв|отсутств|remove', prefix, re.I)
                                or re.match(r'\s+(?:нет|отсутств)', suffix, re.I)
                                or re.search(r'^(?:\s+\w+){0,3}\s+(?:вне кадра|за (?:верхней|нижней|левой|правой) границей кадра)', suffix, re.I))
                    if negative:
                        rejected = True
                    else:
                        mentioned = True
        if explicit_edit:
            if mentioned:
                excluded.discard(prop_id)
            elif rejected:
                excluded.add(prop_id)
        if mentioned and prop_id not in ids and prop_id not in excluded:
            ids.append(prop_id)
    ids = [prop_id for prop_id in ids if prop_id not in excluded]
    refs = dict(scene.get('references') or {})
    required = list(scene.get('story_required_reference_ids') or [])
    descriptions = dict(scene.get('story_prop_descriptions') or {})
    for prop_id in excluded:
        refs.pop(prop_id, None)
        descriptions.pop(prop_id, None)
    required = [ref_id for ref_id in required if ref_id not in excluded]
    for prop_id in ids:
        entry = catalog.get(prop_id)
        if not isinstance(entry, dict):
            continue
        if not entry.get('approved') or not entry.get('path'):
            raise ValueError(f"Сначала утвердите референс предмета: {entry.get('display_name') or prop_id}.")
        if entry.get('style') != scene.get('story_visual_style_key'):
            raise ValueError(f"Стиль референса предмета {prop_id} изменился. Утвердите новый образ.")
        refs[prop_id] = entry['path']
        descriptions[prop_id] = entry.get('description', '')
        if prop_id not in required:
            required.append(prop_id)
    scene.update(story_prop_ids=ids, story_excluded_prop_ids=sorted(excluded), references=refs,
                 story_required_reference_ids=required, story_prop_descriptions=descriptions)
