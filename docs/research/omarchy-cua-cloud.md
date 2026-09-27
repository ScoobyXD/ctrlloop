# Omarchy Cua Driver and the cloud worker decision

Research checked September 26, 2026. Research only: no installation, browser launch, cloud provisioning, implementation change, or workflow experiment.

The user wants unattended CUA + Jev workers on central infrastructure, with task/status UI and occasional user takeover. Continuous desktop streaming is not a requirement.

**Assessment:** evaluate Linux CUA before committing the product to Windows VMs. Omarchy supplies a concrete desktop/control integration and an existing cloud deployment path. It does not establish ordinary-container deployment, arbitrary concurrent browser input on one desktop, or improved anti-bot acceptance.

## What the announcement adds

The user's [announcement](https://x.com/trycua/status/2103498682532253734) concerns synthetic agent cursors inside Hyprland, Omarchy's Wayland compositor. The original X page did not load directly. The full official [September 25 announcement](https://github.com/trycua/cua/blob/cua-driver-rs-v0.30.1/blog/omarchy-cua-driver.md) was located in the release source. It describes target selection in Driver, a separate plugin cursor, and compositor delivery through an isolated input seat. It explicitly identifies Omarchy Edge availability and does not independently verify stable-channel promotion. This is narrower than treating the social announcement as certification of every stable installation.

The [repository demonstration](https://github.com/trycua/cua#see-cua-driver-in-action) shows two Driver sessions operating Calc and Inkscape while a terminal remains foreground. This matters because the compositor owns input routing and window focus: a compositor integration can provide independent input paths without relying on the physical user's pointer. It does not create separate customer security boundaries or independent application/account state.

The [Linux desktop concepts guide](https://cua.ai/docs/concepts/linux-desktops-and-computer-use) separates distribution, compositor, display protocol and application backend. Omarchy is Arch-based with Hyprland; support for a different Wayland compositor is not proof of Omarchy support.

## Cloud execution already exists, using VMs

[Run Omarchy on Fleet](https://cua.ai/docs/how-to-guides/sandbox/run-omarchy-on-cloud-fleet) supplies a bootable amd64 Omarchy image with computer-server and Driver MCP interfaces. Its KubeVirt `containerDisk` contains `/disk/disk.img`, and the SDK example selects `kind="vm"`. This is an OCI-delivered virtual-machine disk, not an ordinary application container. Agent observation and input work without a live viewer. The example's 4 CPUs/6 GiB are an example allocation, not a measured minimum.

[Optional noVNC access](https://cua.ai/docs/how-to-guides/sandbox/connect-to-omarchy-with-novnc) provides the occasional human interaction path. Maintaining user browser state across worker lifecycles is a separate design requirement; do not infer durable login persistence from a disposable provisioning example.

The [runtime support reference](https://cua.ai/docs/reference/sandbox-sdk/runtime-support) documents a separate local Ubuntu container path. That is evidence that Linux CUA containers are possible, not that the Omarchy Fleet image can run as a normal Docker process. A self-hosted container version would require its own session, display, accessibility and input validation.

## Release and support scope must be read separately

The [current support matrix](https://cua.ai/docs/reference/cua-driver/platform-support) still records restricted Hyprland background-input qualification: two agent seats, selected native Calc/Inkscape versions, and a separately enabled compatible plugin. Chromium, Electron and XWayland raw background input are outside that documented scope. Semantic AT-SPI actions and explicitly foregrounded input are separate routes. This does not establish that private-desktop Chrome control is unsupported; it means the two-app demo cannot certify independent concurrent Chrome workers sharing one desktop.

This restriction was also checked against pinned Driver 0.30.1 source, rather than relying solely on historical documentation. [hyprland_compatibility.rs](https://github.com/trycua/cua/blob/cua-driver-rs-v0.30.1/libs/cua-driver/rust/crates/platform-linux/src/wayland/hyprland_compatibility.rs#L14) admits only the specified LibreOffice/Inkscape executables and package versions. [hyprland_input.rs](https://github.com/trycua/cua/blob/cua-driver-rs-v0.30.1/libs/cua-driver/rust/crates/platform-linux/src/wayland/hyprland_input.rs#L1096) applies that check to the production background route. Chrome/Chromium is therefore excluded by this specific route's admission code, not merely missing from a public demonstration. Do not extend that conclusion to separate foreground or accessibility routes.

[Driver 0.30.1](https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.30.1) was released September 26. Its notes explain that the GitHub pre-release label is repository bookkeeping; ordinary Driver SemVer releases use the stable channel. That label alone is not a maturity verdict for every platform capability.

A [maintainer issue opened September 26](https://github.com/trycua/cua/issues/4216) reports Omarchy Driver 0.28.2 in stable/RC/edge, but its optional compositor plugin only in edge. The plugin uses a kit based on Driver 0.26.1 and lacks later fixes. Current upstream kits do not match Omarchy's compiler/compositor profile. Select a known compatible driver/plugin/image combination; installing the newest Driver alone does not update the complete input stack. This is a packaging issue to resolve, not proof the overall approach is unusable.

## Fit with Jev and our existing native demo

Linux's [native Driver guide](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/Skills/cua-driver/LINUX.md) exposes accessibility snapshots and element-addressed actions through AT-SPI. The driver and applications must share the desktop user's accessibility session bus. Empty trees can indicate a missing session bridge, not an absence of controls.

The preserved Jev candidate extraction is largely OS-neutral: [pipeline.py](C:/Users/Jonat/ctrlloop-hermes-cua-demo-2026-09-25/src/pipeline.py:97) consumes roles, labels, values, actions, frames and parent context. Preserve those existing selection interfaces and questions.

The adapter still needs a port: its [transport](C:/Users/Jonat/ctrlloop-hermes-cua-demo-2026-09-25/src/cua_mcp.py:79) defaults to a Windows named pipe; its [preflight](C:/Users/Jonat/ctrlloop-hermes-cua-demo-2026-09-25/src/cua_mcp.py:188) expects a patched accessibility-only Windows click schema; [focus verification](C:/Users/Jonat/ctrlloop-hermes-cua-demo-2026-09-25/src/driver.py:292) expects HWND fields; and [application search](C:/Users/Jonat/ctrlloop-hermes-cua-demo-2026-09-25/src/pipeline.py:205) uses Windows Search. None of these findings requires replacing Jev or inventing new selection questions.

**Important:** the [typed browser guide](https://github.com/trycua/cua/blob/main/libs/cua-driver/rust/Skills/cua-driver/BROWSER.md) says its default trusted browser-click route uses CDP Input. Therefore Cua's product name alone does not imply OS-only interaction. A native-control comparison must use the window/desktop/accessibility route deliberately.

## Decision implications

- Evaluate an existing Omarchy/Linux cloud desktop before building a Windows worker fleet solely to preserve our demo.
- Start with one controlling agent per independent desktop. Foreground inside that private desktop does not require a visible user stream. This avoids making general multi-cursor browser support a prerequisite.
- Treat multiple agents sharing one customer's desktop as a separate density improvement requiring browser-specific evidence. Two input seats do not establish arbitrary agent count, separate sessions, or protection between customers.
- Compare native CUA and manual browsing in the same cloud environment before attributing anti-bot benefits to the compositor. No source reviewed supplies that acceptance measurement.
- Choose VM versus container independently from the operating-system decision. The supported Omarchy cloud path found here is VM-based; a denser container path remains an engineering option to evaluate.
