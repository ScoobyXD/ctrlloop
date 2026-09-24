# Windows UI isolation: what an agent workspace actually is

Research date: September 24, 2026. Scope: Windows architecture and its implications for one or several computer-use agents. This is a research note; no desktops, sessions, services, registry settings, or application instances were created or changed.

**The key engineering distinction is between concurrent reasoning and concurrent GUI input.** Many tasks can plan, call Jev, read files, or await network responses simultaneously. Several tasks that use an ordinary Windows mouse and keyboard need independent input environments or serialization. Merely giving each task a sidebar entry does not create that independence. The practical architecture is a task coordinator plus execution workers, with an optional viewer of each worker. That architecture is a recommendation derived from the constraints below, not an existing Windows API called an “agent workspace.”

## The layers that are often confused

| Layer | What it supplies | Relevance to the requirement |
|---|---|---|
| Task/process | A unit of work, possibly running concurrently | Does not by itself isolate foreground focus, pointer, or clipboard. |
| Task View virtual desktop | A grouping of visible/hidden windows | Windows remain on the same underlying desktop. Foregrounding a window can switch the user's virtual desktop. [Microsoft explanation](https://devblogs.microsoft.com/oldnewthing/20171002-00/?p=97116) |
| Win32 desktop (`HDESK`) | A logical UI surface containing windows, menus, and hooks | Provides UI-object separation, but only one desktop in an interactive window station is the active input desktop. [Desktops](https://learn.microsoft.com/en-us/windows/win32/winstation/desktops) |
| Window station | A process-associated container for desktops, a clipboard, and an atom table | Clipboard isolation lives here, rather than at the individual desktop level. [Architecture overview](https://learn.microsoft.com/en-us/windows/win32/winstation/about-window-stations-and-desktops) |
| Windows interactive session | Its own interactive window station named `WinSta0` | An RDS session can have a separate input/display environment. Other window stations in that session are noninteractive. [Window stations](https://learn.microsoft.com/en-us/windows/win32/winstation/window-stations) |
| Virtual machine | A separate OS instance | A candidate execution boundary for broad application compatibility; viewer behavior and guest session state still need validation. VM configuration is covered in the companion research. |

The Task View distinction matters particularly here: hiding a task's windows is useful presentation, but it is not a second mouse and keyboard. Microsoft's explanation also states that programs on a noncurrent virtual desktop continue consuming resources normally; switching away does not suspend them or save their RAM. Thus “the process kept running” does not establish that an input-driven loop remained operable. [Virtual desktops and performance](https://devblogs.microsoft.com/oldnewthing/20200421-00/?p=103689)

## Why ordinary input is the bottleneck

`SendInput` inserts synthetic events into Windows' keyboard/mouse input stream. Its interface does not accept a target desktop or window. A batch is inserted without other input being interspersed inside that batch, but this does not reserve a desktop across a whole observe–decide–act loop. Existing pressed keys can also affect injected input. Windows integrity rules permit injection only into equal or lower integrity applications. [SendInput reference](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)

The cursor API is even more explicit: the cursor is shared, and `SetCursorPos` requires the caller's current desktop to be the input desktop. Switching the calling thread to a hidden desktop does not manufacture another independently controllable pointer. [SetCursorPos reference](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setcursorpos)

Consequently, two loops can interleave model calls on one desktop but must not independently assume they own focus and pointer state. A scheduler could serialize GUI transactions, yet that still interferes with a human using that same interactive desktop. For generic physical-style automation while the human works, the recommendation is an independent interactive session/VM per concurrently acting worker. This conclusion is an architectural inference from the input constraints, not a claim that each task must permanently own a VM.

Posting `WM_KEYDOWN` or `WM_CHAR` messages is not a universal background-input replacement. Microsoft explains that posted messages skip the real input queue and associated keyboard hooks; applications may detect the mismatch. Such techniques can work with particular controls, but must remain explicit application-specific capabilities. [Microsoft's PostMessage analysis](https://devblogs.microsoft.com/oldnewthing/20250319-00/?p=110979)

## What a hidden Win32 desktop can actually do

Windows exposes a real desktop lifecycle. `CreateDesktopW` creates a desktop in the calling process's window station. Creation can specify access rights, security descriptor, and handle inheritance; a repeated name can return an existing object. `CreateDesktopEx` adds explicit desktop-heap sizing. These calls create UI infrastructure, not another Windows user, a browser profile, or an OS instance. [CreateDesktopW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-createdesktopw)

A process can be launched against a named desktop using `STARTUPINFO.lpDesktop`, including a `windowStation\\desktop` path. This is the relevant launch-time connection point, not a command to move an existing application's complete running UI elsewhere. [STARTUPINFO](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfow)

Thread placement has rules that affect implementation. Explicit `SetThreadDesktop` takes priority; inherited desktop handles and launch information affect default placement. Inheriting multiple desktop handles makes the selection undefined, so a worker launcher needs deliberate handle inheritance. [Thread connection rules](https://learn.microsoft.com/en-us/windows/win32/winstation/thread-connection-to-a-desktop)

`SetThreadDesktop` cannot switch a thread that already owns windows or hooks on its current desktop, unless the destination is that same desktop. A clean worker process or a carefully initialized worker thread is therefore easier to reason about than relocating a long-lived GUI/UIA thread. The destination must belong to the process's window station. [SetThreadDesktop](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setthreaddesktop)

Enumeration is possible without making a desktop the active one: `EnumDesktopWindows` accepts the desired `HDESK` and requires read-object access. This establishes an API for finding that desktop's top-level windows; it does not guarantee that every application's accessibility provider or rendering path will work when inactive. [EnumDesktopWindows](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumdesktopwindows)

Calling `SwitchDesktop` makes a desktop visible and activates input there. That action would violate the user's noninterference requirement if applied to their interactive window station during background work. Window messages and hooks are scoped to the same Win32 desktop, so the proposed worker should be colocated with the applications it automates. [Desktop boundaries](https://learn.microsoft.com/en-us/windows/win32/winstation/desktops)

## UI Automation can avoid input, but support is per capability

UIA clients retrieve properties and invoke functionality exposed by application providers. Standard Win32, Windows Forms, and WPF controls have supplied providers; custom controls need suitable provider implementations. Without a provider or proxy, available information can be limited to basic window properties. Thus an accessibility tree that identifies a button does not automatically prove every useful operation is available programmatically. [UIA providers](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-providersoverview)

Control patterns expose individual behaviors: for example, Invoke, Toggle, Selection, Scroll, and Value. An agent can request the behavior rather than moving the cursor to approximate it. This supports a modular driver in which the existing action contract maps to the strongest available semantic operation. [Control patterns](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternsoverview)

Microsoft's current `winapp ui` documentation illustrates the boundary concretely: pattern-driven inspection and actions are described as suitable for headless or locked-session use, whereas injected clicks, drag, hover, wheel, touch, pen, and `send-input` require an unlocked interactive desktop and foreground target. Its value-setting documentation also identifies WinUI 3 `RichEditBox` and WPF `RichTextBox` as examples that lack a writable value path. This is strong evidence for a capability distinction, not proof that our driver can already run every action on a custom inactive desktop. A product may impose additional session restrictions around otherwise available provider operations. [WinApp UI automation](https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/ui-automation)

Do not initialize or call UIA casually from the viewer's UI thread. Microsoft recommends a separate MTA thread that owns no windows, because UIA's use of Windows messages can otherwise cause slowdowns or hangs. This should inform any future worker implementation. [UIA threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)

A minimized application, an occluded application, an inactive desktop, a locked session, and a minimized viewer are different states. None should be silently substituted for another in capability claims. In particular, Microsoft documents per-window WGC capture while occluded, but that does not establish equivalent capture from every minimized window or inactive desktop. [Capture behavior](https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/ui-automation)

`PrintWindow` is another capture option, but the application processes a rendering request and draws the result. It is synchronous and can block. A successful hidden-window image from one app does not establish a general-purpose live viewer for all apps. [PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow)

## UI separation and security separation are different

Multiple desktops inside one `WinSta0` share its clipboard and atom table. Creating a separate window station gives a separate container, but only the interactive `WinSta0` can receive normal input or display UI. A hidden-desktop implementation must therefore avoid treating copy/paste as private by default. [Window-station architecture](https://learn.microsoft.com/en-us/windows/win32/winstation/window-stations)

Window-station access rights include separate permissions for clipboard use, screen reads, desktop creation, and attribute changes. This shows that these resources are protected at that object's boundary; it is not equivalent to assigning a new account or limiting filesystem access. [Window-station access rights](https://learn.microsoft.com/en-us/windows/win32/winstation/window-station-security-and-access-rights)

A desktop has its own ACL and access rights for enumeration, reading/writing UI objects, hooks, and switching. Default desktop ACLs derive from the containing window station. Therefore an arbitrary desktop name is not an authorization mechanism; handles and ACLs matter. [Desktop access rights](https://learn.microsoft.com/en-us/windows/win32/winstation/desktop-security-and-access-rights)

Windows evaluates an application's identity and privileges using its process/thread access token. **Inference:** changing only a desktop does not change the token, so it does not automatically isolate the user's files, registry access, credentials, or network permissions. A separate account with restricted access or a suitably configured VM addresses a different requirement from hiding UI. [Access tokens](https://learn.microsoft.com/en-us/windows/win32/secauthz/access-tokens)

There is another non-UI sharing boundary: named mutexes, events, and file mappings use per-session namespaces by default; applications can also use the system-wide `Global\\` namespace. **Inference:** desktop-per-task does not solve all single-instance application conflicts, because desktop names do not create new namespaces for those objects. Separate sessions reduce some collisions but do not eliminate explicitly global objects or shared profile files. [Kernel object namespaces](https://learn.microsoft.com/en-us/windows/win32/termserv/kernel-object-namespaces)

These distinctions suggest separate design decisions for visual noninterference, input independence, account/profile isolation, and protection of host resources. A design can satisfy the first while failing the other three.

## Session 0 and scaling limits

Putting the whole GUI agent into a Windows service is not a shortcut. Modern services cannot directly interact with the user, and Microsoft advises against interactive-service techniques for new code. A service can coordinate work and communicate with a separately launched GUI process using IPC. Our recommended split is a coordinator outside the GUI worker, with the actual driver beside its target applications. [Interactive services](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)

There is no responsible fixed answer such as “Windows supports exactly 20 background agents.” App memory/CPU, UI-object usage, desktop heap, session configuration, and workload duration affect density. The `CreateDesktop` page includes historical 48 MB/registry-default guidance, whereas a newer Microsoft troubleshooting page describes per-session heaps and dynamic allocations in later operating systems. Those sources should not be combined into a supposedly universal modern desktop limit. [CreateDesktop remarks](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-createdesktopw), [desktop heap guidance](https://learn.microsoft.com/en-us/troubleshoot/windows-server/performance/desktop-heap-limitation-out-of-memory)

The newer guidance associates heap exhaustion with missing windows/controls and notes that adding physical RAM does not directly change desktop-heap sizing. Capacity needs measurement with the intended applications, rather than registry tuning based on old defaults. No such tuning or measurement was performed during this research. [Desktop heap diagnosis](https://learn.microsoft.com/en-us/troubleshoot/windows-server/performance/desktop-heap-limitation-out-of-memory)

## Established facts and unresolved evidence

| Question | Status | Consequence / evidence needed |
|---|---|---|
| Can Task View desktops isolate concurrent physical input? | No, by documented architecture | Use them for organization only. |
| Can Windows create hidden UI desktops and launch apps there? | Yes, documented APIs | Requires an intentional desktop-aware worker lifecycle. |
| Do two desktops in one station get separate clipboards? | No | Avoid shared clipboard actions or use a stronger boundary. |
| Can UIA avoid foreground mouse/keyboard interaction? | Yes, for supported patterns | Record capability per action/control/provider. |
| Does our UIA stack work on custom inactive desktops? | Unverified | Validate discovery, actions, events, and provider lifecycle with the actual driver. |
| Does every target app launch a fresh instance there? | Unverified | Check application singleton behavior, child processes, brokers, and profile locks. |
| Does every target app render usable hidden screenshots? | Unverified | Validate visual freshness and capture failure behavior independently from UIA. |
| Does minimizing a viewer preserve a usable guest/session desktop? | Environment-dependent | Validate viewer minimize, close, disconnect, lock, and reconnect separately. |
| Can multiple tasks safely share an execution worker? | Design-dependent | Serialize interaction transactions and explicitly allocate app state. |
| Does UI separation prevent unintended access to host data? | No automatic guarantee | Specify account privileges and resource boundaries separately. |

For ctrlloop, the research recommendation is to preserve Jev's existing selection workflow and treat execution placement as a replaceable backend concern. A semantic-only desktop worker is an attractive lighter option for applications with sufficient UIA support; a separate interactive environment is the broader option when physical-style input is required. The viewer should observe the worker rather than supply the worker's continued existence. Multiple sidebar tasks can share a queue or receive separate workers; the interface should distinguish “reasoning,” “waiting for a worker,” and “operating applications” so concurrency is accurate rather than merely visual.
