"""Authored Jev questions used throughout the action loop.

QUESTION is reused for every window and control selection group. ACTION and
hypothetical action timers share the final control round. TEXT_WORD evaluates
each original-request word in a separate text-selection round, packed into
parallel requests when the encoded body exceeds the shared byte budget. Code
types every word that meets the inclusion threshold in original request order.
The original user request and loop history are supplied as shared state.
"""

# Window and control selection share this question.
QUESTION = (
    "Which of these candidates is the next best step to serve the user's request."
)

ACTION = (
    "If this candidate control gets picked as the next action, should we click or type into it?"
)

TEXT_WORD = (
    "We are using a search bar, does this key word to serve the user's request?"
)

# Timer question
TIMER = (
    "How much time do you think it will take the app/browser to finish changing state?"
)
