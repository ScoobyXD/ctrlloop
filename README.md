# ctrlloop

Cua Driver observes an Omarchy cloud desktop, existing Jev rounds select a window and control, and Cua Driver performs one click or typing action before the next observation. Browser Use and its local headless-browser runtime have been removed.

The loop connects to an existing Fleet claim. It does not provision, renew, release, or delete workers. The preserved Windows Hermes/CUA demo remains separate; its [compatibility launcher](docs/hermes-plugin.md) still works independently of this runtime.

The new **[CTRL] Loop website MVP** adds typed chat, real `gpt-6-luna` instruction generation with high reasoning, PostgreSQL persistence, an assigned in-VM worker using `jev-1.13.0`, and an embedded desktop viewer with coordinated takeover. See the [MVP setup, AWS/worker deployment, APIs, and current limitations](docs/mvp.md). The bounded sample opens X in an already-open Chrome window; it does not establish that authenticated feed contents were read. Deployment and run evidence are documented separately from implementation.

## Run

Use Python 3.11–3.13 and `uv` from this repository:

```powershell
uv sync --locked --no-dev
```

The project pins `typesafe-sdk==0.7.1`, `cua-sandbox==0.7.0`, and `cua-fleet==0.1.17`. Jev remains `jev-1.13.0`; the adapter requires the trial image's upstream Cua Driver `0.24.0`.

Keep `TYPESAFE_API_KEY` in the local `.env` and Fleet credentials in the local `.env.fleet`. Both files are Git-ignored. The default worker reference is `.local/omarchy-fleet/claim.json`, saved by the separate [Fleet setup helper](docs/omarchy-fleet-trial.md). That reference identifies an existing claim; it is not a desktop backup. An expired or deleted claim needs deliberate provisioning outside the control loop.

```powershell
uv run --locked python src/main.py "Click the More information link." --url https://example.com/ --max-iterations 1
uv run --locked python src/main.py "Enter the provided text into the selected text field." --text "example" --max-iterations 1
```

`--url` opens an HTTP(S) URL through Driver in the remote desktop's default browser. Omit it to continue the desktop as it stands. Neither invocation imports cookies or uses the host's personal Chrome. Prepared text is exact caller input, available for one typing dispatch; Jev does not generate it.

The first command illustrates the CLI contract, not a claim of verified end-to-end website coverage. Window/control accessibility, action delivery, and website behavior depend on the selected application and the guest runtime.

| Option | Purpose |
| --- | --- |
| `--request-file PATH` | Read the exact UTF-8 request instead of the positional request. |
| `--text TEXT` / `--text-file PATH` | Supply exact text for one typing operation. |
| `--claim-file PATH` | Select an existing Fleet claim reference. |
| `--env-file PATH` | Select the TypeSafe credentials file; default `.env`. |
| `--fleet-env-file PATH` | Select the Fleet credentials file; default `.env.fleet`. |
| `--max-iterations N` | Bound observation/action iterations; default `10`. |
| `--no-screenshots` | Disable before/after desktop PNGs; `--screenshots` is the default. |

Run `uv run --locked python src/main.py --help` for the full CLI. The former Browser Use profile, browser-executable, session-import, and local-preview options are gone.

## Desktop ownership and lifetime

The CLI prints the existing noVNC desktop URL. Open it in a browser signed into Cua to inspect the worker. **Do not send manual mouse or keyboard input through noVNC while the agent is running.** Stop the command before taking control, then start a new command so it observes fresh state. The [website MVP](docs/mvp.md#access-and-desktop-ownership) has its own authenticated viewer and session-owned takeover, locked while a run is queued or running; that coordination does not cover external Fleet viewers or separate CLI processes.

An OS lock prevents two ctrlloop commands from this checkout from using the same claim concurrently. It does not exclude viewers, other machines, or other controller implementations. All window activation and input occur inside the assigned VM.

Normal completion, errors, and Ctrl+C disconnect the controller and release its local lock. They leave the cloud claim and its expiry unchanged. The original six-hour trial was extended in place to **September 30, 2026, 2:19:52 PM PDT**; see the [verified lifetime readback](docs/vm-lifetime-20260927.md) and the [trial's viewer configuration and cleanup command](docs/omarchy-fleet-trial.md). Closing the viewer does not stop billing or delete the worker.

Existing `.local/browsers` profiles were preserved but are unused. The Browser Use Windows login startup entry was removed. The remote desktop maintains its own application state for the worker's lifetime; persistence across worker deletion is not implemented here.

## Selection and execution

Each iteration discovers visible windows, runs the existing window-selection question, brings the chosen window forward inside the VM, and requests a fresh native control snapshot. Existing `Candidate` and `ChoiceSelector` interfaces retain compact descriptions and native execution identities. Screenshot pixels are not sent to Jev.

A group contains at most **10 candidates**. Groups shrink when needed to fit the existing **90,000-byte request ceiling**; round questions are batched, and winners advance together. The final control round reuses the existing click/type and settling questions. No new Jev question or completion decision is introduced.

One chosen control receives `click` or `type_text` with explicit foreground delivery inside the VM. Execution uses its native element token, or its element index together with the originating snapshot ID. Stale identities, malformed observations, and native refusals stop the run. Degraded observations containing only window/application metadata return `observation_unavailable`. Driver effects `confirmed` and `unverifiable` permit a fresh observation and new selection when iterations remain; `unverifiable` does not establish that the intended effect occurred. Partial effects, suspected no-ops, malformed responses, and refusals stop the run through `action_unconfirmed` or a Driver error. The controller never automatically replays a native action.

Nonempty prepared text survives click-only iterations and is consumed before the first typing dispatch, even if delivery becomes uncertain. Selecting typing without available text returns `no_action`. Driver owns native typing semantics; the loop does not invent a separate clear/replace operation.

An action reporting `confirmed` or `unverifiable` can be followed by the existing selected settling delay before another iteration. That iteration observes and selects again; it does not reuse the previous action. There is no extra settling wait after the final permitted action. Reaching the bound returns `iteration_limit`: it does not establish task completion.

## Reports and source

The CLI writes result JSON to stdout and diagnostics to stderr. Each run saves observations, native action results, exact Jev payloads and responses, probabilities, API counts, input-token cost, and stage timings under ignored `reports/`. Input cost uses the configured rate of `$0.042` per million input tokens. Report data can contain application text and caller-supplied text; keep it local unless intentionally sharing it.

Desktop screenshots are enabled by default and saved as `screenshots/loop-0001-before.png`, `loop-0001-after.png`, and so on. The before image is taken after selection and before action dispatch. The after image is immediate and may show an unfinished page transition. Capture failures are recorded separately from action failures.

Exit code `1` indicates an error, `observation_unavailable`, or `action_unconfirmed`. Exit code `2` indicates a bounded stop without established completion, including `iteration_limit`. A native `unverifiable` effect alone is not an error or proof of completion.

- [`src/main.py`](src/main.py): entry point and preserved cached-Hermes dispatch.
- [`src/cli.py`](src/cli.py): configuration, connection, reporting, and command lifetime.
- [`src/driver.py`](src/driver.py): native Driver MCP through the existing Fleet claim or guest loopback, ownership lock, response validation, and snapshot-bound actions.
- [`src/pipeline.py`](src/pipeline.py): window/control observation, selection, and action loop.
- [`src/selection.py`](src/selection.py), [`src/jev_questions.py`](src/jev_questions.py), and [`src/reporting.py`](src/reporting.py): shared selection, authored questions, and reports.

See the [main workflow](docs/main-workflow.md), [target architecture](docs/architecture.md), and [preserved Hermes demo](docs/hermes-plugin.md). Older Windows and browser research describes the implementation that existed when each investigation was written.
