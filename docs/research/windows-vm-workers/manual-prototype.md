# Bounded manual prototype and acceptance evidence

**Proposed work only; not executed.** Research date: 2026-09-24. This document is
a reviewable plan, not authorization to enable features, reboot, install or boot
guests, change networking/security policy, register tasks, run GUI automation,
purchase licenses, or use external accounts. No automated tests are proposed.

The prototype answers one narrow question: **can the existing pinned
ctrlloop/CUA loop operate restored guest applications continuously while the
host user works and the guest viewer is minimized?** A second guest then checks
independence. A functioning VM or a screenshot alone does not answer it.

## 1. Scope and fixed starting configuration

Start with one persistent, normally provisioned Windows 11 guest on Hyper-V,
then at most two. Use VMConnect **basic mode** first, with in-guest execution and
an interactive guest user. This avoids making RDP display suppression part of
the first experiment. Check the actual `LockOnDisconnect` setting; do not change
it or policy silently. Basic mode's suitability remains a hypothesis.
[Viewer rationale and lifecycle evidence](viewer-and-lifecycle.md)

Suggested initial resource budget: 4 vCPUs, 8 GiB fixed RAM, and a dynamically
growing virtual disk with roughly 100–128 GiB logical capacity per guest. These
are experiment settings, not measured requirements or a promise of worker
capacity. Keep VM files on storage with adequate free space, record actual
allocation and growth, and stop expansion if the host becomes unresponsive.
Do not count virtual disk capacity as already allocated bytes or assume it
will remain small after updates and checkpoints.
[Platform and sizing discussion](platform-and-images.md)

Provision only guest apps needed for the proof: a simple native app with known
UIA controls, File Explorer, and a supported browser. Use distinct local worker
profiles and deliberately distinguishable nonsensitive files/windows. Keep
clipboard, drive, microphone, camera and other redirection off unless a later
case explicitly qualifies that integration. Do not sign into production cloud
accounts merely to prove local UIA.

Record these facts before acting:

| Evidence | Why it matters |
| --- | --- |
| Host Windows edition/build; enabled virtualization features; power policy; guest VM GUID/name | Reproducible host/provider identity, separate from prior point-in-time research readings. |
| Guest edition/build, image origin, firmware/TPM/Secure Boot configuration, update state | Windows 11 compliance and reproducible app behavior. |
| Guest username category, session ID, window station/desktop, lock state, driver process owner/integrity | Driver and target UI must inhabit the intended interactive environment. Record no passwords/tokens. |
| CUA release/commit, both patch identities, binary hash and daemon tool capability | An executable's version alone does not prove the connected daemon has the direct-only patch. |
| ctrlloop source revision plus exact working-tree snapshot used, package pins, Jev pin | The host checkout is evolving. Copy only the selected code/artifacts, not the whole personal profile or secret stores. |
| Viewer version/mode, `LockOnDisconnect`, guest resolution/DPI/monitor count, app versions | Attach/detach can alter the surface automation uses. |
| Switch type, guest address, DNS, proxy/VPN situation, allowed control endpoint and redirection policy | Distinguish web connectivity from control reachability and file sharing. |
| Host/guest UTC clock offset plus local monotonic timing | Wall clocks from different machines are not directly comparable stage timers. |

Jev credentials would be provisioned deliberately through an approved setup
path at implementation time. Do not copy or inspect host browser authentication
stores or bake personal credentials into an image. All UI data sent to Jev in
this proof should come from nonsensitive fixtures.

## 2. Time and stopping bounds

After prerequisite setup, budget approximately **two hours of manual operation**
across the blocks below, plus separately measured installation/boot time. Do not
expand to a third VM, a custom viewer, or a rewritten driver during this proof.
If a required case fails, retain its evidence and narrow the cause first.

Stop active dispatch immediately on host input/focus interference, wrong-VM
routing, unexpected access to unshared host files, or unexplained external
side effects. Lock/unsupported-control failures should return a clear existing
error and require attention. Do not fall back to controlling the host desktop.

## 3. Manual cases with explicit pass evidence

| Block and budget | Operation | Required evidence / pass condition |
| --- | --- | --- |
| **A. One guest baseline — 15 min** | In the guest, discover windows, inspect real UIA controls, invoke a reversible native control, enter prepared text in an empty intended field, and read the changed state. Exercise the unchanged Jev loop on a bounded fixture. | Native guest `pid/window_id`, snapshot identity, chosen control/action and resulting state agree. Connected daemon advertises the patched route. No result is inferred solely from an API success. |
| **B. Viewer minimized — 15 min** | Leave target apps restored inside the guest. Keep the host focused on a nonsensitive typing fixture. Minimize basic VMConnect and perform at least five complete, timestamped observation/action loops spanning several minutes; restore viewer. | Guest actions and fresh observations occur **during** minimization. Host typing, focus, pointer and clipboard stay correct throughout. Restored viewer shows current guest state without restart/replay. This is the primary acceptance gate. |
| **C. Two guests — 20 min** | With B independently provisioned beforehand (setup time measured separately), give A/B similar app/window names but different content. Run overlapping guest loops while host typing continues; minimize both viewers. | Simultaneous intervals contain successful A and B actions. Each native target/report stays tied to its assigned VM. No keystroke/control/file appears in the wrong environment. Similar names or coincident numeric PIDs cannot reroute actions. |
| **D. Local/shared data and web — 20 min** | Create/rename/delete disposable local files in A; browse a public page and download a harmless fixture. Then deliberately configure one dedicated scratch share/mapping and edit one shared file. Qualify an explicit file-copy channel separately. | Local-only mutations stay in A. Browser has working DNS/HTTPS. Shared changes appear at the intended shared endpoints; the checked host/A/B private fixture locations remain unchanged. Account/path/permissions are recorded. Internet access alone does not make host files appear. |
| **E. Lifecycle/failure cases — 30 min** | Separately minimize, close/reopen viewer, lock/unlock guest, lock/unlock host, manually stop a loop for human takeover, and interrupt its control connection. Evaluate sleep/restart only in a separately safe window. | Record process liveness, desktop readiness, actions, capture and task outcome separately for each transition. No replay of uncertain input. Before takeover, stop dispatch and establish that no action remains in flight; start a fresh run/observation afterward. Closing behavior matches `LockOnDisconnect` and policy. |
| **F. Persistence/recovery — 20 min** | Save a local fixture; orderly guest shutdown/start; verify data and app setup. On a disposable guest with no cloud sign-ins, exercise one checkpoint restore around only local fixture changes. | Guest-local disk restoration behaves as recorded; required app/profile data survives ordinary shutdown. Stop the old run and discard its references manually; observe afresh after restore. Explain external state explicitly instead of claiming it rolled back. |

Do not count repeated no-op/refusal responses as five successful loops in B.
If the current prepared-text behavior permits only one typing operation, use
one-time text plus suitable reversible click actions or several separately
bounded existing runs. Do not add new Jev questions or alter text semantics to
make the experiment pass. A current loop limit ending the run is not evidence
of task completion; a human checks the requested fixture outcome.

There is no implemented coordinator to prove automatic takeover fencing,
cancellation, or restore invalidation yet. E–F use manual stop/quiescence and
fresh runs to qualify platform behavior. Automatic ownership, verified Stop,
late-action rejection and recovery are later product acceptance requirements,
described in the architecture chapter; they cannot be credited as passing this
unchanged-loop prototype.

### Detailed observations for block B

Keep two independent views of evidence: host observations for noninterference,
and guest reports/readback for progress. Before/after host focus snapshots alone
can miss a brief focus steal. A human observer and, if authorized, a recording
of nonsensitive host/guest fixtures are appropriate. Do not rely on a viewer
that stops repainting as the only indicator of whether the guest is working.

A simple example is the host typing `HOST-ONLY` in a local scratch editor while
guest A changes a value/control in its own native app. Guest A's saved/read-back
result should contain its intended data; neither field should contain the
other environment's input. Independently verify clipboard sentinel values
without production content. Guest overlays may appear inside the VM; no overlay
or desktop Search UI should appear over the host user's work.

### Lifecycle case sheet

Use one row per event and restore a known baseline between cases:

| Transition | VM running? | Guest user session available? | CUA native observation? | Input effect verified? | Capture fresh? | Host unaffected? | Recovery required? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Basic viewer minimized | To measure | To measure | To measure | To measure | To measure | To measure | To measure |
| Basic viewer closed | To measure | Include lock-on-disconnect | To measure | To measure | To measure | To measure | To measure |
| Enhanced/RDP viewer minimized, if separately evaluated | To measure | To measure | To measure | To measure | To measure | To measure | To measure |
| Guest locked | To measure | Lock is expected to limit readiness | To measure/refuse | No unverified continuation | To measure | To measure | Fresh state after unlock |
| Host locked | To measure | Do not assume guest locks | To measure | To measure | To measure | No host input | To measure |
| Guest signed out | To measure | Original session gone | Must not target another session silently | Stop/attention | To measure | To measure | New sign-in/startup |
| Host sleep/restart, later safe window | Interrupted | Re-establish | Re-establish | No progress promise | Re-establish | Not applicable during sleep | Reconcile actions |

An RDP-specific registry workaround is a separate configuration experiment,
not part of baseline acceptance and not proof that basic mode works. Do not
disable guest locking, UAC or other protections to conceal a failure.
[Documented viewer caveats](viewer-and-lifecycle.md)

## 4. Shared/cloud scenarios without unsafe demonstrations

Block D can prove local-vs-explicit-share behavior with disposable files. It
does not need real Slack posts, sent mail, purchases, or changes to production
documents. If cloud consistency is later qualified, use a dedicated disposable
document/account under explicit authorization and record which devices are
signed in and whether sync is enabled. The
[visibility matrix](isolation-and-networking.md) already explains expected
propagation and conflict behavior; research alone does not warrant live writes.

A checkpoint case must remain local. A useful manual demonstration is creating
file `after-checkpoint.txt` on A's private disk, then showing it disappear after
restore. If a separate scratch share was intentionally used, its files should
remain at their current version. Do not restore a signed-in guest and rerun a
real-world task merely to demonstrate that duplication can occur.

## 5. Evidence bundle and timing

Use existing `stdout.json`, `stderr.log`, `total_run.json`, and Jev input/output
artifacts. Store them under distinct VM/run directories with a short manual
environment/event note. No replacement report schema or automatic test harness
is needed. Current reports are buffered until close, so record a crashed run
as potentially missing evidence rather than treating absent logs as no action.
[Existing reporting](../../../src/reporting.py), [timing definitions](../../timing.md)

For each qualifying run retain:

- Run start/end UTC, worker/VM identity, action ownership interval, viewer event
  times, host/guest clock offset, versions, settings, and actual final result.
- Per-loop shared-scale stacked bars for window discovery, each Jev window
  round, focus, controls, each Jev control round, CUA action, settling and
  overhead; include milliseconds, candidate reductions, actual API counts and
  durations, and click-sent/loop-end markers.
- The existing Jev questions, round/group candidates and probabilities, and
  ultimate choice. Use actual reports and current grouping; do not synthesize
  “successful” rounds or infer completion from the iteration limit.
- A timeline showing each loop's start/end, wall time, cumulative elapsed time,
  operation duration and loop-total row. Preserve the requested loop boundary:
  current-state acquisition through the last CUA command sent before the next
  state. Label subsequent waits/bookkeeping separately where applicable.
- Host and guest CPU/RAM, disk allocation/growth, network and action latency
  at baseline, one worker and two. Note peak contention and host input lag,
  not just averages or idle VM consumption.

Boot/login/provisioning time belongs outside the existing loop's operational
duration, clearly labeled. Separate wall time spent waiting for Jev from local
UIA/input time. A two-worker run can slow because of host resource contention,
the remote API, or both; measurements should identify which.

## 6. Decision after the prototype

**Accept the basic-console architecture for the tested app set only if A–D
pass**, including minimized viewers and simultaneous host use. Passing one app
does not certify all apps. Record E–F as individually supported transitions or
explicit pause/recovery requirements; do not hide failures behind VM liveness.

If UIA succeeds but foreground text fails only with RDP minimized, first
qualify the basic-console path. If basic console also fails, investigate guest
session, display driver and app-provider behavior with the same pinned stack
before selecting a different viewer/hypervisor. If native control patterns are
missing even with the viewer visible, that is an app/driver capability issue,
not proof against VM isolation.

Only after those findings choose a product lifecycle provider, implement
task ownership/cancellation, and decide whether the loop stays in the guest or
uses a secured remote transport. The
[architecture guide](architecture-and-scheduling.md) defines the existing
contracts to preserve. The prototype itself is not a task manager and does not
establish unattended sign-in, licensing, crash-safe resume, or production capacity.
