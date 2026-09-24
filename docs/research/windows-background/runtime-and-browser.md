# Runtime and browser isolation: findings for ctrlloop

Research date: 2026-09-24. This note audits ctrlloop and the local CUA Driver 0.28.2 source, then compares current primary documentation. It is research, not a claim that a VM, hidden desktop, browser worker, or minimized-viewer configuration was exercised here. No GUI operations, runtime changes, tests, or Jev questions were introduced.

## Decisive finding

Multiple task cards can represent independent work, but **multiple CUA sessions do not create multiple Windows desktops**. Independent full-desktop automation requires an execution boundary underneath the task: ordinarily an agent-owned interactive Windows session or VM. Browser-only work can often use headless browsers and separate browser contexts instead. These are different resource classes with different compatibility, login, and persistence properties.

For the existing Jev/native-accessibility loop, the lowest-disruption architectural proof is to run the existing worker and patched driver **inside one agent-owned Windows guest**, keeping the guest desktop available while a host-side viewer is minimized. Foreground actions inside the guest then change the guest's focus, not the user's host focus. Scale to two guests only after proving one. This is a recommendation inferred from the source audit, not a completed deployment.

## What the repository actually does

The local source reports version `0.28.2`, commit `fc188250b4ca8549b8e61f937fdb1fb560770e86`. At inspection, that checkout had a preexisting modification only in `platform-windows/src/input/keyboard.rs`. It is not itself proof that the separate built binary includes ctrlloop's direct-accessibility patch. The tracked build script and patch are the reproducible source of that stronger contract. The repository documents the separate build and its prior manual results in [direct accessibility activation][direct-doc].

| Existing component | Audited behavior | Consequence |
| --- | --- | --- |
| [Driver protocol][pipeline] | Native window dictionaries, native control dictionaries, `click`, `type_text`, `hotkey`, `bring_to_front` | Preserve this interface for a native Windows worker. |
| [CuaDriver constructor][driver] | Accepts an injected `transport` or explicit `socket` | There is already a seam for choosing a worker connection. |
| [McpTransport][transport] | Starts `cua-driver mcp --socket ...`, with one persistent client per run and serialized calls | It selects a daemon endpoint; it does not allocate a desktop. |
| `pipeline._activate_window` | Calls `bring_to_front`, or desktop `Win+S` for the synthetic Search target | The current loop deliberately changes its execution desktop's focus. |
| `_FOREGROUND_TYPING_APPS` | Defaults several browsers to foreground text delivery | `--delivery-mode background` alone does not eliminate takeover. |
| `get_window_state` | Requests accessibility but sets `include_screenshot: False` | A viewer should not be implemented by changing the meaning of the selection snapshot. |
| [Direct click patch][click-patch] | Direct UIA/MSAA activation, explicit refusal of synthetic clicking, but still cursor overlay calls | Accessibility-only activation is not invisibility or desktop isolation. |

The transport configures a one-millisecond cursor glide. Its own comment says this bounds animation wait rather than hiding the cursor. The click patch calls `pin_overlay_above`, `overlay_glide_to`, and `ClickPulse`. Moving the driver into the guest naturally confines these effects to the guest; hiding just the host task window would not.

The direct patch refuses a minimized target before activation. Meanwhile some upstream descriptions still broadly claim minimized-window support. For this project, the concrete patch and observed capability contract take precedence over those generic descriptions. **Minimize the workspace viewer, while leaving target applications restored inside the workspace.**

The [Hermes launcher](C:/Users/Jonat/ctrlloop/plugins/hermes/__init__.py:15) already starts the CLI with `CREATE_NO_WINDOW`, writes its output under an individual launch directory, and immediately returns `status: launched`, the PID, and that directory. Its registered description explicitly promises only a launch receipt, with no completion reporting or Stop, and prohibits overlapping computer-use actions. A hidden console therefore already exists; safe independent desktop execution does not.

A sidebar experience additionally needs scheduling, status observation, cancellation, worker assignment, and artifact retrieval around the current loop. Reuse its existing report/output rather than inventing a second action model. The pipeline's `iteration_limit` explicitly says task completion was not assessed. A child process exiting, its last action returning, and the user's requested result being verified are three distinct events. A future task UI must preserve those distinctions; it cannot label every exited process “completed successfully.”

## CUA sessions, concurrency, and stale observations

[Current CUA process-model documentation](https://cua.ai/docs/reference/cua-driver/process-model) explicitly distinguishes lifecycle sessions from operating-system isolation. It documents multiple clients sharing screen, pointer, keyboard, accessibility, and focus. A private implicit transport identity scopes cursor, recording, configuration, and cleanup; a public session label is not an independent computer or a credential.

The pinned source independently supports the narrower claim. [serve.rs][serve] resolves public labels and private transport lifecycle identifiers separately. [mcp_http.rs][mcp-http] creates a private identity for each TCP connection and ends its sessions when that connection closes. Reusing a string such as `research-1` on another transport is not a resume protocol. A viewer must not own the execution connection, or closing the viewer could terminate the driver's lifecycle unexpectedly.

The native snapshot cache deserves special attention. [ElementCacheCore][element-cache] stores snapshots by process with a window identity; publishing another snapshot removes the previous snapshot for that same window. Resolution rejects stale tokens and checks runtime generation. Separate clients using the same daemon/runtime can therefore interfere even if each has its own cursor: task A observes a window; task B observes that same window; A's old token becomes stale. This is a concrete reason to assign a native window to one active worker, and to keep passive viewer capture away from `get_window_state` unless coordinated with the worker.

The [pinned input coordinator][tool-core] has a process-wide desktop action mutex and text-input admission keyed by PID. This can serialize individual dispatches or refuse overlapping text. It is not a transaction covering the sequence “observe, Jev chooses, type, observe.” Nor does a process-local mutex coordinate independent daemon processes. Launching four daemons on four pipes in the same interactive desktop creates neither four keyboards nor four safe foreground lanes.

Within one guest, use one worker as the owner of foreground desktop actions. It can perform network/model work concurrently where useful, but application interactions should remain ordered. For independent GUI tasks, assign independent guests or properly isolated interactive sessions. Different files, browser accounts, and server-side records can still conflict even when their desktops are separate; desktop isolation solves input contention, not all business-data concurrency.

## Where the Windows driver must live

The pinned [Windows diagnostics][diagnostics] checks the Windows session ID, process window station, desktop information, and foreground availability. It rejects Session 0 as a desktop runtime location. The source's doctor explicitly notes that window enumeration is scoped to the calling session's desktop. [Current CUA integration documentation](https://cua.ai/docs/how-to-guides/driver/connect-your-agent) likewise distinguishes controlling the host desktop from provisioning an isolated computer.

This means an SSH process or service can supervise a worker but must not be assumed to gain a logged-in GUI merely because it can launch an executable. The UI-owning daemon belongs in the intended interactive session. A guest-local scheduler or supervisor can manage that process; provisioning, interactive sign-in, display availability, lock behavior, and process integrity still need a measured qualification.

A source search did not establish an implemented `CreateDesktop`/`SetThreadDesktop` execution backend in this pinned Windows driver. Its `virtualdesk.rs` name is misleading for this question: the file normalizes coordinates over the combined multi-monitor screen rectangle using `MOUSEEVENTF_VIRTUALDESK`; it does not implement isolated workspaces. An inactive Win32 desktop would therefore be a separate engineering investigation, not a configuration switch we have discovered.

An agent-owned VM is attractive because the ordinary native pipeline can remain foreground-capable **inside its own desktop**. It does not automatically solve guest locking, remote-viewer graphics suppression, sleep, permissions, or elevated prompts. These are explicit validation boundaries, not reasons to send input back to the user's main desktop.

## Transport and remote control

The current Python adapter passes an explicit Windows named pipe to the local `cua-driver mcp` process. It preserves the native MCP result envelope and reuses the existing `DriverError`; it never starts or stops the daemon. It also checks the connected daemon's click schema before the first click because the client executable's version alone does not identify the running server's capabilities. Retain that check in guests.

The pinned server also contains HTTP MCP, enabled by `CUA_DRIVER_RS_MCP_HTTP_PORT`, requiring `CUA_DRIVER_RS_MCP_HTTP_TOKEN`. It binds to `127.0.0.1`, not an arbitrary network interface. Requests on a TCP connection are ordered; connections can execute concurrently. This is a useful existing transport, but it is not a host-to-guest routing service. A remote design needs an authenticated connection path and clear connection lifetime; casually replacing the pipe string with a VM address would not implement it.

Two reasonable placements preserve the action contract:

1. **Worker inside guest:** run ctrlloop, its MCP client, and CUA daemon together; host supervises the run and receives existing reports/artifacts. This is the smallest proof because the current local transport remains unchanged.
2. **Controller on host, driver in guest:** adapt transport using the same native MCP tools/results, with a secured guest connection and explicit lifecycle ownership. This concentrates model calls and scheduling but adds transport/reconnection work.

Do not replay an uncertain action after reconnect. The current transport already discards failed connections without replaying a command; preserve that behavior. Recover by observing current guest state and then deciding what remains to be done.

## What Cua Sandbox adds—and what it does not prove

Cua distinguishes its desktop driver from its sandbox runtime. [Local Sandboxes](https://cua.ai/docs/local-sandboxes) represents an isolated computer running on the user's hardware. [How sandboxes work](https://cua.ai/docs/concepts/how-sandboxes-work) explains separate GUI and code capabilities sharing the guest filesystem, with Windows supplied as a VM rather than a GUI Windows container.

The [versioned runtime matrix](https://cua.ai/docs/reference/sandbox-sdk/runtime-support) is unusually useful: it documents `cua-sandbox 0.7.0`, exported by `cua 0.1.6`, and distinguishes image acceptance from boot verification. Default `Image.windows()` means Windows Server 2022; local selection prefers Hyper-V when available, with QEMU alternatives. `Image.windows('11')` uses a local installation path and has no built-in Fleet mapping in that version. Neither constructor proves that this user's host or a particular guest image is ready. `Sandbox.snapshot()` is explicitly unimplemented in the documented local and Fleet paths. Hypervisor checkpoint capability must not be confused with a working SDK snapshot method.

[Local lifecycle documentation](https://cua.ai/docs/how-to-guides/sandbox/manage-local-lifecycle) separates connection lifetime from computer lifetime: persistent creation can outlive the script; disconnect keeps it running; destruction removes it. Ephemeral context exit destroys the sandbox. That distinction should map directly to product behavior: minimizing or closing a viewer is not “destroy workspace,” and a task finishing is not automatically “delete all guest state.”

[Parallel sandbox guidance](https://cua.ai/docs/how-to-guides/sandbox/scale-out) demonstrates multiple async local jobs and bounded concurrency. Capacity consumes host CPU, memory, and disk. Its examples are Linux-based patterns, not measurements of Windows 11 guests on this computer; do not derive a promised task count from the examples.

[Fleet-to-Driver documentation](https://cua.ai/docs/how-to-guides/sandbox/control-the-desktop-with-cua-driver) requires a qualified guest service advertising the typed Driver envelope extension; an arbitrary MCP endpoint is insufficient for that typed SDK route. It also separates shell/file/terminal APIs from the desktop driver. This is evidence to evaluate an existing integration, not justification to replace ctrlloop's stable transport with unverified latest SDK methods. Pin the actual sandbox package, driver build, image identity, and guest OS together.

## Browser-only execution can be much lighter

[Chrome Headless](https://developer.chrome.com/docs/automation-and-testing/headless) runs without displaying platform windows. It is not merely a normal browser that happens to be minimized. This is the cleanest invisible route when the task's complete workflow is web content and can use a browser-native automation channel.

[Playwright browser contexts](https://playwright.dev/docs/browser-contexts) isolate cookies and web storage and can coexist within one browser. Use one context per independent task/account state, or separate browser processes when process-level fault separation is required. Contexts are browser isolation, not Windows desktops: they do not provide Excel, native file dialogs, system-tray applications, or the user's existing unsaved app instances. A preview can be built from browser screenshots or a viewer, but the actual task must not depend on that preview being visible.

The [Playwright BrowserType reference](https://playwright.dev/docs/api/class-browsertype) documents persistent profiles, headless launch, and connection choices. Multiple processes cannot concurrently launch the same user-data directory. Its default Chrome profile is unsupported for automation; create task-owned profiles. CDP attachment is Chromium-only and lower fidelity than Playwright's own protocol. A browser route is therefore an additional supported capability, not a claim that every native interaction can be translated automatically.

[Chrome's remote-debugging change](https://developer.chrome.com/blog/remote-debugging-port) requires a nonstandard user-data directory for the remote-debugging switches from Chrome 136, with Chrome for Testing recommended for automation. This limits the tempting “just attach invisibly to my everyday browser” shortcut. Authentication needs explicit provisioning and persistence. [Playwright authentication guidance](https://playwright.dev/docs/auth) also explains that shared accounts can still interfere through server-side state and that saved authentication material is sensitive. A copied browser context does not create a separate copy of the user's cloud document.

There is already relevant capability in pinned CUA: [its browser guide][browser-guide] describes `browser_prepare`, driver-owned `isolated_new`/`isolated_named` profiles, and session-scoped tab/ref capabilities. The flow binds through an exact native window; its presence does not establish a headless runtime. ctrlloop currently exposes no browser/DOM actions, so adopting this would be a consciously chosen extension, not an invisible implementation substitution. [Current background-browser documentation](https://cua.ai/docs/concepts/browser-targeting-and-background-delivery) further distinguishes trusted CDP input from synthetic DOM events, and requires fresh effect verification. Native dialogs and browser setup can still surface visible UI; keep those inside the guest if the strict host-invisibility promise must hold.

## Why Windows containers do not solve the native-app requirement

Microsoft's [Windows container migration guidance](https://learn.microsoft.com/en-us/virtualization/windowscontainers/quick-start/lift-shift-to-containers) excludes applications requiring an interactive desktop/GUI and RDP sessions. A Docker-wrapped QEMU Windows runtime contains a **VM**, not a newly supported GUI Windows container. Linux containers with a Linux desktop and remote display are another valid architecture, but do not supply Windows UIA or arbitrary Windows native-app compatibility.

## Manual proof sequence before implementation commitment

This is a proposed manual qualification sequence under the repository's no-new-tests rule, not work already performed:

1. Qualify one guest image and session. Record OS edition/build, driver commit and binary hash, transport identity, app versions, viewport size, and display backend. Confirm the driver sees only the intended guest's windows.
2. Run an existing small Jev loop unchanged inside the guest. Exercise native discovery, control observation, direct activation, targeted typing, and a post-action observation using fresh native tokens.
3. Keep the host foreground application active while the guest works. Observe host focus, pointer, keyboard input, overlays, taskbar changes, and clipboard behavior throughout; before/after snapshots alone miss transient takeover.
4. Minimize the viewer for several complete loops; restore it and verify fresh timestamps, changed guest state, and correct artifact output. Repeat with the viewer closed if the selected viewer supports independent connection lifetime. Keep the guest app restored.
5. Repeat with two separate guests and distinguish their artifacts and reports. Intentionally give similar window titles to prove routing uses execution identity plus native target, not titles or recycled host PIDs.
6. Exercise viewer disconnect, guest lock, guest process failure, and transport loss. Verify clear existing errors, no host fallback, and no automatic replay of a possibly delivered action. Independently qualify whether the chosen display/session survives each condition.
7. Measure RAM, CPU, disk, Jev call duration, action latency, and total loop time at one and two workers. Choose a bounded worker count from measurements, not from the number of sidebar cards.

Reuse the current native records, `Driver` protocol, `RunReport`, snapshot tokens, and refusal codes. Add execution placement and lifecycle management around them; avoid duplicate window/control/action schemas. No new Jev question is required for the first guest proof. If driver changes become necessary, follow the user's explicit preference: “maybe we should do driver changes in main because merging them over from a worktree sucks”.

[pipeline]: C:/Users/Jonat/ctrlloop/src/pipeline.py
[driver]: C:/Users/Jonat/ctrlloop/src/driver.py:149
[transport]: C:/Users/Jonat/ctrlloop/src/cua_mcp.py:69
[direct-doc]: C:/Users/Jonat/ctrlloop/docs/cua-driver/direct-accessibility-only.md
[click-patch]: C:/Users/Jonat/ctrlloop/scripts/patches/cua-driver-0.28.2-direct-accessibility-only.patch
[serve]: C:/Users/Jonat/ctrlloop/.local/cua-driver/source/libs/cua-driver/rust/crates/cua-driver/src/serve.rs:168
[mcp-http]: C:/Users/Jonat/ctrlloop/.local/cua-driver/source/libs/cua-driver/rust/crates/cua-driver/src/mcp_http.rs:56
[element-cache]: C:/Users/Jonat/ctrlloop/.local/cua-driver/source/libs/cua-driver/rust/crates/cua-driver-core/src/element_cache.rs:35
[tool-core]: C:/Users/Jonat/ctrlloop/.local/cua-driver/source/libs/cua-driver/rust/crates/cua-driver-core/src/tool.rs:74
[diagnostics]: C:/Users/Jonat/ctrlloop/.local/cua-driver/source/libs/cua-driver/rust/crates/platform-windows/src/diagnostics.rs:178
[browser-guide]: C:/Users/Jonat/ctrlloop/.local/cua-driver/source/libs/cua-driver/rust/Skills/cua-driver/BROWSER.md:106
