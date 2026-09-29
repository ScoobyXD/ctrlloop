# Linux Driver: editable-entry value readback

The native entry-value fix was **built, activated, and verified** in the original Omarchy VM at **`2026-09-28T04:55:44.038Z`**. It exposed Chrome's actual address in the existing observation schema, and the resumed worker used that evidence to complete the saved-instruction X navigation. Chrome, its window, the compositor, and viewer/MCP services were preserved throughout that activation. This is historical evidence; a later guest restoration returned the original VM to the upstream binary.

On **2026-09-29 00:59 UTC**, the same reviewed patch was rebuilt and activated on Kevin's independent development guest `ctrlloop-omarchy-dev-kevin-20260928-1683478c`. Its active binary hash exactly matches the reviewed binary below. Native MCP initialization, window listing and window-state observation passed; noVNC returned HTTP 200. Chrome's fresh profile remains at its unaccepted Additional Terms screen, so address-value behavior was not exercised on this guest. The worker is stopped pending account assignment/configuration; no worker or Jev credentials were transferred. Private evidence is under `.local/environments/dev/workers/kevin/desktop-setup/`, including `native-readback.json`, the build manifest and activation log. The new pool expires at **2026-10-01 00:22:16 UTC**. Reproduce setup with [setup_worker_desktop.py](../scripts/setup_worker_desktop.py), selecting the exact namespace, claim and sandbox.

The MVP's second real run stopped with `address_readback_failed` after typing the generated X URL. Native Cua Driver 0.24.0 exposed Chrome's **Address and search bar** as a named editable entry but omitted its text from `value`. The worker correctly refused to press Enter without reading back the intended destination.

The pinned upstream Linux AT-SPI walker already reads the entry's Text interface. It previously used that text only as a label fallback when the accessible name was empty. Chrome supplies the name “Address and search bar,” so the typed address was discarded from the resulting observation.

The current [patch](../scripts/patches/cua-driver-0.24.0-atspi-entry-value.patch) preserves this already-read text in the existing `value` field when the role is `entry`, its text is nonempty, and no value has already been read (`value.is_none()`). Reading text does **not** require the AT-SPI EditableText interface. The accessible name remains the label, and a successfully read numeric value keeps precedence. No new observation schema, action, API, or Jev question is introduced.

The patch also excludes Text and Value payload reads for roles containing `password`, `protected`, or `secure`; those payloads cannot become label fallbacks. It does not change accessible names supplied by applications.

## Pinned source and build inputs

| Input | Pin |
| --- | --- |
| Official repository | `https://github.com/trycua/cua.git` |
| Driver release | `0.24.0` |
| Exact source commit | `4b3396d9fe4bd3cf723b0eb8db83c18a8764b520` |
| Changed source | `libs/cua-driver/rust/crates/platform-linux/src/atspi/native.rs` |
| Upstream source-file SHA-256 | `de702ccbd43e6338bb7a73bddb2e5cafef0f8f0de994f0666f14ecc373922a10` |
| Current reviewed patch SHA-256 | `3f201b2498c7024de8c7ef77d7a56df8fc99e2e3a83a020a24fc4a02a67ef035` |
| Source-file SHA-256 after current patch | `c056bf05190cc370e468c6e4eddcedb3e8c70e1d6afa95b826f2996f68097177` |
| Current built binary SHA-256 | `5cf2a5f956db046329606ef10fb113cf0db8a8c58da7fa6aa1068049ab0e25b4` |
| Original guest binary SHA-256 | `a033485d78a1c5966edd8ce29792d7f2d19a1988bcb58a6e292b6d17255262e1` |
| Original guest binary | `/opt/cua-driver/venv/lib/python3.13/site-packages/cua_driver/bin/cua-driver` |
| Rust / target | `1.97.1` / `x86_64-unknown-linux-gnu` |

The current patch hash was read back locally and matches the pin. The build helper checks both the original source hash and the expected patched source hash. The reported Driver version stays `0.24.0`; use the binary hash and patch manifest to distinguish each reviewed build from the upstream binary.

## Reproduce the build

[`scripts/patch-omarchy-driver.sh`](../scripts/patch-omarchy-driver.sh) is a **build-only** helper with no arguments. Transfer it together with its sibling `patches/cua-driver-0.24.0-atspi-entry-value.patch` into an explicit revision directory in the existing guest, preserving their relative paths. Run as the existing `omarchy` desktop user from that revision:

```bash
bash scripts/patch-omarchy-driver.sh
```

The helper requires the original reviewed binary, an x86_64 Omarchy session, at least 6 GB free space, Git, curl, tar, xz, Python, a C compiler, pkg-config, flock, and user systemd. It checks the X11, Xi, XTest, Xext, Wayland client, and xkbcommon development libraries. It uses `/home/omarchy/ctrlloop-driver-build/` for the source, toolchain, downloads, Cargo cache, and build outputs.

It fetches the exact official commit, verifies the source and patch, applies the patch with whitespace checking, and refuses unrelated source changes. It can upgrade the exactly recognized earlier patch by restoring only this source file from the pinned commit before applying the current patch; build caches remain intact. Pinned official `rustc`, `cargo`, and `rust-std` archives are checked against their release SHA-256 files before their installers run under this home-directory build root.

Compilation uses the transient user service **`ctrlloop-driver-build.service`**, with **1536 MiB memory**, **256 MiB swap**, **100% CPU quota**, **Nice 10**, and **one Cargo job**. The child process reads back the limits before building and fails if they do not match. Cargo uses the committed lockfile, the `cua-driver` binary target, release mode, and `portal-input`. LTO and incremental compilation are disabled, debug information is off, and codegen units are 16. An out-of-memory failure is a failed build; the helper does not drop limits and retry.

After compilation it checks `--version` and dynamic-library resolution. The output path is:

```text
/home/omarchy/ctrlloop-driver-build/target-4b3396d9fe4bd3cf723b0eb8db83c18a8764b520/x86_64-unknown-linux-gnu/release/cua-driver
```

On success, `/home/omarchy/ctrlloop-driver-build/build-manifest.json` records the source commit, Rust version, original/patch/built binary hashes, build time, and **`activated: false`**. The helper leaves the installed `/opt` binary and active services unchanged.

## Separate activation and rollback

Chrome child processes that shared the original Driver cgroup were preserved before any daemon restart. The one-use [`scripts/preserve-omarchy-chrome.py`](../scripts/preserve-omarchy-chrome.py) invoked the normal user systemd manager's `StartTransientUnit` method once, passing the verified PIDs into the new **`ctrlloop-chrome-preserved-22550.scope`**. It made no direct cgroup-file writes, enabled no delegation, and changed no `KillMode`. The helper is pinned to the original process identities and is not a general repeatable migration command. Do not stop the preservation scope while Chrome is using it.

[`scripts/activate-omarchy-driver.sh`](../scripts/activate-omarchy-driver.sh) implements explicit **`--activate`** and **`--rollback`** modes. Run it only inside the assigned guest after a successful build and while no run is active:

```bash
bash scripts/activate-omarchy-driver.sh --activate
```

It checks the original binary and system unit hashes, verifies the built manifest, acquires the same native-endpoint lock used by the worker, checks for an active-run journal, and stops the idle worker. It then rechecks the journal and verifies that every protected Chrome/Hyprland/WayVNC process and viewer/MCP service is outside the Driver's cgroup. A failed preflight leaves the worker paused and stops before modifying the Driver.

After those checks, the helper backs up the original binary/unit under the build root, installs a verified home-directory release, and writes only the owned user drop-in **`~/.config/systemd/user/cua-driver.service.d/50-ctrlloop-entry-value.conf`**. It restarts **`cua-driver.service`**, waits up to five seconds for the exact expected executable to become observable, and compares the protected desktop and service PIDs before/after. It preserves the original `/opt` binary and the system unit; it does not change `KillMode` or cgroup delegation.

The helper attempts restoration if activation fails after modification. Explicit rollback uses:

```bash
bash scripts/activate-omarchy-driver.sh --rollback
```

Rollback verifies the owned drop-in and retained activation manifest, restores the original daemon configuration, and checks desktop preservation. It accepts either reviewed patch's retained manifest and does not depend on mutable Cargo build artifacts. Activation accepts only the current patch and requires the original daemon configuration, so roll back an earlier reviewed build before activating the new one. Both modes leave **`ctrlloop-worker.service` stopped**. Establish a fresh native MCP session and prove address-value readback before restarting the worker separately.

## Verification status and limits

**The current patch's build passed** at **`2026-09-28T04:45:37.026213Z`** (September 27, 9:45:37 PM PDT). The manifest `.local/mvp/native-build-manifest.json` records patch SHA-256 `3f201b2498c7024de8c7ef77d7a56df8fc99e2e3a83a020a24fc4a02a67ef035` and binary SHA-256 **`5cf2a5f956db046329606ef10fb113cf0db8a8c58da7fa6aa1068049ab0e25b4`**. This release rebuild reused existing artifacts and took **2m 10s**, with reported peak **1.2 GB memory** and **no swap**. Cargo incremental compilation remained disabled.

**Current-build activation and fresh native address readback passed.** The daemon became active at **`2026-09-28T04:49:56Z`**, with MainPID **125644** executing the release identified by the current binary hash. A fresh MCP snapshot (`s00000001`) of Chrome window **94562712109632**, PID **22550**, exposed **Address and search bar** with the exact `value` **`https://x.com/`**. The observation is retained in `.local/mvp/patched-native-readback.json`. The worker was then resumed. The address was still pending in Chrome's New Tab, so this readback establishes the observation fix; it does not establish a loaded X page.

Independent readback in `.local/mvp/native-build-effective-limits.json` verified the build's actual cgroup limits while running: `memory.max=1610612736` (1.5 GiB), `memory.swap.max=268435456` (256 MiB), and `cpu.max=100000 100000` (one CPU's quota). The unit reported `Nice=10`; the captured memory counters showed no OOM or OOM-kill events.

Process preservation is **verified** in `.local/mvp/native-chrome-preservation.json`: all **16** targeted Chrome child PIDs and their start times survived, the destination scope is active with `Delegate=no`, and no targeted Chrome process remains in the Driver cgroup. Main Chrome PID **22550** and its existing window were preserved. No Chrome or VM restart occurred.

The current daemon activation is independently recorded in `.local/mvp/native-activation-evidence.json`. Before/after observations retained Hyprland PID **609**, main Chrome PID **22550**, native MCP service PID **739**, WayVNC PID **1876**, and websockify PID **1886**. The existing Chrome window identity also remained unchanged. The activation record identifies the current patch and binary hashes and the running home-directory executable.

The completed demo run **`fd224a26-652c-4896-bb4b-64115b7f38e7`** then verified the loaded X page using worker revision **`7443318e84165bc5`**. After Jev selected an address-field click, the worker checked the exact pending URL in fresh native state and committed it with Enter. Native snapshot **`s0000000d`** at **`2026-09-28T04:55:44.038Z`** returned URL **`https://x.com`** and document title **“X. It’s what’s happening / X”** in the same Chrome window. Worker execution took **8,486.804 ms**, with one Jev call using **3,809 input tokens** at **$0.000159978**. The [MVP acceptance record](mvp.md#acceptance-evidence) records the full evidence and earlier failed attempts. No new Jev question was introduced.

The upstream Text read is capped at **4,096 characters**. Empty entry text still produces no text-derived `value`, so this change does not support proving that a field is empty. Existing numeric values are retained; missing values may fall back to nonempty Text for `entry` roles even without EditableText. Other roles do not receive this text-value fallback.

The patch supplies native observation evidence; it does not prove a site loaded or make an uncertain input effect successful. The worker must still compare the fresh address with the intended URL before Enter, then observe both the destination and a loaded X document before recording completion. No browser CDP connection, clipboard fallback, profile modification, or screenshot inference is added.

This helper is specific to the preserved Linux VM and original binary hash. It does not use the repository's separate Windows Driver 0.28.2 build scripts. No repository test suite is added; compilation and the authorized native readback/integration run provide the required validation.
