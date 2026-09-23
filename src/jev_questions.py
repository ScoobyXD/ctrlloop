"""Authored Jev questions used throughout the action loop.

QUESTION is reused for every window and control selection group. ACTION and
hypothetical action timers share the final control round. TEXT_WORD evaluates
each original-request word in a separate text-selection round, packed into
parallel requests when the encoded body exceeds the shared byte budget.
The original user request and loop history are supplied as shared state.
"""

# Window and control selection share this question.
QUESTION = (
    "Which of these candidates is the next best step to serve the user's request."
)

ACTION = (
    "If this candidate is selected as the next control, should the next action "
    "click it or type text into it to serve the user's request? Use the observed "
    "role, label, value, enabled state, and capabilities together; a text-like "
    "role alone does not establish that a control is writable. Choose type_text "
    "when the next step is entering text into this control, and click when the "
    "next step is activating or focusing it without entering text. type_text "
    "inserts or appends without clearing the existing value. Do not repeat the "
    "previous typing unless the request requires another insertion."
)

TEXT_WORD = (
    "Should this specific occurrence of a word from original_request be included "
    "in the exact text to type into text_target now? Include only the requested "
    "content for this destination, not surrounding instructions or content meant "
    "for another destination. Judge this indexed occurrence in the full request. "
    "The code copies one contiguous source span verbatim; it cannot rewrite, "
    "combine separated spans, invent a URL, or remove punctuation from a word."
)

# Timer question
TIMER = (
    "How much time do you think it will take the app/browser to finish changing state?"
)
