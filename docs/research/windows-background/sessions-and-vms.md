# Windows background execution: sessions, VMs, and RPA precedents

Research date: 2026-09-24. Scope: local Windows workspaces for one or several computer-use agents while the owner keeps using the PC. This is research and a proposed direction; no workspace was installed, configured, or exercised. Statements labelled **design inference** or **unverified** are not demonstrated compatibility claims for ctrlloop.

## What the product needs

An agent task, an agent process, a Windows session, and a virtual machine are different units. Ten tasks can exist in a sidebar without ten independent desktops. The decisive resource is the environment that owns focus, input, application state, and the UI that the agent observes.

**Design recommendation:** model each running GUI worker as having exclusive use of an execution workspace, then let many user tasks queue for, resume in, or run concurrently across those workspaces. A viewer is an optional attachment to a workspace. Hiding a viewer should not release the worker, shut down its environment, or stop its loop. This separation makes the requested task-sidebar experience possible without coupling task count to the number of visible windows.

For the first implementation, distinguish these acceptance criteria:

- The main desktop remains usable while a task operates another desktop.
- The task continues when its viewer is minimized.
- Reopening the viewer shows current state rather than restarting the task.
- Two workers can act simultaneously without exchanging focus, keystrokes, clipboard contents, files, or browser profiles accidentally.
- A task that requires human sign-in or attention can pause its own workspace without taking over the host.

These are proposed product requirements, not claims that every option below satisfies them automatically.

## Persistent Hyper-V VMs: strongest general local option

Hyper-V provides separately managed guest operating systems with virtual hardware. Windows client Hyper-V is distinct from the more extensive Windows Server offering; enterprise capabilities described in combined documentation should not all be assumed available on a workstation. Microsoft documents PowerShell management, templates, and isolated development environments. **Design inference:** a Windows VM per active GUI worker gives ctrlloop a straightforward isolation boundary and lets the guest use normal desktop applications and its own input state. This is the leading general-purpose option for multiple local workers. [Hyper-V overview](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/overview)

The key architectural separation is already present in Hyper-V: Microsoft documents starting a VM using `Start-VM`, followed separately by opening VMConnect. The VM and its viewer are different objects. **Design inference:** run the automation worker inside the guest and make viewer attachment optional. Do not drive the VM by clicking its host-side viewer window; doing that would reintroduce dependence on the owner's desktop. [Create and start a Hyper-V VM](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/create-a-virtual-machine-in-hyper-v)

VMConnect is a console tool for interacting with the guest. It can send input, change VM settings, and create checkpoints. Its documentation also notes that a second authorized basic-mode connection can take over an existing connection. **Design implication:** observing a live agent should default to an explicitly controlled viewer; human takeover should pause agent action rather than race it. [VMConnect](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/virtual-machine-connection)

Enhanced Session Mode uses RDP. It adds resizing and configurable sharing of clipboard, drives, audio, and other devices; some sharing is enabled by default. **Design recommendation:** make sharing deliberate. A VM can have strong OS separation while a redirected clipboard still creates exactly the interference the user wants to avoid. An enhanced-mode viewer is also subject to RDP-specific behavior and must not be mistaken for a guaranteed console-independent automation channel. [Enhanced Session Mode](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/enhanced-session-mode)

PowerShell Direct can execute commands and copy files between the Hyper-V host and a running Windows guest without relying on guest network configuration. It requires host Hyper-V administration privileges and valid guest credentials. **Design inference:** it is useful for provisioning, health checks, and controlled artifact transfer. It is not documented here as an interactive UIA transport or a guarantee that a process launched through it lands in the correct interactive desktop. The actual UI driver should be launched in the intended guest user session. [PowerShell Direct](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/powershell-direct)

Persistent guests let us maintain application installation and user state intentionally. Hyper-V checkpoints can capture or restore VM state, but standard checkpoints include memory whereas production checkpoints use data-consistent backup mechanisms and omit memory. **Design recommendation:** use an image/checkpoint strategy for recovery and environment setup, not as an automatic rollback of real-world actions. Restoring a guest cannot undo an email already sent or a record already changed in a remote service. [Checkpoints](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/checkpoints)

**Unverified:** ctrlloop's current driver working correctly inside a guest; its observations and input remaining healthy after viewer minimization/closure; guest lock behavior; performance of target applications; authentication recovery; and practical parallel-worker capacity. The running VM alone proves none of those application-level properties.

## Windows child sessions: promising for one companion worker

Windows exposes child sessions as special loopback RDP sessions attached to an existing parent session. The system supports only **one active and connected child session at a time**. The child terminates when its parent terminates. It cannot be locked, has no ordinary logon screen or screen saver, and is not governed by normal RDP connection timeout policies. Public APIs enable/query the feature, obtain the child session ID, and connect through the Remote Desktop ActiveX control's `ConnectToChildSession` property. [Windows Child Sessions](https://learn.microsoft.com/en-us/windows/win32/termserv/child-sessions)

**Design inference:** a child-session backend could be lighter than provisioning a complete guest OS for a single companion agent. But the one-child limit makes it an unsuitable sole foundation for arbitrary parallel task workspaces. Several queued tasks could reuse that one workspace; they would not thereby become independently concurrent GUI workers. Parent-session termination is a relevant lifecycle constraint for a product that should keep working after its window disappears.

There is direct precedent for the requested minimize behavior. UiPath's **versioned 2023.10** child-session documentation says minimizing its PiP viewer does not interrupt the process; closing it stops the process and signs the session out. That demonstrates feasibility for a product built on child sessions, not that every RDP viewer or custom implementation inherits the behavior. The same documentation describes browser-profile separation and startup/VPN application conflicts caused by applications being launched again for the same user. [UiPath PiP child session, 2023.10](https://docs.uipath.com/assistant/standalone/2023.10/user-guide/pip-child-session)

## Power Automate PiP: two different technologies

Power Automate currently documents two PiP modes. **Child session** supplies a secondary Windows session with mouse/keyboard and UI automation. **Virtual desktop, in preview**, runs within the same user session and supports selector/UIA-style automation; physical input, screenshots, and image automation are unsupported. Its viewer connection can close while execution continues. PiP is an attended feature; cloud-triggered PiP supports child-session mode only.

For child sessions, Microsoft lists shared clipboard, browser-profile separation, Office not being open simultaneously in parent and child, startup-app conflicts, and authentication/policy constraints. Virtual-desktop mode also has an Office concurrency restriction. Windows Home is excluded from the listed supported PiP platforms. These are substantial compatibility limits, particularly if the owner is using Office while the agent also needs it. [Power Automate PiP](https://learn.microsoft.com/en-us/power-automate/desktop-flows/run-desktop-flows-pip)

**Design inference:** “Power Automate supports PiP” is evidence that the user experience is possible, not a reusable guarantee for our driver. Selecting its newer virtual-desktop mode would mean qualifying every action against that environment's supported operations. Selecting its child-session approach would require a host/session lifecycle and collision policy. The two modes should never be collapsed into one capability flag called “background.”

## Current UiPath virtual desktops: a useful non-VM precedent

UiPath's current virtual-desktop robot-session documentation describes a live
preview while the main desktop stays usable, and says the product supports
multiple PiP user sessions per machine. This is different from the native
child-session limit above. It does not establish a particular per-user worker
count or identify a supported ctrlloop backend. Its preview also has a
primary-monitor limitation. [Current UiPath virtual desktop](https://docs.uipath.com/assistant/standalone/latest/user-guide/pip-virtual-desktop)

UiPath's current technology comparison excludes hardware-event and image-based
automation in that mode, but supports its Windows-message and simulated input
methods with application-dependent coverage. It recommends the virtual-desktop
mode when both options meet the workload's needs. The table also marks recording
and screenshot activities unsupported: an internal live preview is therefore
not proof that a screenshot-based agent interface is available. **Design
inference:** this strengthens the case for a later lighter desktop backend
for compatible apps; it does not remove our current foreground-typing and
desktop-hotkey dependencies. [Current technology comparison](https://docs.uipath.com/assistant/standalone/latest/user-guide/technology-comparison)

## Minimize, disconnect, lock, sign out, and sleep are distinct

Microsoft's RPA troubleshooting explicitly identifies minimized RDP windows and locked desktops as causes of UI automation failures. It documents a `RemoteDesktop_SuppressWhenMinimized` client registry setting for the former and recommends simulated actions where supported. That is evidence that ordinary RDP minimization is a real failure mode, not a universal invisibility switch. No registry changes were made during this research. [RDP and UI automation troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/desktop-flows/ui-automation/uipi-issues)

Disconnecting an RDS session is different from terminating it: Microsoft's `tsdiscon` documentation says running applications are available again on reconnection without losing their data. It does **not** promise that an unattended mouse/keyboard/UIA loop can continue successfully throughout disconnection. [Disconnect a session](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/tsdiscon)

**Design recommendation:** record both environment liveness and automation readiness. “VM running,” “worker process alive,” “interactive desktop available,” and “last action verified” should be separately observable states. A viewer may be hidden while all four remain healthy, or an RDP session may retain processes while the desktop becomes unusable for the needed action.

For a local worker, host sleep, hibernation, shutdown, and power loss should be treated as execution interruptions. A product may resume after wake; it should not promise continued local execution while the machine is suspended. Locking the host is a different event from locking a guest. The exact host-lock effect on a child-session backend and on a chosen guest-console setup remains an acceptance item, not a researched guarantee.

## Windows Server RDS and Azure Virtual Desktop

Windows Server RDS provides multi-session desktops and RemoteApp, with each user session sharing one server OS. Its session host, connection broker, licensing, and profile infrastructure provide a real platform for concurrent workers. RDS documentation recommends validating application assumptions about machine-wide paths and registry state. **Design inference:** RDS can improve worker density but trades VM-level independence for shared-OS compatibility work and operational complexity. [RDS overview](https://learn.microsoft.com/en-us/windows-server/remote/remote-desktop-services/overview)

Power Automate documents concurrent desktop flows on supported Windows Server versions using **different user accounts**; concurrent desktop flows under the same user are unsupported. Its page describes turning on RDS for more than two parallel user sessions. This is a product implementation rule, not proof of an identical limit for ctrlloop and not a free-production-use entitlement for two sessions. [Power Automate concurrent runs](https://learn.microsoft.com/en-us/power-automate/desktop-flows/run-desktop-flows-concurrently)

Ordinary unattended Power Automate on Windows 10/11 does not solve “work while I use my PC”: Microsoft says it cannot run with an active user session, including a locked one. It creates and manages an RDP session and distinguishes that session from console access. **Design inference:** its unattended mode is appropriate as a worker on a dedicated guest/machine, rather than on the desktop the owner is actively using. [Unattended desktop flows](https://learn.microsoft.com/en-us/power-automate/desktop-flows/run-unattended-desktop-flows)

Windows Enterprise multi-session is a specific edition allowing concurrent interactive sessions. Microsoft limits production use to Azure Virtual Desktop (AVD); ordinary Windows Pro or Enterprise is not upgraded into it by changing a configuration flag. Capacity depends on resources and workload. Application installers can also treat multi-session as a server OS. [Windows Enterprise multi-session FAQ](https://learn.microsoft.com/en-us/azure/virtual-desktop/windows-multisession-faq)

AVD is not strictly cloud-compute-only: its session hosts can run on **Azure Local** infrastructure. However, its service components remain in Azure, it requires supported registered Azure Local infrastructure, and it is not a standalone feature for an arbitrary Arc-enabled server or personal Hyper-V host. User access rights and service charges still apply. **Design inference:** this is an enterprise deployment alternative, not the shortest path to background agents on one developer PC. [AVD on Azure Local](https://learn.microsoft.com/en-us/azure/virtual-desktop/azure-local-overview)

## Windows Sandbox: useful disposable experiment, weak persistent pool

Windows Sandbox is a hypervisor-isolated temporary desktop. Host applications are not automatically installed there. Closing Sandbox discards its software, files, and state; on supported recent versions, restarting **inside** the sandbox preserves state for that sandbox lifetime. Microsoft explicitly says simultaneous multiple Sandbox instances are unsupported. This makes stock Sandbox a poor match for persistent parallel agent workspaces, though it is useful for a single disposable experiment. [Windows Sandbox overview](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/)

Its `.wsb` configuration can specify memory, networking, folder mappings, clipboard redirection, and a startup command. Writable mapped-host-folder changes survive disposal; they are not reverted with sandbox-local state. Clipboard redirection is enabled by default. **Design inference:** Sandbox can bootstrap a temporary worker and export selected artifacts, but supporting persistent logins or multiple agents would require something beyond the documented stock lifecycle. Mapping a folder does not make the entire guest persistent. [Sandbox configuration](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)

## Capacity and licensing boundaries

Hyper-V requires appropriate Windows editions, firmware virtualization support, SLAT, and enough RAM for the host plus all simultaneously running guests. Microsoft notes that an already detected hypervisor can suppress the usual `systeminfo` requirement display. A hypervisor being present does not establish that the complete VM management tools and guest setup are ready. Hardware eligibility, installed feature state, and successful automation are separate checks. [Hyper-V requirements](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/host-hardware-requirements)

Power Automate's high-density guidance suggests a baseline of four CPU cores/four GB RAM for the first session and two cores/four GB per additional session, explicitly varying by workload. These are session recommendations for that product, **not ctrlloop VM sizing measurements**. For our planning, measure peak working sets, browser subprocesses, storage growth, UI response latency, and simultaneous model-call pressure while preserving host responsiveness. [Power Automate requirements](https://learn.microsoft.com/en-us/power-automate/desktop-flows/requirements)

Do not equate an included hypervisor with rights to an arbitrary number of Windows guests. Microsoft's Windows virtual-desktop licensing guide is specifically scoped to Commercial Licensing and varies rights by program and deployment. We have not established this user's guest entitlements or application rights. [Windows virtualization licensing guidance](https://www.microsoft.com/licensing/guidance/Windows-11-Licensing-for-Virtual-Desktops)

RDS has its own user/device CAL requirements; AVD access licensing differs for Windows client versus Server hosts and internal versus external use. AVD access rights do not automatically include Office. These are deployment selection inputs, not a conclusion about a specific license purchase. [RDS CALs](https://learn.microsoft.com/en-us/windows-server/remote/remote-desktop-services/rds-client-access-license), [AVD licensing](https://learn.microsoft.com/en-us/azure/virtual-desktop/licensing)

## Ranked direction for ctrlloop

1. **Persistent local Windows VM per active GUI worker:** leading general option. Establish one worker with optional viewing, then add a second only after input isolation and resource use are understood.
2. **One native child-session worker:** worthwhile narrower option if quick access to host-installed apps matters and its app/profile limitations are acceptable. Treat it as a single companion workspace, with queued tasks.
3. **RDS or AVD workers:** stronger candidates for a later managed fleet where session density and centralized operations justify the infrastructure.
4. **Stock Windows Sandbox:** temporary research/prototyping, with its documented disposal and concurrency limits.

This ranking is engineering judgment from the documented properties above. The decisive next artifact is a compatibility matrix for the actual applications and driver actions, including viewer hidden/restored, host lock/unlock, guest reconnect, concurrent typing in the host, profile ownership, and worker recovery. That can be gathered through a bounded manual prototype without introducing automated tests, new Jev questions, or a second competing driver/action API.
