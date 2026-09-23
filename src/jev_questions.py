"""The two authored Jev questions used throughout the action loop.

QUESTION is reused for every window and control selection group. TIMER is
batched once per finalist in the final control round, alongside selection.
The original user request and loop history are supplied as shared state.
"""

# Window and control selection share this question.
QUESTION = (
    "Which of these candidates is the next best step to serve the user's request."
)

# Timer question
TIMER = (
    "How much time do you think it will take the app/browser to finish changing state?"
)
