"""Authored Jev questions used throughout the action loop.

QUESTION is reused for every window and control selection group. ACTION and
hypothetical action timers share the final control round. The original user
request and loop history are supplied as shared state.
"""

# Window and control selection share this question.
QUESTION = (
    "Which of these candidates is the next best step to serve the user's request."
)

ACTION = (
    "If this candidate control gets picked as the next action, should we click or type into it?"
)

# Timer question
TIMER = (
    "How much time do you think it will take the app/browser to finish changing state?"
)
