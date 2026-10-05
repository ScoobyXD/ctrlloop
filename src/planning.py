"""LLM planning into the existing action/step contract, with server-owned IDs."""

import json
from uuid import uuid4

from .actions import ACTION_TOOLS, normalize_action


PLAN_PROMPT = """Turn the user's job into a short, ordered sequence of executable computer actions.
Preserve the user's requested application, task, and wording. Return only the supplied JSON schema.
Rules:
- Each step performs exactly one click, type_text, press_key, or read action. Use the smallest useful sequence, at most 100 steps.
- target describes the application and intended visible control, so Jev can select it from fresh observed state. Do not invent coordinates, selectors, element refs, window IDs, or observations.
- Every action needs a specific target. Never rely on unspecified desktop focus. For type_text, include the exact text in text and use the same target in a preceding click step. Typing replaces the field's value.
- Preserve explicitly quoted typing text verbatim. For a search request, derive the literal search query from the user's requested subject. Do not type the whole instruction or add invented dates, facts, credentials, recipients, destinations, or answers.
- Set unused text and key fields to null. click/read need target; type_text needs target and text; press_key needs target and key.
- The worker already opens its configured browser at run start. Browser actions operate on webpage controls, not browser chrome. Use the page search field and submit button, not the browser address bar, tabs, or OS hotkeys. Prefer clicking a visible submit button over Enter in a browser; targeted browser key actions are unavailable.
- press_key is for a specifically targeted native application control when a key is needed. Do not use a key step to bypass missing page controls.
- Do not invent unsupported launch, navigate, shell, wait, verification, or conditional tools. Do not claim the task has completed. read reads a selected control's exposed text, not an entire unknown page or a future fact.
- These steps are a fixed plan, not a script with variables or branching. If the job requires unknown information, a missing credential, an unsupported capability, or decisions based on future content, return steps=[] and explain the missing requirement in blocked_reason. Do not return a partially executable plan.
- Human verification challenges require the user; do not generate steps to solve or bypass them.
- When the job is representable, blocked_reason=null and instruction is a concise summary of the requested job.
Example job: Search for hot snacks.
Steps: click the browser webpage's search input; type_text \"hot snacks\" into that same input; click the webpage's search submit button.
"""

# This constrains generation of existing actions; normalize_action remains the
# authoritative runtime validator shared with manual steps and the worker.
PLAN_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['instruction', 'steps', 'blocked_reason'],
    'properties': {
        'instruction': {'type': 'string'},
        'blocked_reason': {'type': ['string', 'null']},
        'steps': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['action'],
            'properties': {'action': {
                'type': 'object', 'additionalProperties': False,
                'required': ['tool', 'target', 'text', 'key'],
                'properties': {'tool': {'type': 'string', 'enum': list(ACTION_TOOLS)},
                               'target': {'type': ['string', 'null']},
                               'text': {'type': ['string', 'null']},
                               'key': {'type': ['string', 'null']}},
            }},
        }},
    },
}


class PlanningError(ValueError):
    """The complete job cannot be represented as executable existing actions."""


def planning_payload(prompt, model):
    return {'model': model, 'reasoning': {'effort': 'high'}, 'store': False,
            'instructions': PLAN_PROMPT, 'input': prompt, 'max_output_tokens': 6000,
            'text': {'format': {'type': 'json_schema', 'name': 'ctrlloop_steps',
                                'strict': True, 'schema': PLAN_SCHEMA}}}


def generated_steps(output):
    plan = json.loads(output)
    if not isinstance(plan, dict) or set(plan) != {'instruction', 'steps', 'blocked_reason'}:
        raise ValueError('Invalid plan envelope')
    instruction, steps, blocked = plan['instruction'], plan['steps'], plan['blocked_reason']
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 2000:
        raise ValueError('Invalid plan instruction')
    if not isinstance(steps, list) or len(steps) > 100:
        raise ValueError('Invalid plan steps')
    if blocked is not None:
        if not isinstance(blocked, str) or not blocked.strip() or len(blocked) > 2000 or steps:
            raise ValueError('Invalid blocked plan')
        raise PlanningError(blocked.strip())
    if not steps:
        raise ValueError('Plan has no executable steps')
    rows = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'action'}:
            raise ValueError('Invalid plan step')
        raw = step['action']
        if not isinstance(raw, dict) or set(raw) != {'tool', 'target', 'text', 'key'}:
            raise ValueError('Invalid generated action')
        action = normalize_action({k: v for k, v in raw.items() if v is not None})
        if not action.get('target'):
            raise ValueError('Generated actions must have an explicit target')
        if action['tool'] == 'type_text' and (not rows or rows[-1]['action'] != {'tool': 'click', 'target': action['target']}):
            raise ValueError('Generated typing must follow a click on the same target')
        rows.append({'id': str(uuid4()), 'action': action})
    return instruction.strip(), rows
