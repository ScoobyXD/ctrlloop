# Browser workflow

The caller supplies an original request, starting HTTP(S) URL, optional exact text and iteration bound. One invocation owns one local headless Browser Use session and fresh profile.

1. Start the browser and navigate to the supplied URL.
2. Capture fresh `BrowserStateSummary` without a screenshot. Use its native filtered selector map, including every offered entry.
3. Build existing `Candidate` objects. Compact descriptions contain observed labels, roles, selected attributes, state and ancestor context. Local records retain execution identity; original nodes remain in Browser Use.
4. Run the existing `ChoiceSelector` in a worker thread. Groups of up to 50 advance through batched rounds to one winner. Selection, speculative action and settling questions retain their wording/choices. The model remains `jev-1.13.0`; requests retain the 90,000-byte ceiling.
5. Dispatch one Browser Use click or input with prepared text and `clear=False`. Do not refresh the selected map before dispatch. Stop on unavailable text, invalid identity or native action error.
6. Retain action result and previous-control context. If another iteration is allowed, wait the selected settling duration and observe again.
7. At the limit, return `iteration_limit` without claiming completion. Close the owned browser.

No Browser Use `Agent.run()` or planning LLM is used. Browser Use performs observation/execution, and our Jev selector owns decisions. No additional eligibility filtering is applied, so Browser Use's candidate-quality limitations remain visible in reports.

Prepared text is not generated or rewritten. It survives click-only iterations and is consumed once at typing dispatch. The current questions choose click versus type; other browser capabilities are not newly inferred decisions.

Parallel invocations share code/packages only. Each owns its profile, session, candidate map, history and reports. An index from one browser must never be dispatched in another.

The preserved desktop loop and its launch compatibility are described in [demo preservation](hermes-plugin.md).
