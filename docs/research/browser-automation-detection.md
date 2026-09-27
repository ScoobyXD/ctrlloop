Browser automation detection research — 26 September 2026

This investigation covers local, isolated browser agents on a user's Windows computer. It reviews primary project documentation, original detection research, and this repository's implementation. No candidate was installed or exercised, no personal browser was accessed, and no product code or Jev questions were changed. Project claims below are distinguished from measurements. This is a research shortlist, not a migration decision.

**Existing implementations address the same problem, but universal acceptance is unproven.**

| Implementation | Documented mechanism and deployment | Relevance to ctrlloop |
| --- | --- | --- |
| [Camoufox](https://camoufox.com/stealth/) | Modified Firefox engine, fingerprint handling and isolated automation through its Juggler integration; supports local headless operation. | A replacement browser/control backend, not a Chromium CDP endpoint compatible with our present adapter. |
| [CamoFox Browser Server](https://github.com/redf0x1/camofox-browser) | Separate project wrapping Camoufox with an agent API, accessibility snapshots, element references and persistent profiles. Documents a portable Windows x64 package including Node and its pinned browser. | Particularly close to our local agent requirement. Windows support explicitly targets headless operation; native headed Windows and Linux virtual-display modes are not interchangeable. The server is Preview and does not promise frozen APIs or automatic state migration. No third-party site acceptance rate was verified. |
| [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) | Patches Playwright's Chromium control implementation and launch behavior. | Its documented preferred setup uses persistent headed Chrome. It can be evaluated as a comparison implementation, but installing it does not modify Browser Use's current direct-CDP commands. Its detector/site claims are maintainer claims, not our workflow results. |
| [Nodriver](https://github.com/ultrafunkamsterdam/nodriver) | Independent asynchronous Chromium automation implementation over CDP, without Selenium/ChromeDriver. | Another possible adapter backend. Our stack already avoids ChromeDriver, so that feature alone does not resolve our failure. Connecting Browser Use to a browser it launches would not automatically inherit all of Nodriver's interaction behavior. |
| [Rebrowser patches](https://github.com/rebrowser/rebrowser-patches) | Patches Puppeteer/Playwright behavior and provides comparison tooling. | Useful implementation research; the same direct-CDP integration mismatch applies. |
| [Browser Use Cloud](https://docs.browser-use.com/cloud/browser/stealth) | Managed modified Chromium, fingerprint handling, proxies and supported CAPTCHA handling. Its documentation explicitly allows for site rejection. | Hosted alternative or comparison reference. These capabilities are not automatically supplied by our local OSS library. No supported downloadable version of its hardened cloud runtime was established. |
| [Browserbase Verified](https://docs.browserbase.com/platform/identity/overview) | Managed purpose-built Chromium, partner-recognized fingerprints and separate Web Bot Auth integration. | Hosted offering; no supported local binary was established. Authentication of an agent's identity still depends on participating websites accepting it. |

The distinction between the browser engine and its controller matters. Our pinned Browser Use 0.13.10 imports `CDPClient` from `cdp_use` in [session.py](C:/Users/Jonat/ctrlloop/browser-use/browser_use/browser/session.py:15). Advice to replace a Playwright import with Patchright does not apply directly to this implementation. Replacing the browser backend should preserve existing Jev selection and action interfaces where feasible rather than introduce duplicate APIs.

**Published measurements demonstrate improvement, not an Internet-wide guarantee.**

Browser Use's [March 2026 benchmark](https://browser-use.com/posts/stealth-benchmark) reports 81% for its cloud service on 71 deliberately difficult sites. The tasks were short and unauthenticated; the LLM judge assessed blocking rather than correct task completion. Its ordinary Chromium baselines ran on a GitHub Actions datacenter IP. Those conditions cannot establish our laptop's failure rate or the reliability of account workflows. The benchmark is vendor-run, with [published code](https://github.com/browser-use/benchmark); even the vendor's preferred service still had failures.

Its [engineering article](https://browser-use.com/posts/bot-detection) describes fully headless execution with Chromium source patches, fingerprint consistency, network infrastructure and interaction changes. This is evidence that the offered implementation is more substantial than toggling window visibility. Claims of undetectability remain vendor claims; the explicit limitations in its documentation and benchmark are more useful for planning.

A green result on a public fingerprint checker establishes only that the checker did not flag the signals it measured. It does not establish acceptance by another site's private classifier, permission to perform a particular operation, successful login, or durable session access.

**Why normal Chrome and CUA can behave differently from our worker.**

Ordinary browsing presents a collection of browser, session, network and interaction signals. There is no transferable universal browser-trust value. [Cloudflare's detection documentation](https://developers.cloudflare.com/bots/concepts/bot-detection-engines/) describes combining request characteristics, browser signals and session behavior. A site can score or challenge a request rather than know with certainty that a human or agent generated it. Ordinary users can also encounter blocks, including from shared networks, as [Google documents](https://support.google.com/websearch/answer/86640?hl=en).

The user's successful CUA experience is evidence for that particular setup. It does not isolate physical visibility as its cause. CUA operated an existing ordinary browser and retained its complete local state; our worker changes the profile, startup flags, control channel, observation code and interaction path simultaneously.

| Dimension | Preserved CUA path | Current Browser Use worker |
| --- | --- | --- |
| Browser and profile | Existing native application window and its state | Independent Chrome process and dedicated profile |
| Control | Windows accessibility/native input | Chrome DevTools Protocol, plus page JavaScript |
| Window | Explicitly foregrounded | Headless launch |
| Login/state | Existing browser's actual state | Initial starting-site cookie/origin-storage import; later its own persistent state |
| Page modifications | Native accessibility click route does not require the Browser Use page helpers | DOM observation, interaction highlighting and framework event helpers |

Modern headless Chrome shares the normal Chrome implementation, according to [Chrome's documentation](https://developer.chrome.com/docs/automation-and-testing/headless). It is not the old separate, incomplete browser engine. Sites can read page visibility/focus information, but the [Page Visibility API](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API) reports document state, not proof that a person is watching a physical screen. Visible-window automation can still be detected, and background operation alone does not establish a bot.

**Concrete findings in our code.**

1. [The launcher](C:/Users/Jonat/ctrlloop/src/browser_runtime.py:277) uses `--headless=new` and `--remote-debugging-port=0`, a separate profile and a configured window size. These flags are documented to affect the page-visible automation indicator `navigator.webdriver`; its actual value in the failed run was not recorded. Websites do not need access to our command line to inspect exposed browser properties. [MDN reference](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/webdriver).
2. [Browser Use attaches afterward](C:/Users/Jonat/ctrlloop/src/driver.py:362). Its own launcher does not run. Consequently the flags in [Browser Use's defaults](C:/Users/Jonat/ctrlloop/browser-use/browser_use/browser/profile.py:201), including its automation-feature setting, are not automatically applied. This merits a deliberate audit, not wholesale copying of every upstream flag.
3. [Session import](C:/Users/Jonat/ctrlloop/src/browser_session.py:263) includes starting-host cookies and matching-origin local/session storage. It is not a full profile transfer and does not copy IndexedDB, extensions, preferences or browsing history. [Initialization logic](C:/Users/Jonat/ctrlloop/src/driver.py:375) imports once; later runs retain the worker's independent state.
4. [Normal click handling](C:/Users/Jonat/ctrlloop/browser-use/browser_use/browser/watchdogs/default_action_watchdog.py:903) sends CDP mouse movement/down/up events with programmed waits. Some element-specific fallbacks use JavaScript `element.click()`.
5. [Normal typing](C:/Users/Jonat/ctrlloop/browser-use/browser_use/browser/watchdogs/default_action_watchdog.py:1997) also invokes a framework compatibility helper. That helper [dispatches script-created input/change/blur events](C:/Users/Jonat/ctrlloop/browser-use/browser_use/browser/watchdogs/default_action_watchdog.py:2112). Script `dispatchEvent` events are distinguishable through `isTrusted`, but this does not mean every CDP-generated input event is untrusted or that untrusted events alone indicate abuse. [Event semantics](https://developer.mozilla.org/en-US/docs/Web/API/Event/isTrusted).
6. [Interaction highlighting](C:/Users/Jonat/ctrlloop/browser-use/browser_use/browser/session.py:2989) creates a DOM element with a Browser Use-specific attribute. This is an observable modification, not evidence that Google inspected it.
7. [Observation](C:/Users/Jonat/ctrlloop/browser-use/browser_use/dom/service.py:403) uses CDP DOM/Accessibility snapshots and JavaScript evaluation. The normal path reviewed does not appear to enable the CDP Runtime domain: [SessionManager](C:/Users/Jonat/ctrlloop/browser-use/browser_use/browser/session_manager.py:890) enables Page and Network. The explicit `Runtime.enable` call found is in a separate [Actor Page API](C:/Users/Jonat/ctrlloop/browser-use/browser_use/actor/page.py:53). This is a static code-path finding, not a captured protocol trace.
8. The preserved demo [forces Windows accessibility for clicks](C:/Users/Jonat/ctrlloop-hermes-cua-demo-2026-09-25/src/driver.py:313) and [foregrounds the selected window](C:/Users/Jonat/ctrlloop-hermes-cua-demo-2026-09-25/src/driver.py:292). It should not be described as exclusively simulating a physical mouse: its native accessibility route is a material distinction.

The failed run's screenshot establishes that Google presented an unusual-traffic page after the search. It does not identify which of these differences mattered. We also must not assume a datacenter IP, missing GPU or generic Linux fingerprint: this worker uses the user's installed Chrome on Windows. The actual emitted headers, fingerprint and network path must be measured before assigning those causes.

**Some widely repeated detection advice is outdated.**

Original [Castle research from August 2025](https://blog.castle.io/why-a-classic-cdp-bot-detection-signal-suddenly-stopped-working-and-nobody-noticed/) explains why the familiar console/Error.stack getter probe stopped working after V8 changes. The upstream [object-preview change](https://github.com/v8/v8/commit/61a907540d4c1dda4733476e54c977910f31041d) and [error-preview change](https://github.com/v8/v8/commit/e08e97347454255a337dcea361808fb25ca09077) prevent those getter side effects. That does not make every CDP implementation invisible, but it invalidates treating this old probe as automatic proof of present-day detection.

**A diagnostic comparison can separate the causes. It has not been run.**

| Comparison | Question it addresses |
| --- | --- |
| Personal Chrome manually and through CUA | Is the previously accepted configuration still accepted for this exact workflow? |
| Dedicated worker profile, ordinary headed Chrome, manual operation | Does separating the profile already change login/access? |
| Same worker with Browser Use attached, then observation, then actions | Does attachment, page inspection or interaction introduce the first difference? |
| Equivalent initialized worker state, headed versus headless | What changes specifically with headless execution? |
| Current backend versus Camoufox/CamoFox and a Chromium comparison driver | Does a maintained specialist implementation materially improve the intended workflows? |

Use the same browser version where applicable, network and low-frequency workflow; compare equivalent initialized state sequentially rather than concurrently opening the same profile. Record user agent/client hints, webdriver, visibility/focus, screen/viewport, locale/timezone, relevant rendering properties, input event sequence and actual navigation outcome. Avoid recording credential or cookie values. Record the first challenged operation, distinguish authentication failure from automation challenge, and repeat enough to recognize transient effects.

The research priority is to evaluate the closest existing local implementation while isolating our current runtime's signals. No evidence gathered so far justifies either discarding local headless operation or promising that a launch flag, cookie import or specialist runtime will eliminate all challenges.

**Assessment of the user's NHI Management Group sources (September 26, 2026).**

The [headless-browser FAQ](https://nhimg.org/faq/why-do-headless-browsers-and-automation-frameworks-increase-bot-risk-for-website/) is useful for the website operator's perspective: automation supports legitimate work and scalable abuse, so detection considers behavior and request/session consistency. It supplies neither a compatibility benchmark nor a concrete remedy for our failed Google search. The FAQ was accessible through indexed search text; direct retrieval failed.

The linked [guide's standards and audit sections](https://nhimg.org/the-ultimate-guide-to-non-human-identities) concern organizational credentials, permissions and accountability. These are relevant to securing an agent service, but do not establish acceptance by arbitrary websites. Its standards references need verification: it misnames WIMSE and conflates IETF SPICE with a remote-display protocol. The official charters describe [WIMSE as workload identity across systems](https://datatracker.ietf.org/wg/wimse/about/) and [SPICE as digital credentials and presentations](https://datatracker.ietf.org/wg/spice/about/). The FAQ also mislabels OWASP NHI entries; the [official OWASP list](https://owasp.org/projects/non-human-identities-top-10) identifies NHI3 as Vulnerable Third-Party NHI and NHI6 as Insecure Cloud Deployment Configurations.

For our research, distinguish three questions: what browser signals reveal automation, how an agent proves its identity, and whether a destination authorizes that agent's action. Addressing one does not automatically address the others. A signed agent identity also does not, by itself, prove which user delegated a particular task.

[Cloudflare's Web Bot Auth implementation](https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/) is a more directly relevant example: cryptographic HTTP message signatures authenticate bot/agent requests, using IETF drafts for the protocol and key directory. This is a path for participating services to recognize automation. It is not a universal authorization or a switch that makes an ordinary headless browser trusted by every website. Engineering inference: study it as a separate integration option alongside local browser compatibility; do not substitute enterprise identity standards for measuring the current browser's observable behavior.
