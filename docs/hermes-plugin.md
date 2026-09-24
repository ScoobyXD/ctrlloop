# Minimal Hermes plugin

This integration registers one task-level tool, `ctrlloop_run`. Hermes interprets
the user's intent and conversation context, chooses the tool for desktop/app
interaction, and supplies a concise English command as `request`. It may prepare
one optional `text` string in that same initial call. The existing ctrlloop CLI runs in its own Python process
and Jev selects the UI actions. Prepared text serves the first typing operation,
including a real control such as Chrome's address bar. The tool immediately returns a **launch receipt**.
It does not establish that initialization, any UI action, or the task succeeded.

There is no generated-text request/reply connection, `ctx.llm` use, conversation
injection, result retrieval/summary, Stop forwarding, cancellation, process
supervision, or automatic file cleanup. The user's Hermes configuration disables
the competing built-in desktop and browser-control tools; the plugin itself does
not replace them. Avoid overlapping desktop runs: this adapter does not enforce
mutual exclusion.

## Source and installation

| Item | Exact location |
| --- | --- |
| Existing main checkout | `C:\Users\Jonat\ctrlloop` |
| Plugin manifest | `C:\Users\Jonat\ctrlloop\plugins\hermes\plugin.yaml` |
| Plugin adapter | `C:\Users\Jonat\ctrlloop\plugins\hermes\__init__.py` |
| Existing CLI / engine | `C:\Users\Jonat\ctrlloop\src\main.py` / `src\pipeline.py` |
| ctrlloop interpreter | `C:\Users\Jonat\ctrlloop\.venv\Scripts\python.exe` |
| Hermes development source | `C:\Users\Jonat\hermes-dev` |
| Hermes interpreter | `C:\Users\Jonat\hermes-dev\venv\Scripts\python.exe` |
| Existing effective Hermes home / profile | `C:\Users\Jonat\AppData\Local\hermes` / `default` |
| User plugin discovery junction | `C:\Users\Jonat\AppData\Local\hermes\plugins\ctrlloop` → `C:\Users\Jonat\ctrlloop\plugins\hermes` |
| Existing Hermes configuration | `C:\Users\Jonat\AppData\Local\hermes\config.yaml` |
| Existing desktop userData | `C:\Users\Jonat\AppData\Roaming\Hermes` |

Hermes's directory loader requires `plugin.yaml` and `__init__.py` with
`register(ctx)`. It scans `<effective HERMES_HOME>\plugins` and follows directory
junctions. The adapter resolves its own file path through that junction to find
the source checkout. No source is copied into Hermes, no ctrlloop package is
installed into Hermes's environment, and `tool.uv.package = false` is unchanged.

Manifest fields implemented here:

| Field | Type | Value |
| --- | --- | --- |
| `name` | string | `ctrlloop` |
| `version` | string | `0.1.0` |
| `description` | string | `Launch the local ctrlloop computer-use program with a task request.` |
| `provides_tools` | array of strings | `["ctrlloop_run"]` |

The omitted `kind` uses Hermes's `standalone` default. The directory name
`ctrlloop` is also the loader's registry key. No hooks, dependencies, custom
configuration schema, or tool override permission are declared.

Enablement requires `plugins.enabled` to include `ctrlloop`; any matching
`plugins.disabled` entry would prevent loading. The desktop normally uses
`platform_toolsets.cli`; this setup uses an explicit list containing `ctrlloop`
and the existing non-control toolsets. The `hermes-cli` bundle was expanded so
it cannot restore competing controls in delegated agents. There is no new
platform or profile. Existing coding context remains `auto`; selecting a new
tool preset or explicitly re-enabling controls changes this configuration.

The relevant configuration entries are:

```yaml
platform_toolsets:
  cli: [clarify, code_execution, cronjob, ctrlloop, delegation, file, image_gen, kanban, memory, session_search, skills, terminal, todo, tts, vision, web]
agent:
  disabled_toolsets: [computer_use, browser_tools, browser-cdp]
plugins:
  enabled:
    - ctrlloop
```

These are entries within the existing config, **not a replacement config**.
For a fresh installation into the same existing home, create the junction once:

```powershell
New-Item -ItemType Directory -Path 'C:\Users\Jonat\AppData\Local\hermes\plugins' -Force
New-Item -ItemType Junction `
    -Path 'C:\Users\Jonat\AppData\Local\hermes\plugins\ctrlloop' `
    -Target 'C:\Users\Jonat\ctrlloop\plugins\hermes'
```

Then add the entries above while preserving the rest of the config. Hermes also
supports `hermes plugins enable ctrlloop --no-allow-tool-override` when invoked
with this existing effective home. Do not run plugin install/update/remove on
the source junction; this is an editable source registration, not a cloned
plugin managed by Hermes's installer.

## Computer-use tool selection

The saved default-profile configuration removes Hermes's built-in `computer_use`
and all `browser_*` tools, including CDP, console execution, dialogs, screenshots,
clicking and typing. `ctrlloop_run` remains enabled. Global exclusions apply to
normal tool resolution and tool discovery; the explicit desktop enabled list
also prevents these tools from returning through inherited child toolsets.

`browser_tools` is Hermes's supported legacy exclusion for the core browser
controls; `browser-cdp` also removes `browser_cdp` and `browser_dialog`. Using
these instead of excluding the whole `browser` toolset preserves the separate
`web_search` setting. Ordinary terminal and code-execution tools remain enabled;
this is a control-tool configuration, not an OS-level sandbox against scripts.
No competing MCP controller is configured, and `ctrlloop` is the only explicitly
enabled user plugin.

The previous config was backed up beside `config.yaml` as
`config.yaml.before-ctrlloop-only-20260924T013713851995Z.bak`. Only
`agent.disabled_toolsets` and `platform_toolsets.cli` changed; authentication,
history and other settings were preserved. Finish active work, fully restart
Hermes, and begin a new conversation to apply the tool selection and discard
previous tool guidance. Existing sessions may still hold their old tool lists.

## Development loop

- Edit `src\*.py`, save: the next `ctrlloop_run` launch loads that source.
- Edit `plugins\hermes\__init__.py` or `plugin.yaml`, save: finish active Hermes
  work and preserve drafts, fully exit Hermes, then open
  `C:\Users\Jonat\hermes-dev\Hermes Dev.lnk`. The existing launcher refuses
  to switch while Hermes/backend processes remain running.
- Ordinary Python edits need no rebuild or reinstall. ctrlloop dependency
  changes still use its own existing `uv sync --locked` workflow.

The shared Hermes home also makes this plugin discoverable by the original
Hermes installation on its next startup. The original and development launchers
continue to use the same profile, authentication, settings, and histories.

## Complete tool input and registration contract

`register(ctx) -> None` receives Hermes's existing `PluginContext` and registers
exactly one tool. No code is run at import/registration beyond path resolution
and tool registration. It passes `name="ctrlloop_run"`, `toolset="ctrlloop"`, the
raw function schema, and `handler=run` to `ctx.register_tool`. Remaining Hermes
defaults apply: `check_fn=None`, `requires_env=None`, `is_async=False`,
`description=""`, `emoji=""`, `override=False`. The handler is synchronous;
the launched child runs independently.

Exact registered function schema (also in [the adapter](../plugins/hermes/__init__.py)):

```json
{
  "name": "ctrlloop_run",
  "description": "Use for desktop or app interaction. Launch receipt only; no completion reporting or Stop. Do not overlap other computer-use actions.",
  "parameters": {
    "type": "object",
    "properties": {
      "request": {
        "type": "string",
        "minLength": 1,
        "description": "Turn the user's intent and conversation context into a concise English computer-use command."
      },
      "text": {
        "type": "string",
        "description": "Prepare the exact text for the first typing step (search, URL, or message). Required for typing; used once."
      }
    },
    "required": ["request"],
    "additionalProperties": false
  }
}
```

| Input | Type | Required/default | Treatment |
| --- | --- | --- | --- |
| `request` | string | Required; no default | Hermes's concise English command derived from user intent/context; must contain non-whitespace text and encode as UTF-8; accepted command is forwarded unchanged |
| `text` | string | Optional for click-only tasks; required for typing | Nonempty string used verbatim once; missing, empty, or consumed text stops typing with `no_action`; whitespace-only strings are valid; `null` and other non-string values are rejected |
| Other argument fields | none permitted | Rejected | No per-call driver, timeout, iteration, or path options |
| Hermes handler `args` | Python dict | Supplied by Hermes | The decoded argument object above |
| Hermes handler `**kwargs` | Hermes execution context | Optional | Ignored; current Hermes can supply `task_id`, `session_id`, `user_task` |

The callable signature is `run(args: dict, **kwargs) -> str`. Hermes wraps the
registered raw schema in its existing function-tool envelope. The adapter
receives only the command and optional typing text the Hermes model supplies.
Hermes interprets the conversation before calling the tool; the adapter never
reads the full conversation, system prompt, attachments, or Hermes context
fields. Its descriptions guide the existing model's tool choice and argument
generation, without a separate intent classifier or generation call. The saved
configuration removes competing desktop/browser tools; other non-control tools
remain available.

The accepted command reaches the existing Jev calls unchanged. The engine's
existing `original_request` name refers to this command, not necessarily the
user's raw message. The adapter does not separately store the raw message.

Example arguments:

```json
{"request": "Open Chrome using Windows Search.", "text": "Chrome"}
```

```json
{"request": "In my browser on x.com, open the History tab, then open Liked posts.", "text": "x.com"}
```

Hermes can turn a conversational request such as "can you get me to x.com in
my browser and then open History and Liked posts?" into the second example.
Typing text can be a search query, URL, or drafted message and need not appear
verbatim in the command. Both arguments are prepared in the same initial call;
no additional model call or later generated-text callback is added.

## Experimental prepared-text behavior

A nonempty `text` stays pending through click-only iterations. The first actual
typing dispatch uses it, whether Jev selected a real control or the synthetic
Windows Search route. There is no word extraction, text-selection round, Noul
question, or probability filter. The supplied value is not sent as
extra control-selection context; Jev still selects the control and action from
the supplied command and existing state. No new capability check is added.

After that typing dispatch, later typing stops with `no_action` because the
prepared text has been consumed. The value is never automatically replayed, even after an
unverified effect; existing partial/no-op and native-error stopping behavior
remains. If the run stops before typing, the value is unused. This experiment
does not associate text with a particular field: the caller must supply the
string appropriate for the **first** typing operation. This MVP supplies one
typing operation per run; it cannot generate more text during the loop.

Omitted `text` and `text: ""` permit clicks but stop at typing with `no_action`.
Windows Search stops before Win+S when text is unavailable. The reason is
`No prepared text is available for this typing operation (missing, empty, or already used).`
Nonempty whitespace-only strings are literal input. Unicode, leading and
trailing spaces, tabs, LF, CRLF, and lone CR are preserved through the adapter,
CLI and pipeline to the existing CUA `type_text` payload. Native target/app
behavior still determines the result: this does not clear/select existing field
contents or submit Enter. An address bar with existing text may receive an
insertion rather than a replacement URL. The existing `run_pipeline(..., text=...)`
argument and `--text` now share these semantics; `--text-file` reads UTF-8 bytes
without newline conversion and is mutually exclusive with `--text`.

## Try it in Hermes

Finish active Hermes work and preserve drafts, fully exit Hermes, then open
`C:\Users\Jonat\hermes-dev\Hermes Dev.lnk`. A full restart loads the changed
adapter/schema; no rebuild or reinstall is needed. Ensure an earlier ctrlloop
child is no longer running before launching another desktop run. Start a new
Hermes conversation so previously loaded control tools/guidance are not reused.

Copy this prompt into Hermes:

```text
Can you get me to x.com in my browser and then open History and Liked posts?
```

Hermes should recognize the desktop task and form arguments like the second
JSON example above; exact wording and tool choice are model decisions. Check
the tool-call arguments to see what Hermes supplied. The receipt's directory holds
the exact `request.txt` and `text.txt`; `stderr.log` points to the run report.
For a New Tab click followed by address-bar typing, `action_requested` for
`type_text` should contain `text: "x.com"` and that iteration should have no
`text_r1` source-word round. The receipt alone does not establish this happened
or that navigation/task completion succeeded.

## Process and file boundary

Each valid launch creates `.local\hermes\launch-<random suffix>` beneath the
resolved checkout using `tempfile.mkdtemp`. All paths below are absolute at runtime.

```text
C:\Users\Jonat\ctrlloop\.venv\Scripts\python.exe
  C:\Users\Jonat\ctrlloop\src\main.py
  --request-file
  C:\Users\Jonat\ctrlloop\.local\hermes\launch-<suffix>\request.txt
```

With nonempty prepared text, append `--text-file` and the absolute path to
`<directory>\text.txt`. This is a four-element argument list without prepared
text, or six elements with it, passed directly to `subprocess.Popen`, with
`shell=False` (default), `cwd=C:\Users\Jonat\ctrlloop`, stdin `DEVNULL`, stdout
and stderr redirected to the files below, and Windows `CREATE_NO_WINDOW`.
There is no terminal window, shell quoting, stdin protocol, IPC server, or
request/prepared text on the command line. The child inherits the process environment;
the adapter adds or overrides no environment variables and does not read secrets.

| Local file | Format / contents | Lifecycle |
| --- | --- | --- |
| `<directory>\request.txt` | UTF-8 bytes, no BOM; exactly `request.encode("utf-8")` | Written before process creation; no newline conversion |
| `<directory>\text.txt` | UTF-8 bytes, no BOM; exactly `text.encode("utf-8")` | Written only for nonempty prepared text before process creation; no newline conversion |
| `<directory>\stdout.json` | Unmodified child stdout bytes; normally the existing final JSON object plus newline | Opened before launch; can be empty while running or on early failure; not parsed by the adapter |
| `<directory>\stderr.log` | Unmodified child stderr bytes; UTF-8 diagnostics, selection logging and report locations | Opened before launch; may grow during execution |
| `reports\run-<UTC timestamp>-<UUID hex>\total_run.json` | Existing chronological JSON array of report events | Produced by the CLI, not the adapter |
| Same report directory's numbered `*_jev_input*.json` / `*_jev_output*.json` | Existing captured Jev request/response bodies | Produced by the CLI; non-JSON response bodies may retain raw bytes |

Files persist, including partially created launch folders after a launch error.
The adapter performs no cleanup. `/.local/` and `reports/` were already ignored
by Git. Reports and launch files can contain the supplied command, prepared text and UI text.

Existing CLI defaults inherited by this invocation:

| Existing input | Type / current default |
| --- | --- |
| Positional request | Not supplied; `--request-file` is used instead |
| `--text` / `--text-file` | Optional mutually exclusive first-typing inputs; adapter uses `--text-file` only when tool `text` is nonempty; missing/empty/consumed text stops typing |
| `--delivery-mode` | Optional `foreground` / `background`, `None`; existing browser/native behavior |
| `--env-file` | Path, `C:\Users\Jonat\ctrlloop\.env` |
| `--driver` | Optional string, `None`; resolves PATH `cua-driver`, then `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin\cua-driver.exe`, then literal `cua-driver` |
| `--driver-socket` | Optional string, default `\\.\pipe\cua-driver` |
| `--max-elements` / `--max-depth` | Integers, `5000` / `25` |
| `--max-iterations` | Positive integer, `10`; does not assess completion |
| Jev configuration | Existing `pyproject.toml` pin `jev-1.13.0` |
| Credentials | Existing `TYPESAFE_API_KEY` environment variable or default `.env`; inherited environment wins (`override=False`, `interpolate=False`) |
| Desktop input | Existing CUA daemon and live Windows UI state, acquired by ctrlloop |

The existing driver must already be available. This plugin neither starts nor
changes it. In particular, the default shared daemon needs the existing direct
accessibility capability for clicks; an unpatched daemon will fail closed. See
[the existing driver setup](cua-driver/direct-accessibility-only.md). This
packaging step does not select an isolated patched pipe automatically.

## Complete tool output contract

The handler returns a **JSON-encoded string**, using Hermes's existing
`tool_result` / `tool_error`. No new result class or transport envelope is added.

On process creation:

```json
{"status": "launched", "pid": 12345, "directory": "C:\\Users\\Jonat\\ctrlloop\\.local\\hermes\\launch-example"}
```

| Field | Type | Presence / meaning |
| --- | --- | --- |
| `status` | string, always `"launched"` | Required on launch receipt; `Popen` returned, nothing more |
| `pid` | integer | Required on launch receipt; Windows child PID, not a durable task ID |
| `directory` | string | Required on launch receipt; absolute folder containing the three launch files plus optional `text.txt` |
| `error` | string | The only field on failure before a launch receipt; no `status`, `pid`, or `directory` |

Example failure: `{"error": "request must be a nonempty string."}`.
Exact adapter error cases:

| Condition | `error` value |
| --- | --- |
| Non-dict arguments, missing request, or extra fields | `Provide request (a nonempty string) and optionally text (a string).` |
| Non-string, empty, or whitespace-only request | `request must be a nonempty string.` |
| Non-string `text`, including `null` | `text must be a string when provided.` |
| Non-Windows host | `This ctrlloop launcher requires Windows.` |
| Missing interpreter or entry point | `ctrlloop requires .venv/Scripts/python.exe and src/main.py in its source checkout.` |
| UTF-8 encoding, file, or process launch error | `ctrlloop launch failed (<exception class name>). Check the local checkout and file permissions.` |

No child exit code, status, action result, or report is sent back to Hermes.
An immediate child crash can still follow a `launched` receipt. Hermes Stop
has no connection to this child. The plugin does not poll, retry, wait, retain
a process registry, or promise that the child survives all parent shutdowns.

## Existing CLI output structures (local only)

The adapter redirects these without decoding or changing their schema.
[PipelineResult](../src/pipeline.py) still owns the normal stdout object:

| Existing field | Type |
| --- | --- |
| `status` | string |
| `window`, `control` | Existing native record object or `null` |
| `last_control` | Existing context object `{window: app/title context, control: reduced description, action: "click" or "type_text"}` or `null` |
| `snapshot_id`, `reason` | string or `null` |
| `observation` | Existing observation object |
| `selections` | Object mapping stage names to arrays of existing selection rounds |
| `action_result` | Existing native CUA action object or `null` |
| `iterations` | Array of existing loop trace objects |
| `timing` | Existing report timing object, added when report finalization succeeds |

Caught errors instead use the existing `{status: "error", error: string}` with
optional `DriverError.details()` (`code`, `tool`, `exit_code`, and existing
context fields) and `timing`. Native records, round/group/candidate structures,
loop traces, and event payloads remain defined in [the main workflow](main-workflow.md),
[selection](../src/selection.py), [driver](../src/driver.py), and
[reporting](../src/reporting.py); the plugin declares no parallel copies.
The existing timing fields are `origin`, `duration_ms`, `jev_api_call_count`,
`time_to_first_jev_request_ms`, `end_boundary`, `report_generation_duration_ms`,
`report_write_duration_ms`, `report_write_count`, `jev_requests_directory`,
`jev_request_file_count`, `jev_request_write_duration_ms`,
`jev_response_file_count`, `jev_response_write_duration_ms`, and
`through_report_duration_ms`. See [timing definitions](timing.md).

Current CLI exit codes: `2` for normal pipeline returns because task completion
is unassessed; `1` for caught failures; argparse errors also use `2` and may have
no JSON stdout. Unexpected exceptions/interruption can leave stdout empty.
The adapter does not inspect exit codes. Existing iteration limits, retained
window behavior, Jev question wording, and CUA action behavior are unchanged.

## Initial adapter verification on 2026-09-23

- Hermes's real user-directory scanner found the junction; its config readers
  confirmed `ctrlloop` is enabled and not disabled.
- Its real loader registered exactly `ctrlloop_run`; its toolset resolver
  returned `["ctrlloop_run"]`, and the adapter resolved the original checkout.
- The CLI toolset includes `ctrlloop`; built-in override permission remains false.
  The config edit was checked to change only `plugins.enabled` and that list entry.
- Adapter syntax parsing, the existing CLI's `--help`, an empty-request error
  check, and `git diff --check` passed. No automated tests were added or run.

No valid computer-use request was dispatched, no Jev call was made, and no
desktop action or child launch was exercised. Live activation in the
already-running Hermes desktop requires the next full restart after active
work and drafts are preserved. Hermes was left running without a restart.

## Historical prepared-text MVP verification on 2026-09-23

This records the initial MVP before the word/Noul fallback was removed. Its
fallback results below describe that earlier implementation, not current behavior.

- Syntax/import and schema registration checks passed. The JSON schema in this
  document matches the adapter's registered schema exactly. Invalid `text`
  types and extra keys return adapter errors; the launch receipt is unchanged.
- An intercepted adapter launch preserved request/text UTF-8 bytes, including
  Unicode, whitespace, CRLF, lone CR, and LF. Whitespace-only text was forwarded;
  omitted/empty text produced no text-file argument. `Popen` was substituted,
  so no child was launched. Sandbox ACLs prevented reading/writing inside
  `tempfile.mkdtemp` directories during that check, so ordinary directory
  creation was substituted too; production launch-directory logic is unchanged.
- CLI `--help` and mutual exclusion of `--text` / `--text-file` passed. A manual
  CLI invocation with network/driver/pipeline objects substituted in memory
  forwarded exact request/text file contents into the existing pipeline call.
- A manual in-memory pipeline example dispatched click, prepared typing, then
  ordinary typing: source-word selection occurred only in iteration 3. Omitted
  and empty text selected words in iterations 2 and 3. Whitespace-only input
  remained exact. Windows Search used prepared text with no text-selection call.
- `git diff --check` passed. No automated test suite was added or run, no live
  Jev request or desktop action was sent, and Hermes was not restarted. A real
  Hermes/desktop run remains the trial described above.

## Control-tool configuration verification on 2026-09-23 (Pacific)

Fresh Hermes schema resolution from the saved config retained `ctrlloop_run`
and returned no `computer_use` or `browser_*` tools. The inherited child list,
checked without the parent's disabled list, also retained `ctrlloop_run` with
zero competing control tools; no additional child-request toolset could restore
them. `web` remains enabled. Availability probes in the verification process
reported web-search credentials unavailable; credentials were not modified.
No tool was dispatched and no desktop action or Hermes restart was performed.

## Prepared-only cleanup and live rerun on 2026-09-23 (Pacific)

Removed request tokenization, `TEXT_WORD`, `select_text`, the Noul parser and
70% inclusion filter. Existing window/control/action/timer questions and model
pin are unchanged. Syntax/import/schema checks and a manual in-memory example
confirmed click preservation, exact one-time text delivery, and `no_action`
when text is missing, empty or consumed. No automated test suite was added or run.

The supplied `run-20260924T013947123434Z-c986b8ef73c64de8ae2c9ceda086717c`
report did receive `https://x.com`. Loop 1 inserted it before an existing
ChatGPT URL; loop 2 observed the combined value and stopped in the old Noul
fallback. This was an insertion/submission issue, not missing prepared text.

The same saved request/text were rerun through ctrlloop after removal:
`run-20260924T014645845415Z-e35f374e7e46475392f75098523b7705`. It made five
Jev calls in 5,584.7555 ms operational time, with zero word-selection stages.
It inserted text once, then stopped with the prepared-text-unavailable reason
on loop 2. The readback showed another URL prefix; it did not navigate to X.
Address replacement/submission remains unresolved; no driver behavior was changed.
