# Minimal Hermes plugin

This integration registers one task-level tool, `ctrlloop_run`. Hermes supplies
the user's task text; the existing ctrlloop CLI runs in its own Python process
and Jev selects the UI actions. The tool immediately returns a **launch receipt**.
It does not establish that initialization, any UI action, or the task succeeded.

There is no generated-text request/reply connection, `ctx.llm` use, conversation
injection, result retrieval/summary, Stop forwarding, cancellation, process
supervision, or automatic file cleanup. No built-in Hermes computer-use, browser,
screenshot tools, or skills are replaced or disabled. Avoid overlapping desktop
runs: this initial adapter does not enforce mutual exclusion.

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
`platform_toolsets.cli`; this setup explicitly adds `ctrlloop` alongside
`hermes-cli`, preserving all existing tools. There is no new platform or profile.
Hermes's explicit `HERMES_TUI_TOOLSETS`, coding focus mode, or disabled-toolset
settings can override normal tool availability. This setup does not change them.

The relevant configuration entries are:

```yaml
platform_toolsets:
  cli:
    - hermes-cli
    - ctrlloop
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
  "description": "Launch a computer-use task in the local ctrlloop program; Jev selects the UI actions. Pass the user's original task text unchanged. Returns only a process launch receipt, not execution success or task completion. No result retrieval or Stop forwarding is connected. Do not launch another task or perform other computer-use actions while it may still be running.",
  "parameters": {
    "type": "object",
    "properties": {
      "request": {
        "type": "string",
        "minLength": 1,
        "description": "The original user task text, passed verbatim to ctrlloop. No conversation history is added."
      }
    },
    "required": ["request"],
    "additionalProperties": false
  }
}
```

| Input | Type | Required/default | Treatment |
| --- | --- | --- | --- |
| `request` | string | Required; no default | Must contain non-whitespace text and encode as UTF-8; accepted text is passed unchanged, including leading/trailing whitespace and CRLF/LF |
| Other argument fields | none permitted | Rejected | No per-call driver, text, timeout, iteration, or path options |
| Hermes handler `args` | Python dict | Supplied by Hermes | The decoded argument object above |
| Hermes handler `**kwargs` | Hermes execution context | Optional | Ignored; current Hermes can supply `task_id`, `session_id`, `user_task` |

The callable signature is `run(args: dict, **kwargs) -> str`. Hermes wraps the
registered raw schema in its existing function-tool envelope. The adapter
receives only the request the Hermes model supplies. It cannot guarantee the
model copied the user's message exactly; its description instructs it to do so.
It never reads the full conversation, system prompt, attachments, or Hermes
context fields. The same accepted request text reaches the existing Jev calls.

Example arguments:

```json
{"request": "Open Chrome using Windows Search."}
```

## Process and file boundary

Each valid launch creates `.local\hermes\launch-<random suffix>` beneath the
resolved checkout using `tempfile.mkdtemp`. All paths below are absolute at runtime.

```text
C:\Users\Jonat\ctrlloop\.venv\Scripts\python.exe
  C:\Users\Jonat\ctrlloop\src\main.py
  --request-file
  C:\Users\Jonat\ctrlloop\.local\hermes\launch-<suffix>\request.txt
```

This is a four-element argument list passed directly to `subprocess.Popen`, with
`shell=False` (default), `cwd=C:\Users\Jonat\ctrlloop`, stdin `DEVNULL`, stdout
and stderr redirected to the files below, and Windows `CREATE_NO_WINDOW`.
There is no terminal window, shell quoting, stdin protocol, IPC server, or
request text on the command line. The child inherits the process environment;
the adapter adds or overrides no environment variables and does not read secrets.

| Local file | Format / contents | Lifecycle |
| --- | --- | --- |
| `<directory>\request.txt` | UTF-8 bytes, no BOM; exactly `request.encode("utf-8")` | Written before process creation; no newline conversion |
| `<directory>\stdout.json` | Unmodified child stdout bytes; normally the existing final JSON object plus newline | Opened before launch; can be empty while running or on early failure; not parsed by the adapter |
| `<directory>\stderr.log` | Unmodified child stderr bytes; UTF-8 diagnostics, selection logging and report locations | Opened before launch; may grow during execution |
| `reports\run-<UTC timestamp>-<UUID hex>\total_run.json` | Existing chronological JSON array of report events | Produced by the CLI, not the adapter |
| Same report directory's numbered `*_jev_input*.json` / `*_jev_output*.json` | Existing captured Jev request/response bodies | Produced by the CLI; non-JSON response bodies may retain raw bytes |

Files persist, including partially created launch folders after a launch error.
The adapter performs no cleanup. `/.local/` and `reports/` were already ignored
by Git. Reports and launch files can contain the original request and UI text.

Existing CLI defaults inherited by this invocation:

| Existing input | Type / current default |
| --- | --- |
| Positional request | Not supplied; `--request-file` is used instead |
| `--text` | Optional string, `None`; existing Windows Search override remains unset |
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
| `directory` | string | Required on launch receipt; absolute folder containing the three launch files |
| `error` | string | The only field on failure before a launch receipt; no `status`, `pid`, or `directory` |

Example failure: `{"error": "request must be a nonempty string."}`.
Exact adapter error cases:

| Condition | `error` value |
| --- | --- |
| Non-dict arguments, missing request, or extra fields | `Provide exactly one field: request (a nonempty string).` |
| Non-string, empty, or whitespace-only request | `request must be a nonempty string.` |
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

## Verification performed on 2026-09-23

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
