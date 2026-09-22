Purpose:
We are making a computer use agent using Jev.


General Architecture:
1. Get user question -> Read Windows accessibility tree → Jev narrows candidates → Jev selects a control and action → our code maps that selection to a CUA action → observe the result → repeat
2. Modularity. Every part of our workflow here should be swappable or replacable with another tool that can do the same thing but in a better way. BThis is a fast moving space and developers and companies are making different parts of the stack right now. So whenever something comes up thats better we just swap what we have with theirs.
3. Do not make redundant api's and don't make up new data models if one already exists. I can not emphasize this enough, do not make near-identical data models or api's for the same thing.
4. Have a unified set of backend and and frontend apis, this rule extends from rule #3 of not having near-identical apis or data structures.

User Preferences:
1. When I ask you to create a new git worktree, I always mean to create a new git worktree in a brand new codex instance. And make sure to number them, like # <worktree name>
2. When I throw a json report to you, I want you to break down the series of questions and probabilities Jev gave in the question and what it ultimately chose. And include the different rounds of questions as well.

Jev rules:
1. Don't put 1 question per call. Bunch them up.
2. Keep the state input sent to Jev minimal
3. Don't ask arithmetic/date questions to Jev
4. Pin the Jev model we are doing, the configurations and tuning we're doing for this Jev model may not carry over to the next