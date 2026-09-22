# CUA Driver 0.28.2: native Windows read and click contracts

Read behavior was verified on Windows on 2026-09-21. This reference covers
`list_windows`, explicit window identity, `get_window_state` with accessibility
enabled and screenshots disabled, and the distinct desktop-discovery tool
`get_accessibility_tree`. The click input was checked against the installed
0.28.2 binary; its output and cache contracts were checked in the matching release
source. A separate disposable-control host smoke test verified the click path,
as described below; the earlier saved read samples remain read-only evidence.

## Evidence and authoritative schemas

| File | Origin and meaning |
| --- | --- |
| [installed-tool-definitions.json](installed-tool-definitions.json) | The three selected tool definitions from the installed binary's `dump-docs --type mcp --pretty`. |
| [mcp-tool-definitions.json](mcp-tool-definitions.json) | The same three tools returned by an actual MCP `tools/list` call to the installed daemon. Includes the advertised output schemas. |
| [list_windows input](schemas/list_windows.input.schema.json) | Exact exported JSON Schema for tool arguments. |
| [list_windows output](schemas/list_windows.output.schema.json) | Exact advertised JSON Schema for MCP `structuredContent`, not the outer response envelope. |
| [get_window_state input](schemas/get_window_state.input.schema.json) | Exact exported JSON Schema for tool arguments. |
| [get_window_state output](schemas/get_window_state.output.schema.json) | Exact advertised JSON Schema for MCP `structuredContent`, including the success and refusal alternatives. |
| [get_accessibility_tree input](schemas/get_accessibility_tree.input.schema.json) | Exact exported empty-object argument schema. No output schema is advertised for this tool. |
| [click input](schemas/click.input.schema.json) | Exact input schema exported from the installed binary; includes more targeting modes than ctrlloop exposes. |

The input schemas exported by `dump-docs` matched the live MCP `inputSchema` values. The files are preserved as exported, including any upstream omissions. In particular, the `list_windows` output schema requires `title` but omits its property definition; the version-matched Windows implementation and observed responses establish that it is a string.

The saved success samples and MCP error payload were validated against these schemas with a JSON Schema validator. The missing-required-field input fails its schema as expected; the both-flags-false and mismatched-owner inputs pass the basic schema but fail at runtime. See [validation.json](validation.json).

Examples in [examples](examples/) are sanitized actual responses, not complete invented fixtures. Discovery arrays retain only the relevant Explorer records; IDs are replaced consistently and a folder title is redacted. Refer to [runtime-verification.json](runtime-verification.json) for capture details. Do not reuse the sample IDs.

## Transport: CLI is not the MCP envelope

### CLI

```powershell
'{}' | cua-driver call list_windows
```

The daemon must be running. Pass one JSON object on standard input; this avoids PowerShell native-argument quoting problems. On success, `call` prints the tool's **structured payload directly** as JSON. For example, access `response.windows` or `response.elements`, not `response.structuredContent.elements`. Human summaries are not printed when structured data exists. Diagnostic/telemetry notices may occur on stderr; keep stderr separate from JSON stdout.

### MCP

Connect to the existing service using `cua-driver mcp --socket \\.\pipe\cua-driver`. A bare Windows `cua-driver mcp` instead owns a separate runtime. An MCP client handles initialization and JSON-RPC framing; tool arguments have the same input schemas in either mode.

The request is JSON-RPC `tools/call` with `params.name` and `params.arguments`. The response has JSON-RPC `result`, whose tool-result envelope contains `content` and, when available, `structuredContent`. A text content block has `type: "text"` and `text: string`. For this tree-only workflow no image block is expected.

Read `result.structuredContent.elements` on the raw JSON-RPC response, or `toolResult.structuredContent.elements` when an MCP library has already unwrapped `result`. An error can have `isError: true`; absence of `isError` is normal on success. JSON-RPC transport/protocol errors are separate from tool errors.

The output schemas here validate **structuredContent only**. Their `anyOf` includes both the success object and a permissive refusal object with at least one of `refusal`, `status`, or `code`. Schema validity alone is therefore not proof of success.

Exact recorded JSON-RPC examples: [success request](examples/mcp.get_window_state.input.json), [success response](examples/mcp.get_window_state.output.json), [missing-field request](examples/mcp.get_window_state.missing_window_id.input.json), and [missing-field error response](examples/mcp.get_window_state.missing_window_id.output.json).

## 1. list_windows

Input: an object with no required properties. Unknown properties are rejected by the advertised schema.

| Property | Type | Required | Default / meaning |
| --- | --- | --- | --- |
| `pid` | integer | No | Omit to enumerate all processes' windows; otherwise filter by the observed process ID. |
| `on_screen_only` | boolean | No | `false`; `true` retains windows whose `is_on_screen` is true. |

Minimal input: `{}`. A filter is `{"pid":4242,"on_screen_only":true}` with an actual observed ID substituted. The schema does not declare a numeric minimum for `pid`; use a positive live ID, not zero or a guessed number.

The Windows success payload is:

```typescript
type ListWindowsPayload = {
  windows: WindowRecord[];
  current_space_id: null;
  _legacy_windows: LegacyWindowRecord[];
};
type WindowRecord = {
  window_id: number; // integer HWND
  pid: number;       // integer owner process ID in Windows implementation
  app_name: string;  // executable name, possibly empty when unavailable
  title: string;     // possibly empty
  bounds: { x: number; y: number; width: number; height: number };
  layer: 0;
  z_index: number;   // integer; advertised portable schema also permits null
  is_on_screen: boolean;
  minimized: boolean;
};
type LegacyWindowRecord = {
  window_id: number; pid: number; title: string;
  x: number; y: number; width: number; height: number;
  is_on_screen: boolean; minimized: boolean;
};
```

These types describe this Windows implementation; the exported portable output schema is broader. It requires `windows`, and each window's `window_id`, `pid`, `app_name`, `title`, `bounds`, `is_on_screen`, and `z_index`. It permits a null `pid`/`z_index` and optional nullable platform metadata. Windows emits `layer`, `minimized`, root `current_space_id: null`, and the legacy array. macOS space membership fields are not populated here.

Bounds describe desktop geometry; negative coordinates can occur, including minimized windows. Higher numeric `z_index` means nearer the front; never interpret null as zero or infer stacking from array position. Do not depend on legacy fields for a new integration.

## 2. Choosing a window

There is no separate selection call in this workflow. Choose a record from `windows`, retain its `pid` and `window_id`, and pass both to `get_window_state`. This does not activate, focus, click, or alter the application. One app can have multiple windows; an app name or PID by itself does not identify the window.

The driver verifies that the HWND belongs to the supplied PID. For packaged apps, the owner can be a hosting process; use the pair returned by discovery. IDs are live OS identifiers, not permanent identities; rediscover if the window closes or is recreated.

## 3. get_window_state input

All types below come from the installed input schema. Unknown properties are disallowed. Only `pid` and `window_id` are required. Optional means **omit the property**; input schemas do not permit null values.

| Property | JSON type | Default / behavior |
| --- | --- | --- |
| `pid` | integer, required | Owner process ID from the selected window record. |
| `window_id` | integer, required | HWND from that same record; must belong to `pid`. |
| `include_accessibility_tree` | boolean | `true`; keep true for this workflow. |
| `include_screenshot` | boolean | `true`; set **false** for tree-only reads. |
| `max_elements` | integer, minimum 1 | `5000`; bounds nodes walked, not only returned actionable rows. |
| `max_depth` | integer, minimum 1 | `25`; bounds tree-walk depth. |
| `query` | string | Omit for no query projection. Case-insensitive substring filtering with ancestor retention; original indices remain unchanged. |
| `session` | string | Optional public lifecycle label; not permission or authentication. |
| `max_dimension` | integer, minimum 1 | Screenshot longest-edge limit, folded with the configured ceiling. Irrelevant when capture is skipped. |
| `screenshot_out_file` | string | Omit. Providing it forces screenshot capture even if `include_screenshot` is false. |
| `capture_mode` | string enum `"ax"`, `"vision"` | Deprecated and ignored. Description claims arbitrary aliases work, but the actual advertised schema is the two-value enum. Omit it. |

Both include flags false with no screenshot path is an error. The input schema itself cannot express every cross-field/runtime restriction, so valid input-schema JSON may still produce a tool error.

Example request: [get_window_state.input.json](examples/get_window_state.input.json). It uses smaller explicit limits for the smoke test; those are example limits, not driver defaults.

## 4. get_window_state output

The advertised success schema requires only `pid` and `window_id`. Other declared fields are optional and nullable in that portable schema. **Do not make every field required based on one sample.** The ordinary successful Windows tree-read implementation supplies the following fields:

| Field | Type | Meaning / emission |
| --- | --- | --- |
| `pid` | integer | Requested owner PID. |
| `window_id` | integer | Requested HWND. |
| `snapshot_id` | string | Opaque current snapshot identity; normally emitted for public tree reads. |
| `elements` | array of element records | Indexed rows from the UIA walk, projected by `query` if supplied. |
| `tree_markdown` | string | Readable hierarchy; may also contain unindexed structural rows. |
| `element_count` | nonnegative integer | Indexed-element count before query projection. |
| `total_element_count` | nonnegative integer | Same pre-projection indexed count, within the bounded walk. |
| `returned_element_count` | nonnegative integer | Length of the returned `elements` array after projection. |
| `elements_complete` | boolean | Windows 0.28.2 explicitly emits **false** because it cannot prove every subtree was exhausted. |
| `_note` | string | Human-oriented implementation note; not a stable control field. |
| `window_bounds` | object | `{x,y,width,height}` numeric desktop geometry when available. |
| `window_title` | string | Present when a nonempty title is available. |
| `app_name` | string | Present when a nonempty process name is available. |
| `degraded` | boolean | Emitted as true for an empty indexed tree or partial MSAA fallback. Omission does not prove completeness. |
| `degraded_reason` | string | Explanation when degraded. |
| `escalation` | object | Can appear on an empty indexed tree; a recommendation, not authorization to perform an action. |

The portable schema also declares optional nullable `filtered_element_count`, `truncated`, `truncation_reason`, and screenshot metadata. Their presence in a schema does not mean this Windows path populates them. Native browser-related metadata can also be added; it is outside this non-browser workflow. Output schemas allow additional properties. The complete authoritative list of permitted fields and types is in [get_window_state.output.schema.json](schemas/get_window_state.output.schema.json).

No screenshot bytes, image content block, screenshot dimensions, or screenshot file is expected when `include_screenshot:false` and no screenshot path is supplied. Do not confuse that deliberate omission with a capture failure.

### Element records

The advertised element schema requires only `element_index`, `role`, and `depth`. Every other declared property is optional and nullable. The Windows builder generally omits unavailable fields rather than inserting null.

| Field | Type when present and non-null | Meaning |
| --- | --- | --- |
| `element_index` | nonnegative integer | Identifier inside this snapshot; **not an array offset**, especially after filtering. |
| `role` | string | UIA control type, e.g. `ListItem`, `Button`, `Edit`; do not invent a closed role enum. |
| `depth` | nonnegative integer | Depth in the rendered tree; can be greater than zero even when `parent_index` is absent. |
| `element_token` | string | Opaque snapshot-bound handle; do not parse or manufacture it. |
| `label` | string | Fallback order in Windows: name, value, automation ID, help text. |
| `value` | string | Separately exposed nonempty UIA value, when available. |
| `enabled` | boolean | Provider-reported state; absence/null means unavailable, not false. |
| `selected` | boolean | Toggle/selection state when the provider exposes it. |
| `actions` | array of strings | Advertised accessibility capabilities; omitted when empty in Windows. Examples include `invoke`, `toggle`, `select`, `expand`, `set_value`, `text`, `scroll`. Merely reading them executes nothing. |
| `parent_index` | nonnegative integer | Nearest **indexed/actionable** ancestor, when one exists. |
| `frame` | object | `{x,y,w,h}` numbers from the UIA bounding rectangle, in **screen coordinates**; only emitted for a meaningful rectangle. |
| `in_web_content` | boolean | Windows emits true for descendants of a UIA Document node; otherwise omitted. |

The portable schema also permits nullable `value_description`, `min`, and `max`. This version's Windows element builder does not populate those fields.

`elements` is **not a lossless raw UIA tree**. The Windows builder excludes rows without an `element_index`. Non-actionable Pane/List ancestors can be present in `tree_markdown` yet absent from `elements`. The live Program Manager sample contains two indexed ListItems at depth 2 with no `parent_index`; the Pane and List are visible only in the Markdown hierarchy. This corrects the oversimplification that every visible tree row has a structured element record.

Tokens and indices are tied to snapshots, not permanent UI objects. A newer
snapshot of the same target supersedes old handles. The optional click consumes
the original selected target without an intervening snapshot; see the cache
contract below.

### Bounds, filtering, and completeness

The default walk limits are 5000 nodes and depth 25. Smaller limits may omit controls. `total_element_count` is the number of indexed elements in the completed bounded walk, not the number of every possible UIA node in the entire application.

The live `query: "Recycle"` sample retained one of two indexed elements, preserving its original index. `element_count` and `total_element_count` remained 2; `returned_element_count` became 1. Both Markdown and structured output were projected.

Matching counts do not prove completeness. `elements_complete:false` remained present even with both observed elements returned. An empty/degraded tree, unavailable provider, timeout, virtualization, and limit-truncated observation cannot prove a control does not exist. The native tool's source uses a four-second snapshot timeout; that is not a guarantee about total CLI round-trip time.

## 5. get_accessibility_tree is desktop discovery

Input: exactly `{}` under the exported schema. It has no `pid`, `window_id`, or tree-depth arguments.

The observed CLI success payload and version-matched Windows implementation have this shape:

```typescript
type DesktopDiscovery = {
  processes: Array<{ pid: number; name: string }>;
  windows: Array<{
    window_id: number;
    pid: number;
    title: string;
    x: number;
    y: number;
    width: number;
    height: number;
  }>;
};
```

PID, HWND and geometry values are integers in this Windows implementation. This is an observed/source-derived shape, **not an advertised output schema**. It does not contain `elements` or a window's UIA controls. Use `list_windows` for richer window metadata and `get_window_state` for the selected window's tree. A separate `list_apps` call is not required for this flow.

## 6. Failures and client handling

The live CLI tests established that **exit code zero is not sufficient**:

| Case | Observed CLI behavior |
| --- | --- |
| Valid window tree read | Exit 0; structured JSON on stdout. |
| Missing `window_id` | Exit 0; plain error text on stdout. |
| HWND belongs to a different observed process | Exit 0; plain error text on stdout. |
| Both include flags false | Exit 0; plain error text on stdout. |
| Driver authorization denial | Exit 1 with diagnostic text on stderr. |

The version-matched CLI implementation prints `structuredContent` when present, otherwise prints text content. It can receive a tool-level error inside a successful daemon transport response without turning it into a nonzero process exit code. This explains the tested cases. Do not assume every error has one JSON shape or that every JSON payload is a success.

For CLI consumption: retain exit code, stdout, and stderr separately; treat a nonzero code as failure; require parseable JSON for these successful read results; verify the expected success fields and requested target identity; inspect any refusal/code/status and degraded/completeness fields. Never replace an error with an empty successful result. Diagnostic strings are for humans, not stable enums.

For MCP consumption: handle JSON-RPC errors first; then check `result.isError`, structured refusals, the expected success shape, and observation-quality fields. This preserves error structure better than parsing CLI text. The saved MCP examples show the actual envelopes; schemas alone do not specify the outer JSON-RPC framing.

## 7. Click execution contract

ctrlloop brings every selected real window forward with native
`bring_to_front({pid, window_id})` before taking the control snapshot. Activation
must confirm the exact target; refusals stop the run and preserve native facts.
The result contract is defined by the versioned
[Windows implementation](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs#L9191-L9218).

Each control selection is followed by one `cua-driver call click`. The input is
`{pid, window_id, element_token}` copied from the selected
native records. If no token is present, it uses `{pid, window_id, element_index,
snapshot_id}` from that same observation. Bare indices and guessed identifiers
are not valid targets. Windows 0.28.2 has no `action: "press"` argument; omitting
button, count, and delivery mode selects the default single left background click.

The native public action result has required `effect` and `route`, with optional
`delivery`, `evidence`, and `escalation`. This is the shape both established by the
source and returned by the isolated host smoke test:

```json
{"effect":"unverifiable","route":"accessibility","delivery":{"mode":"background"}}
```

UIA delivery alone does not establish the user-visible effect. PostMessage uses
`route: "synthetic_events"`. The public action contract does not expose private
transport names or the older `path`/`verified` fields. A confirmed effect requires
evidence; a partial effect carries a delivered count. See the versioned
[ActionResult record](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/cua-driver-contract/src/outputs.rs)
and [action projection](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/cua-driver-core/src/action_record.rs).

Refusals use their native diagnostic shapes. For example, the token helper emits:

```json
{"status":"refused","refusal":{"code":"stale_element_token","message":"element_token is stale; call get_window_state again to refresh"}}
```

Background delivery errors can instead carry a top-level `code`, such as
`background_unavailable`, `background_occluded`, or `background_uipi_blocked`,
with an `escalation.recommended` hint. Keep these payloads intact; an escalation
hint is not an automatic retry. A plain-text failure may still have CLI exit 0.
The adapter handles actions separately from read-result validation and preserves
parsed native failures in `DriverError.context["action_result"]`.

The pipeline records `action_returned` with the native `action_result` on a
non-refused result, including `unverifiable` and `suspected_noop`. This reports
driver facts, not completion of the original request. Native errors return CLI
status `error` with `stage: "click"` and the completed selection. A synthetic
target returns `no_action`. The loop reuses this native action contract and waits after CUA returns,
then observes again and asks Jev about progress. See the authoritative
[main workflow](../../main-workflow.md). The loop does not add typing, coordinate
fallback, or automatic foreground escalation for a refused click.

The [native cache](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/cua-driver-core/src/element_cache.rs)
is scoped to the daemon/runtime, with snapshots indexed by PID and snapshot ID.
Publishing a newer snapshot for the same window replaces the previous one. The
[token contract](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/cua-driver-core/src/element_token.rs)
also sets an eight-window cache limit per PID. Runtime changes, eviction, and
window ownership changes can invalidate a target. CLI process exit cleans up its
disposable session but does not itself retire the daemon's element cache.
Keep observation and dispatch on the existing daemon; switching to bare Windows
MCP would create a different runtime. See the [process model](https://cua.ai/docs/reference/cua-driver/process-model).

The isolated host test used the production `CuaDriver` adapter and a disposable,
nonactivating WinForms Test button. One `list_windows`, one `get_window_state`,
and one `click` ran in separate CLI processes. The fixture's callback count was
exactly one and it exited afterward; the harness made zero post-action driver
reads. The callback established this isolated click's effect without adding
runtime observation to the pipeline. The local result is in ignored
`reports/isolated-click-result.json`. The sandbox's separate read returned
`pipe_access_denied`; authorized host execution reached the installed daemon.

## Version-matched implementation references

- [Windows tool definitions and output builders](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs): `ListWindowsTool`, `GetWindowStateTool`, `build_element_entry`, `GetAccessibilityTreeTool`, and `ClickTool`.
- [Windows delivery refusals](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/input/delivery.rs): background error codes and diagnostic hints.
- [UIA walker and node fields](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/uia/mod.rs): limits, role names, screen-coordinate rectangles, nearest indexed ancestor.
- [Portable input/output records](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/cua-driver-contract/src/windows.rs): portable types and optional fields.
- [CLI response serialization](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/cua-driver/src/cli.rs): `call` flattens structured results and falls back to text.
- [MCP result normalization](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/cua-driver-core/src/mcp_result.rs): output-schema and error-envelope handling.

Review and regenerate these artifacts when updating CUA Driver. A contract verified for 0.28.2 is not an assertion that later releases serialize identically.
