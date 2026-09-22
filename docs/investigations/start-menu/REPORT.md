# Windows Start UIA investigation

Observed 2026-09-21 on this Windows desktop with installed **CUA Driver 0.28.2**. Application behavior and the main checkout were not changed; no Jev requests, search queries, unrelated launches, settings changes, or application action loop were added.

This report records the initial investigation, before the synthetic Start Menu → Search selection route was added. Pipeline descriptions below refer to that earlier version; see the [current selection behavior](../../../README.md) for the subsequent implementation.

**Yes: the real open Start menu and its search input are accessible through the installed driver.** Once Start was opened, normal `list_windows → get_window_state` returned its real window and 80 indexed controls. A synthetic Jev candidate is unnecessary for an already-open Start window. Reaching Start from the closed state is a separate discovery/action problem.

| Check actually performed | Observation |
| --- | --- |
| Real taskbar Start while closed | Direct read-only UIA found `Start`, role `Button`, automation ID `StartButton`, class `ToggleButton`, owner `explorer` PID 8700. Patterns: Toggle and ScrollItem. Enabled and keyboard-focusable. It was **offscreen with invalid/infinite bounds** while the taskbar was collapsed; this is not a usable pixel target. |
| Driver discovery while closed | Both `list_windows({})` and `get_accessibility_tree({})` returned 13 windows, omitting Start, Search, and the taskbar. The actual visible, uncloaked taskbar HWND 131360 had class `Shell_TrayWnd` and an empty caption. |
| Exact taskbar snapshot | `get_window_state({pid:8700,window_id:131360,...})` failed with `tool_invocation_failed`: “No window with window_id 131360 exists.” The HWND was real; this is filtered discovery validation, not proof of absence from Windows. |
| Open Start discovery | After the authorized desktop Ctrl+Esc operation, `list_windows({})`, `list_windows({on_screen_only:true})`, and `get_accessibility_tree({})` returned `Start`, `StartMenuExperienceHost.exe`, PID 7908, HWND 66094. Discovery contained 15 windows. |
| Fresh Start accessibility snapshot | Explicit PID/HWND snapshot, 5,000-node/depth-25 limits, no query filter: 80 indexed controls. The accompanying window image matched the reference's search field, pinned apps, recommendations, and mobile-device panel. |
| Restoration | Final `restored-stable-list.json` returned 13 windows with no Start/Search. A fresh desktop image showed Start closed and the original Codex app visible. |

Selected real Start controls from snapshot `s00000014`:

| Name | Role | Advertised capabilities |
| --- | --- | --- |
| Search box | Edit, enabled | `set_value`, `text` |
| Search for apps, settings, and documents | Button | `toggle` |
| Show all pinned apps | Button | `invoke` |
| Pinned | List | `scroll` |
| Microsoft Edge / Word / Excel / PowerPoint | ListItem | `invoke` |
| Recommended | List | `scroll` |
| Show all | Button | `invoke` |
| Power | ListItem | `invoke` |

Capabilities were observed, **not exercised**. No text was entered. The driver assigns the snapshot to the outer Start window; its element rows do not identify each descendant's process. The parent task independently reported a focused `Search box` owned by `SearchHost`, consistent with cross-process UIA, but this report does not infer that ownership solely from the Start snapshot.

## Where the missing observations originate

The installed `get_accessibility_tree` contract describes lightweight process/window discovery. Its live payload had only `processes` and `windows`, without a desktop control tree. `get_desktop_state` is screenshot-only. Neither provides a substitute full desktop UIA hierarchy.

Release source was checked against tag `cua-driver-rs-v0.28.2`, commit `fc188250b4ca8549b8e61f937fdb1fb560770e86`:

- Both the [Win32 enumeration](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/win32/windows.rs#L268-L295) and [UIA desktop-child enumeration](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/uia/windows_enum.rs#L914-L1018) exclude empty Win32 captions. There is no special Start/Search exclusion in those paths. This explains the missing untitled taskbar.
- [`get_window_state` validates through that filtered list](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs#L1281-L1301), explaining rejection of the real taskbar HWND. [`get_accessibility_tree` uses the same window list](https://github.com/trycua/cua/blob/cua-driver-rs-v0.28.2/libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs#L8682-L8726).
- Our [adapter](../../../src/driver.py) passes every discovered window through and permits only `list_windows` and `get_window_state`. The [pipeline](../../../src/pipeline.py) creates a candidate for every returned window, then snapshots only the selected window. It does not filter Start or invent it. The current reads cannot open a closed menu.

The initial sandbox probe had a separate `CodexSandboxDesktop`, zero windows, and denied daemon-pipe access. An approved host probe reached `Default`, Session 1, and the existing daemon. That access restriction was not treated as missing UI. No permission settings or daemon mode were changed.

## Supported path and limits

**Simplest supported path:** with Start open, enumerate windows, retain the returned Start PID/HWND, and snapshot that exact pair. Feed the resulting real window/control observations into the existing selection code. From a closed state, the current two-read adapter cannot complete this path. The driver can open Start using an explicitly authorized desktop action; alternatively, improving untitled shell-window discovery would be upstream driver work. Neither behavior change was implemented.

For the comparison, one persistent MCP connection to the existing named-pipe daemon retained transport lifecycle across reads and minimal keyboard operations. No snapshot-bound element action was used, so token-action validity was not empirically tested. Existing one-shot CLI processes must not be assumed to retain targets. Ctrl+Esc opened Start; background Escape returned `unverifiable` with a foreground escalation hint. Desktop Escape was followed by delayed dismissal. Immediate `restored-list.json` still contained Start; **only the later stable observation proves restoration**. No pointer click followed.

`elements_complete:false` remained present. The 80 controls prove positive availability, not exhaustiveness. Independent direct HWND enumeration during the open state unexpectedly returned empty; those diagnostic results are inconclusive, and the driver snapshot is the positive open-state evidence. A separate Search results window and text entry were not tested.

Historical main-checkout reports were inspected read-only: seven unique real runs, one offline fixture, and one JSON/JSONL duplicate. The seven runs lacked Start/Search in discovery and first-round choices. Notepad++'s `Search` menu item is unrelated. Those reports alone establish no UIA inability.

## Evidence and reproduction

- [Selected live observations](observations.json): relevant window/control rows and action results, with unrelated windows and personal recommendation names omitted. IDs/tokens are historical, not reusable.
- Local-only raw [Start snapshot](raw/start-snapshot.json) and [window image](raw/start-snapshot-1.png). Raw evidence is ignored by Git because it contains personal UI content.
- Final restoration: [window list](raw/restored-stable-list.json), [visual observation](raw/desktop-before-restore-click-0.png). The latter filename reflects a contemplated click that was **not performed** once the image showed Start closed.
- [Read-only driver reproduction](../../../scripts/capture_start_driver.py): manually open Start, then run `python scripts/capture_start_driver.py --output docs/investigations/start-menu/raw/reproduction --screenshot` from an authorized interactive host context. It selects only an actually discovered Start record, without opening UI or synthesizing candidates. Syntax-checked; the underlying read contracts were exercised live by the investigation client.
- [Read-only direct UIA diagnostic](../../../scripts/inspect_start_uia.ps1): bounded ControlView walks for shell windows, recording names, roles, pattern availability, and geometry; no UIA actions or value reads. Its closed-state result established the real Start button. Later empty results remain an explicit diagnostic limitation.

Validation: saved evidence parsed successfully; assertions checked the 80-control count, Search box capabilities, and final absence of Start/Search. Python syntax and whitespace checks passed. No application code was changed.
