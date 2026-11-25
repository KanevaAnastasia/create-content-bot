"""Minimal request envelopes, retaining editorial rules and exact task text."""
import re

from .writing_style import HUMAN_WRITING_RULES, with_writing_style

OUTPUT_CONTRACT = (
    "Return only the requested deliverable. Do not echo the instructions or input, "
    "add a preface, explanation, duplicate summary, or unrequested JSON fields. "
    "For JSON, use compact valid JSON without markdown or indentation. "
    "Keep every required field, scene, ingredient, constraint and approved quotation; "
    "brevity must not omit required content."
)


def text_request(prompt, *, model, profile, images):
    task = with_writing_style(prompt)[len(HUMAN_WRITING_RULES) + 2:]
    rules = {'type': 'input_text', 'text': HUMAN_WRITING_RULES + '\n\n' + OUTPUT_CONTRACT}
    payload = {'model': model, 'store': False, 'input': [
        {'role': 'developer', 'content': [rules]},
        {'role': 'user', 'content': [{'type': 'input_text', 'text': task}, *images]},
    ]}
    # Only set generation-family-specific options on supported model IDs.
    if re.match(r'^gpt-(?:6(?:[.-])|5\.6(?:[.-]))', model):
        rules['prompt_cache_breakpoint'] = {'mode': 'explicit'}
        payload['prompt_cache_options'] = {'mode': 'explicit', 'ttl': '30m'}
        payload['text'] = {'verbosity': 'low'}
        if profile == 'short':
            payload['reasoning'] = {'effort': 'low'}
            payload['max_output_tokens'] = 8192
    return payload
