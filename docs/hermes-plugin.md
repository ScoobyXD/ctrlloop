# Preserved Hermes/CUA demo

Separate demo directory: `C:\Users\Jonat\ctrlloop-hermes-cua-demo-2026-09-25`.

Its original CUA pipeline, Jev questions/selector, lockfile, installed Python packages, Python runtime and actual patched CUA binaries were copied before replacing the current runtime. `PRESERVATION.json` records original source hashes. Original dependencies remain `typesafe-sdk==0.7.1`, `python-dotenv==1.2.3`, `httpx2==2.13.0`, with model `jev-1.13.0`.

Credentials were not duplicated. Demo launches reference `C:\Users\Jonat\ctrlloop\.env`; leave it in place. Hermes keeps its existing home for authentication, configuration and conversations. Those personal files were not copied.

## Existing Hermes sessions

The `ctrlloop_run({request, text?})` tool and launch receipt remain unchanged. New plugin imports launch the preserved interpreter/CLI directly, with explicit credential and archived-driver paths.

Already-running Hermes can retain its old plugin module and checkout path. `src/main.py` therefore recognizes ONLY the old launch signature:

```text
--request-file <this-repo>\.local\hermes\launch-*\request.txt
[--text-file <same-directory>\text.txt]
```

It forwards that call to the preserved CLI before importing the current runtime's dependencies. Extra flags and unrelated request files do not enter this route. Ordinary invocations use the Omarchy cloud desktop loop. Keep this compatibility entry point while the demo needs already-loaded Hermes sessions.

The tool returns `{status: "launched", pid, directory}`; it does not assert completion or parse the child's final report. Existing Hermes settings, plugin junction and running processes are not switched by this migration.

## Direct invocation

```powershell
& 'C:\Users\Jonat\ctrlloop-hermes-cua-demo-2026-09-25\.venv\Scripts\python.exe' `
  'C:\Users\Jonat\ctrlloop-hermes-cua-demo-2026-09-25\src\main.py' `
  'Click the requested control in the selected desktop app.' `
  --env-file 'C:\Users\Jonat\ctrlloop\.env' `
  --driver 'C:\Users\Jonat\ctrlloop-hermes-cua-demo-2026-09-25\.runtime\cua-driver\cua-driver.exe'
```

The original daemon requirement remains: the demo connects to an already-running CUA daemon, default `\\.\pipe\cua-driver`. It does not start/stop the daemon. The copied release is `0.28.2-direct-accessibility-only`, including UIA and cursor companions. Selecting a different executable does not select a different daemon; the original `--driver-socket` option remains available.

The archive's original README and `docs/hermes-plugin.md` preserve the detailed desktop behavior. New demo reports go into that separate directory; old reports here were left in place.

`DEMO.md` and `run-ctrlloop-demo.ps1` at the demo root provide direct launch instructions. The complete Hermes desktop bundle, backend source and installed environment are also copied into `.runtime/hermes`; its `PRESERVATION-HERMES.json` records hashes and relocation. To use that copied Hermes application, fully close the current Hermes app and run `.runtime/hermes/launchHermesDemo.ps1`. Existing authentication and configuration stay in the original Hermes home.
