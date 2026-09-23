Purpose:
We are making a computer use agent using Jev.


General Architecture:
1. Get user question -> Read Windows accessibility tree → Jev narrows candidates → Jev selects a control and action → our code maps that selection to a CUA action → observe the result → repeat
2. Modularity. Every part of our workflow here should be swappable or replacable with another tool that can do the same thing but in a better way. BThis is a fast moving space and developers and companies are making different parts of the stack right now. So whenever something comes up thats better we just swap what we have with theirs.
3. Do not make redundant api's and don't make up new data models if one already exists. I can not emphasize this enough, do not make near-identical data models or api's for the same thing.
4. Have a unified set of backend and and frontend apis, this rule extends from rule #3 of not having near-identical apis or data structures.
5. We are not making any tests right now.

User Preferences:
1. When I ask you to create a new git worktree, I always mean to create a new git worktree in a brand new codex instance. And make sure to number them, like # <worktree name>
2. When I throw a json report to you, I want you to break down the series of questions and probabilities Jev gave in the question and what it ultimately chose. And include the different rounds of questions as well.
    a. Be consistent with the following vocabulary: Rounds meaning if it was a window selection round or a control selection of that selected window round, and of course state how many of those rounds we went through during the window/control selection. Candidates are how many options there were to choose from, whether its the window or control selection. 10 candidates are placed into 1 group, each group being asked a specific question. All the groups in that entire round is sent in 1 api call. And of course, I want a count of all the API calls that went out and how long each one took.
    b. Make sure to state how long each stage or round took, its incredibly important that I understand how long the the total run and the stages inside that run took.
    c. I also want a timeline view which has the current time up to this point, what was the operation that happened like Jev window round 1 or Cua control search, and how much time this  operation took. Within this timeline view I want to see visibly where each loop started and ended, what number loop we are on (dont make it its own column) and see the grand total time elapsed up to this point and how much time this loop took (this will be its own row). 
    d. Loop definition: loop starts when we get current state and ends when the last cua computer use command gets sent (right before getting the next state)

Jev rules:
1. Don't put 1 question per call. Bunch them up.
2. Keep the state input sent to Jev minimal
3. Don't ask arithmetic/date questions to Jev
4. Pin the Jev model we are doing, the configurations and tuning we're doing for this Jev model may not carry over to the next
5. Nouls are supposed to map into if statements, choice to switchable enums, and score to some threshold.