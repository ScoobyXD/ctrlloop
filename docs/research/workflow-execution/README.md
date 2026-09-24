# Executing small VA workflows without taking over the user's computer

Research date: **September 24, 2026**. This note narrows the earlier [Windows background research](../windows-background/README.md) and [VM research](../windows-vm-workers/README.md) around the product: users define small, repeatable workflows that move data between applications. It is a recommendation, not a demonstrated deployment or a capacity benchmark. Existing runtime edits were left alone. No VM, browser worker, new Jev question, or automated test was created.

## Recommended decision

Use **headless browser automation for workflows contained in web pages**, and **one persistent Windows VM for workflows that need native Windows applications**. A mixed workflow stays in that Windows VM from beginning to end, including its browser work. Start with one active native workflow run per VM. Reuse the VM for subsequent runs of any compatible saved workflow.

These are two independent choices:

- **How to control an application:** browser protocol for web pages; Windows UIA/CUA for native applications.
- **Where it runs:** the user's machine, an agent VM on that machine, or a remote worker.

A headless browser can run directly on the host, inside a Windows VM, or on a remote machine. It does not inherently require a VM. Conversely, a Windows VM can contain both a visible browser controlled through a browser protocol and native Excel controlled through CUA.

For the smallest first isolation proof, provision **one persistent Windows VM and run one invocation at a time using the current Jev/CUA stack**, including browser controls exposed through Windows UIA. This proves placement with the least change to the existing implementation; it does not yet supply the missing saved-workflow features. Headless is a second, scoped integration: prefer an existing Playwright browser backend, keeping the existing Jev selection/reporting code. Once serial execution works, add independent browser executions if actual throughput requires it. Do not build both a cloud fleet and a local VM manager to prove the product.

## What headless actually means

Headless Chrome still loads HTML, executes page JavaScript, computes layout, and runs the application. Its platform windows are not displayed. The page has controls and geometry even though the user cannot see a browser window. Modern Chrome shares its implementation between headless and visible modes. [Chrome headless documentation](https://developer.chrome.com/docs/automation-and-testing/headless)

The current repository is Python with **Jev**, so this document interprets the spoken “Java” as Jev. If Java was literal, [Playwright also supports Java](https://playwright.dev/java/docs/intro); the isolation and execution choices are the same.

There are three useful ways to observe a browser page:

| Observation | What it supplies | Role in this product |
| --- | --- | --- |
| DOM | Page elements, their attributes, values, text, and relationships | Exact data reads and element lookup |
| Semantic/accessibility snapshot | Roles, accessible names, control state, text, and often actionable references | Compact candidates for Jev |
| Screenshot | Rendered pixels | Debugging and possible separate visual fallback |

Browser accessibility is available through browser tooling; it does not require reading the Windows accessibility tree of a visible Chrome window. Current Playwright has `locator.aria_snapshot(mode="ai")`, documented as including element references and iframe snapshots. The AI mode was added in 1.59. This is a version-specific capability, so an integration must pin and verify its chosen release. [Playwright Locator reference](https://playwright.dev/python/docs/api/class-locator#locator-aria-snapshot)

Jev currently accepts text/structured textual state, not images. A screenshot therefore does not by itself give our existing Jev loop a way to understand an unlabeled visual control. Such a fallback would require a separate perception component. [TypeSafe System One](https://docs.typesafe.ai/concepts/system-one)

## How a headless Jev loop would work

Suppose a workflow has reached an ERP form. A browser backend might return this **illustrative** observation:

```text
textbox "Customer name" [ref=e12]
textbox "Purchase order" [ref=e13]
button "Save" [ref=e14]
button "Cancel" [ref=e15]
```

The references are assigned by the browser backend. They are not invented by Jev and are not stored forever in the workflow recipe.

1. The runner opens or reuses the run's browser context and navigates to the configured starting page.
2. It obtains a fresh semantic observation for the page/frame the run owns.
3. Existing selection machinery presents relevant candidate descriptions to Jev. Keep the state small and preserve enough surrounding labels/row context to distinguish similar controls.
4. Jev returns a candidate choice and the already-supported action decision. The application retains the backend's exact target reference with that candidate.
5. Code passes the chosen reference and any prepared value to the browser backend.
6. The backend resolves the target, performs its action, and the runner observes the result before progressing.

This reuses the same observe → choose → act → observe structure. It does not require a model to generate JavaScript or a new browser automation engine. Independent selection questions remain batched; dependent rounds still wait for earlier results. TypeSafe's state model explicitly supports evaluating multiple independent questions against one supplied state. [TypeSafe state](https://docs.typesafe.ai/concepts/state)

For a known target, ordinary Playwright code that intentionally sets the entire customer-name field can look like:

```python
page.get_by_role("textbox", name="Customer name", exact=True).fill(customer_name)
page.get_by_role("button", name="Save", exact=True).click()
```

These are explanatory API examples, not a replacement for snapshot-bound Jev selection. `fill()` replaces the field value; the browser integration must preserve the intended replace/append behavior instead of treating all typing methods as equivalent. A locator describes how to find an element; it is resolved against the page when used. Repeated labels require scoping to the correct form, row, or frame. Avoid treating “the third button” as a durable business identity. [Playwright locators](https://playwright.dev/python/docs/locators)

Before a normal click, Playwright waits for a unique, enabled, visible, stable element that can receive the click. It can scroll the target into view. “Visible” here concerns page layout, not whether there is a monitor displaying the browser. Actionability does not prove that the ERP saved the right record; that requires checking the application outcome. [Playwright actionability](https://playwright.dev/python/docs/actionability), [Locator click](https://playwright.dev/python/docs/api/class-locator#locator-click)

In Chromium, Playwright's input implementation sends events through the target page's browser-protocol session, including `Input.dispatchMouseEvent`. That click is delivered inside the browser, not through the host's physical mouse. Page keyboard operations similarly target the browser session. Direct form filling has its own semantics; it is not always a sequence of physical keystrokes. [Playwright Chromium input source](https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/crInput.ts), [form input methods](https://playwright.dev/python/docs/input)

Do not substitute arbitrary `element.click()` JavaScript or forced actions as a universal shortcut. Normal browser actions provide useful targeting and readiness checks. Do not reuse old references after navigation, page replacement, or a competing observation has invalidated their snapshot. Even a still-connected element may now represent changed business data; confirm the relevant record/form identity.

## Which existing browser component to use

**Recommended first candidate: Playwright MCP**, with a deliberately small integration into the current runner. It already supplies semantic snapshots and reference-based click/type operations, and supports headless operation. It is a tool server; our code can call it after Jev returns a choice. Jev does not need to become a general tool-calling language model. [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)

Direct Playwright is also reasonable when we want tighter control over browser contexts and fewer processes. Current upstream code implements `aria-ref` lookup against retained snapshot elements. That is useful evidence that another homegrown reference registry is unnecessary, but source-level selector behavior should not be mistaken for a permanently stable public contract. Prefer the supported server interface initially or explicitly qualify a pinned library integration. [Playwright injected selector implementation](https://github.com/microsoft/playwright/blob/main/packages/injected/src/injectedScript.ts)

The repository's existing [CUA browser guide](../../../skills/cua-driver-skill-0.28.2/BROWSER.md) also describes semantic snapshots, tab references, and browser-specific actions. However, its setup binds through a native `(pid, window_id)` and its profile preparation discovers a native window. That is evidence for a browser-aware route, not proof that the pinned version works headlessly. The current ctrlloop adapter does not expose those browser tools.

Choose one browser implementation for the pilot. Preserve its own target records and action contracts. Do not construct fake Windows PIDs/HWNDs to squeeze browser pages through Windows validation, and do not build parallel public workflow APIs for browser and Windows execution.

One launch detail matters: Playwright's default headless Chromium and its unified Chromium channel can select different browser binaries. The documented `channel="chromium"` option opts into the newer headless mode. Pin the browser/tool version and qualify the exact launch mode; “headless Chrome” is insufficient deployment metadata. [Playwright browsers](https://playwright.dev/python/docs/browsers)

## Workflows, runs, loops, and capacity

| Term | Meaning here | Does it need its own VM? |
| --- | --- | --- |
| Saved workflow | Reusable instructions/steps and their configured inputs | No |
| Run | One invocation, such as copying one order into the ERP | Uses an existing desktop worker when needed; no new VM per invocation |
| Control loop | One fresh observation through its selected actions, ending at the last computer-use command dispatch | No; many sequential loops make up a run |
| Browser context | A browser session with separate cookies/storage from other contexts | No |
| Desktop worker | The controller currently owning one interactive Windows desktop | Initially one per Windows VM |

**Twenty saved workflows can share one Windows VM.** The deciding factors are invocation frequency, run duration, deadlines, app/account compatibility, and shared output conflicts. A workflow configured to repeat every hour should not reserve a VM for the entire hour. A bounded invocation acquires the worker, finishes or stops, then releases it; waiting for its next scheduled occurrence does not need a desktop reservation.

For native UI, keep one run's ownership across all its control loops, including time spent waiting for Jev or the application. Windows has one input desktop per interactive window station, and the current pipeline uses focus and foreground typing. Multiple user sessions can create other execution environments, but those add administration and application constraints. [Microsoft desktop model](https://learn.microsoft.com/en-us/windows/win32/winstation/desktops)

Interleaving desktop jobs while Jev thinks creates a race:

```text
A observes an invoice form → A sends candidates to Jev
B opens another invoice or changes the active application
A receives its answer → its intended form/focus/state may have changed
```

Locking individual clicks does not solve that race. Our recommendation is one complete bounded native run at a time on that desktop. This is a simplicity rule for our mixed-input stack, not a claim that Windows cannot run multiple processes or that every UIA method requires foreground focus.

### How many headless runs fit in one VM?

There is no universal browser count. Browser contexts can coexist inside one browser and separate cookies and web storage. Pages within one context are tabs in the same session; use those for a single workflow that spans several websites. Independent contexts are appropriate when runs require separate browser state. [Playwright context isolation](https://playwright.dev/python/docs/browser-contexts), [Playwright pages](https://playwright.dev/python/docs/pages)

| Arrangement | Sensible use | Shared limitation |
| --- | --- | --- |
| Several tabs in one context | One workflow spanning email and ERP | Login/storage and application state may be shared |
| Several contexts in one browser | Independent browser runs for a trusted customer | Browser failure and machine resources |
| Separate browser instances/process groups | Stronger separation of profiles and browser failures | OS resources; still not independent machines |
| Separate VMs | Native desktops or stronger tenant separation | Physical host resources and operational cost |

A native run and several headless runs can share a Windows VM if their browser/app state and write targets are independent. All browser operations must remain on the browser route: no escape into OS-level keyboard, clipboard, or native dialog automation from those parallel browser workers. Give the native run's browser separate ownership as well.

After the serial pilot and browser integration work, the first concurrency experiment is **one native run plus two independent headless runs**, then four browser runs if measurements justify it. These are conservative experiment settings, not measured capacity or support guarantees. A customer needing only browser tasks can run those headless workers without a Windows VM. Initially serializing runs that share an account or output file is simpler than building a resource-lock service.

Do not assume two MCP connections equal two browser contexts. Configure and verify the chosen server's session/context ownership. Two controllers must not mutate the same page or race its observations. Nor are contexts a substitute for isolation between unrelated customers; use a separate customer worker boundary when operating a hosted service.

### Capacity arithmetic, with no invented benchmarks

Assume 20 runs each take **two minutes at the chosen concurrency**, including model calls, actions, and waits. All arrive together and are independent:

| Simultaneous runs | Ideal time until the last run finishes |
| ---: | ---: |
| 1 | 40 minutes |
| 2 | 20 minutes |
| 4 | 10 minutes |

For native jobs under the proposed single-session rule, four simultaneous runs mean four desktop workers, initially four VMs. For web-only jobs, four contexts/browser workers might fit in one machine. The table assumes concurrency does not slow individual runs; measure that assumption.

If those same 20 two-minute runs occur once per hour, they consume 40 worker-minutes per hour: about 67% utilization of one worker. One VM can serve them if their deadlines tolerate the queue and actual durations leave enough room for variation. If all must finish in ten minutes after simultaneous arrival, the average hourly utilization hides the burst requirement.

Use these engineering calculations:

```text
worker-minutes required = sum(invocation count × measured run duration)
average utilization = worker-minutes required / available worker-minutes
approximate model requests/second = completed runs/second × requests/run
```

Do arithmetic in code. Count actual API requests, not candidates, groups, or rounds interchangeably. Several groups can be sent together, while request-size limits may split a round into multiple calls. Browser concurrency does not remove Jev rate limits or website throttling.

Measure completed business items/minute, success rate, p50/p95 run time, queue delay, peak browser-process-tree memory, guest/host memory pressure, CPU, and actual API request duration. Count cold login/startup separately from warm runs. Stop increasing concurrency when it worsens useful throughput or host responsiveness. Existing hardware readings in the older research are historical observations, not current free-resource guarantees.

## Keeping the product small

You can avoid a large orchestration system. You cannot avoid choosing which job owns a shared desktop next. Implement that choice as a small local runner with a persistent pending list, a fixed concurrency cap, and one active native run. Use existing run/report identities; this is not a proposal for another service or wire model.

The minimal policy is:

1. A due workflow adds a bounded invocation, with overlap prevented for that workflow.
2. The runner starts it when its configured worker is free.
3. The run keeps that worker until it completes, fails, or needs attention.
4. An unresolved native run stops the desktop queue until its app state is safe to reuse.
5. After a crash or ambiguous submission, inspect the outcome before retrying a write.

For a pilot, a plain timer and persistent run records are enough; no distributed scheduler is required. If a poll finds 1,000 emails, process a bounded batch before allowing the next workflow its turn. This controls starvation without a sophisticated priority system. A background wait for new work should not be an endlessly active GUI invocation.

| Keep out of the initial architecture | What replaces it initially |
| --- | --- |
| VM per workflow or invocation | One persistent compatible VM reused serially |
| Dynamic VM pool and autoscaling | Manually configured fixed capacity |
| Switching desktop ownership between control loops | One owner for the whole bounded run |
| Moving a partly completed run between machines | Run stays where it started |
| Snapshot restore as arbitrary workflow resume | Stop and inspect interrupted work |
| Profile/workspace affinity scheduler | Fixed customer/account/workspace assignment |
| Cross-machine browser-to-desktop handoff | Put the mixed run in one Windows VM |
| Custom VM viewer or streaming stack | Existing VM viewer for the pilot |
| RDS/AVD multi-session density work | One normal interactive session per VM initially |
| Generic document/profile synchronization | Explicit source/output location per workflow |
| General DOM/accessibility reference engine | Existing browser backend |

Do retain: explicit worker ownership, a place for prepared input values, a record of which source item was processed, observable completion, and a way to stop for human attention. Removing these creates repeated/incorrect data entry. Do not promise exactly-once writes to an arbitrary GUI: a server can commit a submission while the response is lost. Use destination record IDs or another observable receipt where available; otherwise stop on uncertainty. The current [driver retry policy](../../../src/driver.py) already avoids automatically replaying input after a lost response.

This note recommends deferrals rather than deleting earlier requirements. The key accepted requirement remains: automation cannot take over the user's desktop.

## Moving data without building a synchronization system

For **web email → desktop Excel → browser ERP**, put all three in the same Windows VM. The runner keeps extracted values in its run state, downloads attachments into the run's working directory, operates Excel in that guest, and uploads from the same directory. A browser upload can use a file-input API rather than clicking an OS file picker. Downloads can be captured and saved directly by the browser backend; save the intended output before closing its context, which removes temporary downloads. [Playwright uploads](https://playwright.dev/python/docs/input#upload-files), [Playwright downloads](https://playwright.dev/python/docs/downloads)

When copying a value, use the exact observed source text/value in code. Jev can select the relevant source or destination where supported; it does not have to rewrite the value. A shared Windows clipboard is unnecessary for this transfer and becomes a source of interference between workers. No new extraction question is authored here.

For files already on the host, choose one explicit contract: a selected exchange folder, an imported input file with a returned output, or an existing cloud document location. Avoid a custom bidirectional mirror of the user's whole profile. If the user and worker edit the same workbook concurrently, the data conflict remains even with perfectly isolated mice. The easiest initial restriction is one writer per document or separate output files.

“Excel workflow” needs one distinction during setup:

- If it really requires native Excel UI, VBA, an add-in, or application-specific behavior, use the Windows worker.
- If it only reads/writes supported `.xlsx` cell data, a file library can sometimes eliminate Excel entirely. This is a narrower supported operation, not a general replacement for Excel. Libraries have preservation limitations; openpyxl specifically documents unsupported items that can be lost when existing workbooks are saved. [openpyxl tutorial](https://openpyxl.readthedocs.io/en/stable/tutorial.html)

This capability choice belongs in workflow setup. The scheduler does not need Jev to rediscover it on every invocation.

## Authentication, unsupported controls, and seeing the worker

Persist an agent-owned login/profile where appropriate. Playwright supports saved authentication state, but the complete login arrangement can depend on cookies, local storage, IndexedDB, or session storage. Restoring a subset is not a guarantee that every application stays signed in. Expect reauthentication when the service requires it. [Playwright authentication](https://playwright.dev/python/docs/auth)

The same browser user-data directory cannot be launched by multiple browser instances simultaneously. A browser context is not a copied online account: two sessions can still change the same ERP record. A persistent profile should have one clear owner. [Persistent browser contexts](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)

| Situation | Practical treatment |
| --- | --- |
| Standard forms, links, tables | Browser semantic/DOM route |
| Frames and popups | Explicit frame/page ownership and fresh observations |
| Virtualized grid | Observe loaded rows and deliberately navigate/scroll for more |
| Canvas or unlabeled custom widget | Qualify its exposed semantics; stop or use a separate supported perception route |
| HTML file upload/download | Browser file APIs; no native picker required |
| Native helper, desktop dialog, certificate middleware | Native worker capability may be required |
| Expired login or MFA requiring a person | Pause that run for attention |
| Site refuses automation | Treat as a deployment compatibility issue; headless is not a bypass |

Extensions are not categorically incompatible with headless execution. Playwright documents headless extension operation with Chromium and a persistent context; extension-specific behavior still needs qualification. [Playwright extensions](https://playwright.dev/python/docs/chrome-extensions)

Headless is an execution mode, not a user-experience requirement to make setup invisible. Let users configure and authenticate in a visible setup session. A VM worker can be observed through its ordinary viewer. For headless execution, snapshots/screenshots or traces can show progress; human interactive takeover needs a deliberately supported path, not an assumption that a running headless process can simply become a normal window.

Inside a VM, the driver runs beside the target applications, in their interactive user session. Host UIA does not automatically see through the VM viewer into guest controls. A Windows service in Session 0 is not that user session. [UIA architecture](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-uiautomationoverview), [interactive services](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)

For the local pilot, use the existing Hyper-V/basic VMConnect direction from the earlier research. Keep the guest app restored while minimizing the viewer. Enhanced VMConnect uses RDP, and Microsoft documents minimized RDP windows and locked sessions as potential automation failure conditions. This exact driver/display arrangement still needs manual validation. [VMConnect modes](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/enhanced-session-mode), [automation failure conditions](https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/desktop-flows/ui-automation/uipi-issues)

Ordinary Windows containers do not provide the interactive desktop required by native Excel/CUA. A Docker package containing a Windows VM is still running a VM. [Microsoft Windows container guidance](https://learn.microsoft.com/en-us/virtualization/windowscontainers/quick-start/lift-shift-to-containers)

## Local versus hosted execution

| Placement | Product advantage | Additional work/limit |
| --- | --- | --- |
| Headless browser on user's PC | Smallest web-only local deployment | PC must stay awake; agent profile still needs setup |
| Persistent local Windows VM | Native apps and browser in one place; separate desktop | VM setup, guest app/login state, host resources and awake time |
| Remote browser worker | Work can continue while user's PC is off | Authentication and access to private/local inputs |
| Remote Windows VM | Same native control pattern on an always-available host | Hosting, Windows/apps, networking, licensing, and operational ownership |

If “go off and do its thing” means “while the user works,” local execution is a candidate. If it means “overnight while the laptop is asleep,” local execution cannot satisfy the requirement; the worker must run on an awake machine elsewhere. This is a deployment decision separate from headless versus native control.

Network access is also separate from UI compatibility: a hosted browser cannot automatically reach a customer's private ERP, VPN, mapped drive, or local attachment. Keep the first pilot in one known deployment environment. Windows/Office virtualization and unattended-use entitlements also depend on the actual deployment; the earlier [platform and image research](../windows-vm-workers/platform-and-images.md) remains relevant before selling a packaged worker.

One concrete commercial consideration: Microsoft documents a specific unattended license for Microsoft 365 Apps for enterprise, covering one physical or virtual operating-system environment per license for that scenario. An ordinary user sign-in should not be assumed to cover an arbitrary fleet of unattended Office workers. This is a deployment-specific licensing fact, not an assessment of this user's existing entitlements. [Microsoft 365 unattended RPA](https://learn.microsoft.com/en-us/microsoft-365-apps/licensing-activation/overview-unattended)

If VM provisioning itself is too much initial product work, support connecting to one customer-provided Windows worker first. It can be a manually configured VM or a dedicated spare PC. That keeps image distribution and automated hypervisor lifecycle outside the first pilot while preserving the same native execution boundary.

## What the repository already has, and what remains

| Reuse | Evidence |
| --- | --- |
| Pinned Jev model and batched selection | `src/selection.py`: `Candidate`, `ChoiceSelector`, `jev-1.13.0` |
| Timing and actual selection traces | `src/reporting.py`, `docs/timing.md` |
| Native CUA transport and validations | `src/driver.py`, `src/cua_mcp.py` |
| Observe/select/act structure | `src/pipeline.py` |

The current `Driver` is Windows-shaped: native IDs, window activation, and CUA snapshot targets. A browser backend is a real scoped integration, not a launch-flag change. Keep a shared upper workflow/selection/reporting path and retain backend-native targets below it. Generalize the existing seam where necessary instead of adding near-identical frontend/backend APIs or inventing `BrowserWindow`/`VmWindow` copies.

More fundamentally, `run_pipeline` currently holds one selected real window for the remainder of its run, accepts one prepared text payload that is consumed when typing is dispatched, and stops at an iteration limit without assessing task completion. That is a useful control-loop building block, not yet an email → Excel → ERP workflow runner. [Current pipeline](../../../src/pipeline.py)

The product still needs saved steps, field/data binding, transitions between applications, source-item progress, and an explicit observable finish condition. Those should be bounded workflow execution work. The VM does not supply them. A saved workflow should retain the intended target/operation and data mapping, not stale element tokens or recorded screen coordinates. Re-observe when executing; use deterministic lookups for configured known targets and the existing Jev selector where interpretation is needed.

No new Jev questions are proposed or implemented in this research. Any question additions remain subject to the user's audit requirement.

## The next evidence that would change the decision

First establish isolation with the current small Jev/CUA loop inside one guest; this does not depend on implementing a saved-workflow system. As the browser and workflow capabilities become available, perform manual qualification with real representative small workflows, following the repository's no-new-tests policy:

1. One web-only workflow through headless browser snapshots, exact value transfer, and observed completion.
2. One mixed workflow entirely in the guest while the host user works and the viewer is minimized.
3. Several saved workflows queued sequentially on that same guest, showing reuse without VM creation per run.
4. Two, then four independent browser runs, measuring useful throughput and memory while checking that inputs, accounts, and outputs stay correctly assigned.
5. One controlled interruption at a submission boundary, proving that the product stops on an unknown outcome rather than blindly duplicating it.

Start at **one persistent native worker**. Treat **two independent browser runs** as the first concurrency experiment. Add capacity only when measured completion times and deadlines justify it. The most consequential simplification is keeping a run's application state in one place until that run ends.
