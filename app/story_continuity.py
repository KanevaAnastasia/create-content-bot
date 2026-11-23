"""Persist one compact, immutable continuity manifest per accepted visual plan."""
import hashlib
import json
from pathlib import Path


def save_continuity_manifest(session):
    scenes = session.get('scenes_plan') or []
    refs = session.get('story_character_references') or {}
    payload = {
        'style': session.get('story_visual_style'),
        'assets': [{
            'id': item['id'], 'kind': item.get('entity_type', 'character'),
            'description': item.get('description', ''),
            'reference_prompt': item.get('reference_prompt', ''),
            'approved_path': (refs.get(item['id']) or {}).get('selected_path')
        } for item in session.get('story_character_bible') or [] if item.get('requires_reference')],
        'scenes': [{key: scene.get(key) for key in (
            'scene', 'characters', 'story_prop_ids', 'references', 'location',
            'lighting', 'color_palette', 'continuity_anchors', 'transition_note',
            'shot_type', 'camera_direction')}
            for scene in scenes],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(serialized.encode()).hexdigest()[:20]
    folder = Path(__file__).resolve().parents[1] / 'artifacts' / 'story_continuity'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{digest}.json'
    path.write_text(serialized, encoding='utf-8')
    for scene in scenes:
        scene['continuity_manifest_path'] = str(path)
    return str(path)
