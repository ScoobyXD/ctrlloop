# Captured Windows examples

These are **sanitized live outputs from CUA Driver 0.28.2**, captured on Windows on 2026-09-21. They are examples, not live identifiers to reuse. Request/response key names, value types, optional-field omissions, element ordering, and tree relationships are preserved.

Sanitization and excerpting:

- The captured Explorer process ID is consistently replaced with numeric `4242`; the Program Manager window ID with `123456`; its separate minimized File Explorer window with `123457`. The mismatched process ID is replaced with `4343`.
- `list_windows.output.json` retains two Explorer window records from the original twelve, with the same two entries retained in `_legacy_windows`. The File Explorer title is replaced with `<folder title> - File Explorer`.
- `get_accessibility_tree.output.json` retains the one Explorer process and its two windows from the original 348 processes and twelve windows. Omitted records are not represented by invented JSON fields or ellipses.
- The Program Manager control labels, frames, tree text, snapshot IDs, and element tokens are unchanged. This window exposed only the standard **Recycle Bin** and **Learn about this picture** items. No private application document or message contents were read for a tree sample.
- The raw captures remain outside the repository, in a temporary local directory. They are not documentation artifacts.

## CLI examples

For each `NAME.input.json`, the observed invocation was JSON on stdin to `cua-driver call TOOL`, where TOOL is `list_windows`, `get_accessibility_tree`, or `get_window_state`. Use freshly enumerated IDs in real requests.

| Files | What was actually observed |
| --- | --- |
| `list_windows.input.json`, `list_windows.output.json` | Empty-object input; structured window inventory on stdout. |
| `get_window_state.input.json`, `get_window_state.output.json`, `get_window_state.tree.md` | Native Program Manager UIA read; screenshots disabled; 100-node/depth-8 bounds. |
| `get_window_state.query.input.json`, `get_window_state.query.output.json`, `get_window_state.query.tree.md` | Same window filtered to `Recycle`; one returned element, two total elements, original index preserved. |
| `get_accessibility_tree.input.json`, `get_accessibility_tree.output.json` | Lightweight process/window discovery, not the selected window's controls. |
| `get_window_state.missing_window_id.*` | Missing required `window_id`: plain-text stdout, exit code 0. |
| `get_window_state.mismatched_owner.*` | Window owned by a different process: plain-text stdout, exit code 0. |
| `get_window_state.both_false.*` | Both capture options disabled: plain-text stdout, exit code 0. |

Successful CLI stdout is the structured payload itself, without an MCP `content`/`structuredContent` wrapper. These three validation failures instead produce **plain text** on stdout. A successful process exit alone therefore does not establish a successful tool call or even JSON output.

## MCP examples

The `mcp.*.input.json` and `mcp.*.output.json` files preserve actual JSON-RPC requests/responses through a stdio MCP client connected to the existing daemon with `cua-driver mcp --socket \\.\pipe\cua-driver`. The client performed the normal initialize handshake first; it did not start a second standalone runtime.

- `mcp.get_window_state.*` shows `result.content` containing a text block and `result.structuredContent` containing the structured payload. On this successful response, `isError` is absent; it is not explicitly `false`. There is no image block because screenshots were disabled.
- `mcp.get_window_state.missing_window_id.*` shows the tool-level failure: `result.isError: true`, diagnostic text in `result.content`, and `result.structuredContent.code: "tool_invocation_failed"`. This particular error is not a top-level JSON-RPC `error` object.

## Limits demonstrated by the capture

- `elements` is flat and contains indexed rows. The unindexed `Pane` and `List` ancestors are present in Markdown only. Both returned elements have `depth: 2`, and neither has `parent_index` or `value`; do not invent missing values or a parent record.
- Both full and filtered samples report `elements_complete: false`. The filtered response keeps `element_count: 2` and `total_element_count: 2` while `returned_element_count` becomes 1.
- The calls use separate CLI lifecycle sessions. Each snapshot receives a different `snapshot_id`; its element tokens must not be treated as durable control identifiers.
- These samples verify enumeration and native UIA reading only. They do not verify any action or screenshot behavior.
