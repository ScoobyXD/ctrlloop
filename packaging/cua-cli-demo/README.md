# ctrlloop: original CUA CLI demo

Type a request in a terminal and let Jev select windows and controls on your
visible Windows desktop. This is the original CLI from September 23, 2026,
before the Hermes integration, with its Python runtime and patched CUA driver
included. No Python installation, Hermes, headless browser or build step is needed.

## Start

1. Extract the entire ZIP to a writable folder on a **64-bit Windows** PC.
2. Double-click **demo.cmd**.
3. Enter your own **TypeSafe/Jev API key** when prompted. It is hidden while you
   type and is kept only for this process. Internet access to the Jev API is required.
4. Open the app you want to control, then type your request at the prompt.

For an initial demo, open Calculator and enter a simple request such as
`Click the 7 button in Calculator.` The default limit is 10 observation/action
loops. Use a one-loop command for a single button demonstration:

```powershell
.\demo.cmd "Click the 7 button in Calculator." --max-iterations 1
```

You can also run `demo.cmd` without arguments to enter a request at a prompt.
Launch it again for another request. Press **Ctrl+C** to interrupt a running request. Keep Windows
unlocked and use your normal interactive desktop; this program really operates
your open applications, including focusing them and typing.

To remember your key locally, copy `.env.example` to `.env` in this folder and
fill in `TYPESAFE_API_KEY`. An existing `TYPESAFE_API_KEY` environment variable
also works. No key or personal reports are included in the delivered bundle.

## Check the package

```powershell
.\demo.cmd --check
.\demo.cmd --help
```

The check verifies imports, pinned versions and the bundled CUA connection and
click capability. It does not call Jev or operate desktop controls. Actual task
execution still needs a valid API key and an accessible Windows application.

The launcher starts a hidden instance of the included CUA driver on a private
pipe and closes that instance on completion or Ctrl+C. It uses the existing
CLI's `--driver` and `--driver-socket` options. It does not install a Windows
service or scheduled task. Driver telemetry is disabled. Local driver state
and logs are stored under `.local/` in this folder.

## Original behavior and limits

- Jev stays pinned to **jev-1.13.0**, with the original authored questions.
- Window and control selection groups contain up to 10 candidates. Questions
  are batched using the original request sizing logic.
- Typing selects words from your request in their original order using the old
  threshold. It is not a general text-generation system. `--text` is the
  original Windows Search override, not a universal exact-text option.
- Clicks use the patched driver's native Windows accessibility activation.
  Some custom controls expose no supported accessibility action and will fail.
- The CLI has no task-completion question. Exit code **2** means a bounded run
  ended without establishing completion, even if the visible action worked;
  **1** indicates an error. Read the final JSON result as well as the screen.
- Reports under `reports/` contain requests, observed UI content, Jev questions,
  probabilities and timings. Keep those and `.env` private when sharing again.
- This package targets ordinary desktop apps at the same privilege level as
  the terminal. Elevated apps and the UAC secure desktop are outside this demo.

The CUA driver is a locally patched build. If Windows or your organization's
security policy prevents an executable from running, review the package with
the sender or your administrator; the launcher does not change security policy.

## Source

Original entry point: `src/main.py`, plus the other eight modules in `src/`.
All application modules, questions, `pyproject.toml` and `uv.lock` are unchanged
from commit [`2d30d84b269e8a0493f01ff55ddb2b1ab8acaa17`](https://github.com/ScoobyXD/ctrlloop/tree/2d30d84b269e8a0493f01ff55ddb2b1ab8acaa17),
the parent of the first Hermes integration commit (`25108d8`).

`launch.py` and `demo.cmd` provide portable startup around that existing CLI.
`PROVENANCE.json` records source and binary hashes. See
`THIRD_PARTY_NOTICES.md` for included component licenses.
