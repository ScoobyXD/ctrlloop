# Cua Cloud fit against the agreed architecture

Reviewed September 27, 2026 against [architecture.md](../architecture.md). This is a documentation and public-source audit, not a live deployment result. No cloud resources were provisioned and no product implementation was changed.

Cua Fleet provides the foundations for the proposed private-desktop architecture. Full compatibility is not yet established. Durable customer state across worker recreation, customer-facing desktop access through our own website, and our exact Chrome workflows remain qualification requirements. The architecture's requirements remain unchanged.

| Requirement | Assessment | What remains ours |
| --- | --- | --- |
| Cloud execution without controlling the customer's computer | Documented private VM arrangement. | Connect our backend to each worker; keep desktop actions inside it. |
| One independent desktop per simultaneously acting agent | Fleet pools provide replicas; each claim reserves a computer. | Assign distinct claims and maintain customer/agent/claim ownership. |
| Our dashboard with agent status and progress | Compatible with Fleet lifecycle and service APIs. | Build the dashboard and progress reporting from our existing task and Jev data. |
| Jev selects actions; Cua Driver observes and executes | Omarchy's recipe exposes Driver MCP as a named guest service. | Adapt our existing integration to the remote Linux Driver and verify version compatibility. |
| Agents run without an open viewer | Documented. Viewer connections and worker reservations have separate lifetimes. | Our backend owns the claim and agent loop independently of the viewing page. |
| View the same desktop from a browser | Omarchy has an official noVNC recipe. | Integrate the viewer with our site; verify customer authentication and WebSocket routing. |
| Observation without mouse/keyboard input | noVNC supplies a view-only client setting. | Enforce control ownership at the backend/proxy or guest when strict input exclusion is required. |
| Take control, resume, and remain paused if the takeover viewer closes | Feasible application behavior; not a complete Fleet feature. | Stop agent actions before human input, end human input before resuming, and observe fresh state. |
| Upload through our website and download results | File-transfer APIs are documented. | Website UI, authorization, durable file storage, and transfers to/from the correct claimed worker. |
| Preserve files, browser profiles, and login state after worker recreation | Not established by the documented default Fleet storage behavior. | Obtain a supported persistence mechanism or implement and validate recovery. |
| Chrome works through the intended native desktop route | Needs validation on the exact image and Driver version. | Demonstrate our required browser actions and target-site workflows. |
| Swappable components and preservation of existing demos | Our implementation choice; Fleet does not require replacing Jev. | Reuse existing interfaces and keep provisioning/transport behind the appropriate adapter. |

Fleet's [pool and service concepts](https://cua.ai/docs/concepts/pools-claims-and-guest-services) describe reservations and remote service access. The [Omarchy recipe](https://cua.ai/docs/how-to-guides/sandbox/run-omarchy-on-cloud-fleet) exposes computer-server and a separate Driver MCP endpoint. These support the proposed division between our backend and hosted desktops; they do not supply our product dashboard or agent logic.

**Durable state is the most consequential gap.** Cua's [Fleet integration guide](https://cua.ai/docs/how-to-guides/sandbox/run-openai-agents-api-on-cloud-fleet#retrieve-files-before-cleanup) says releasing a claim resets the disposable disk overlay and instructs applications to retrieve outputs first. [Runtime support](https://cua.ai/docs/reference/sandbox-sdk/runtime-support#image-customization) says Fleet snapshots are not implemented and snapshot-derived images are rejected. Public documentation reviewed here does not establish a persistent-volume attachment workflow for our customer profiles.

Reconnecting to the original live claim preserves access to that existing worker. Serializing its identity does not save the worker's disk or memory. Keeping a claim alive also does not meet the requirement to recover after worker replacement. See [reconnection and renewal](https://cua.ai/docs/reference/sandbox-sdk#reconnection-and-renewal).

Our own durable storage and the [file APIs](https://cua.ai/docs/reference/sandbox-sdk/interfaces#files) can preserve work files independently of compute. Exporting files alone does not prove browser-login recovery: the necessary profile, keyring, and browser-bound state must survive and remain usable. A successful login cannot be guaranteed indefinitely because websites may expire or revoke sessions. The infrastructure requirement is to avoid losing the customer's necessary state when replacing a worker.

**Our website's viewer requires integration validation.** The standard [Omarchy noVNC guide](https://cua.ai/docs/how-to-guides/sandbox/connect-to-omarchy-with-novnc) uses Cua browser authentication. [Signed service URLs](https://cua.ai/docs/how-to-guides/sandbox/share-service-with-signed-url) provide a potential access mechanism for our customers, but the feature must be enabled by the Fleet environment. Validate the viewer page, its assets, and its live WebSocket connection through our intended customer access path. A custom proxy is another design possibility, not a verified implementation from this audit.

The [noVNC API](https://novnc.com/noVNC/docs/API.html) provides view-only behavior, but a client setting is not server authorization. Our handoff must prevent new agent actions, settle any action in flight, and then enable human input. Resuming must disable human input first and discard stale UI selections. Merely revoking a signed URL is not documented as terminating an existing input connection. Pause the agent loop while leaving the VM running for the human.

**Browser compatibility remains separate from hosting.** [Cua's platform support](https://cua.ai/docs/reference/cua-driver/platform-support) distinguishes Hyprland foreground input from its restricted background route. Owning the entire guest desktop avoids needing the experimental multiple-controller background feature, but does not itself prove our Chrome workflows or website acceptance.

Before selecting Cua Cloud for the full architecture, demonstrate these outcomes:

1. A customer enters our website, opens only their agent's desktop, and observes without taking control or needing a separate Cua account.
2. Two agents operate different VMs concurrently. Human takeover pauses one agent without disrupting the other; closing that viewer leaves the taken-over agent paused; explicit resume uses fresh state.
3. A file travels from our website into the correct worker and the resulting file returns to our website.
4. After deliberately replacing a worker, required files and browser state are recovered for the same customer and the signed-in target workflow remains usable, subject to the site's own session policies.
5. Jev and the native Driver complete representative Chrome actions on the selected image without depending on the excluded background-input route.

The persistence result also determines whether we can safely remove idle compute to save money. A low duty-cycle hosting estimate assumes a working durable-state strategy; keeping desktops alive indefinitely is not a substitute for recovery.
