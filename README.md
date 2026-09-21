# ctrlloop

The first Python milestone discovers desktop windows, uses Jev to choose one,
reads that window's Windows UI Automation state once, and uses Jev to choose one
observed control. The result is JSON containing the original window and control
records, snapshot identity, observation quality, and selection rounds.

Selection retains identities in Python. It does not activate or focus windows,
click, type, invoke controls, launch applications, or take screenshots. A request
such as “Click Recycle Bin” only selects a candidate.

## Run

From the repository root, with Python 3.11+ and `uv`:

```powershell
uv sync --locked
# Set TYPESAFE_API_KEY in your process environment or local .env (not committed).
uv run --locked python src/main.py 'Find the Recycle Bin control on the desktop.'
```

The dependencies remain pinned to `typesafe-sdk==0.7.1` and
`python-dotenv==1.2.3`. The existing `pyproject.toml` pin is `jev-1.13.0`;
the pipeline requires that exact model and validates every response model.
Requested and returned model identities are logged to stderr; results are JSON
on stdout. No credentials, full prompt, or full desktop tree are logged.

CUA Driver 0.28.2 must already be installed with its daemon running on the
interactive Windows desktop. The adapter finds `cua-driver` on PATH or in its
default Windows install directory; `--driver <path>` overrides the executable.
It does not start a daemon. See [driver setup](docs/CUA_DRIVER.md).

For a multiline request, `--request-file request.txt` reads UTF-8 bytes without
trimming or normalizing line endings. Use `--env-file <path>` to read an existing
credential file without copying it. Existing environment variables take priority.

```powershell
uv run --locked python src/main.py --request-file request.txt --max-elements 5000 --max-depth 25
uv run --locked python -m unittest discover -s tests -v
```

The source modules live directly in `src/` and feed into `src/main.py`:

- `driver.py` discovers windows and reads the selected window's accessibility state.
- `selection.py` batches Jev Choice questions and advances one winner per group.
- `pipeline.py` connects window discovery, window selection, the snapshot read, and control selection.
- `main.py` provides the CLI, loads configuration, and prints the result.

From the repository root, `uv run --locked python -m src.main` also runs the CLI.
The modules are importable from the checkout:

```python
from typesafe_sdk import RetryPolicy, TypeSafeClient
from src.driver import CuaDriver
from src.pipeline import run_pipeline
from src.selection import ChoiceSelector, PINNED_MODEL

with TypeSafeClient(model=PINNED_MODEL, retry=RetryPolicy(max_retries=0)) as client:
    result = run_pipeline(original_request, CuaDriver(), ChoiceSelector(client))
    print(result.to_dict())
```

The Python API raises `DriverError` or `SelectionError` for failures. The CLI
reports them as `{"status":"error","error":"..."}` with exit code 1.

| Status | Meaning | CLI exit |
| --- | --- | --- |
| `selected` | One observed window and one observed control chosen | 0 |
| `no_windows` | Discovery returned an empty window list | 2 |
| `no_controls` | A valid read returned no indexed controls | 2 |
| `observation_degraded` | A degraded read returned no indexed controls | 2 |
| `observation_unavailable` | Structured controls or their snapshot identity were unavailable | 2 |
| `error` | Driver, configuration, or Jev request/response failure | 1 |

Nonempty degraded observations remain selectable and retain their quality flags.
`selected` means selection completed, not that the user's original UI operation
was performed or that the chosen control is guaranteed to fulfill it.

## Selection contract

Both stages preserve the driver's observed order. Each group contains at most ten
candidates and receives one Choice question. One winner from every group advances
in group order; winners are regrouped in tens until one remains. A singleton is
forwarded in Python without an API request. For 1,000 candidates, the round counts
are `1000 -> 100 -> 10 -> 1`. Group size ten is fixed for this experiment.

Every question uses this exact text:

> Which of these candidates is best suited to help fulfill the user’s original request, based on the provided candidate information?

Every Jev request sends the unchanged original request once as shared `state`.
Choice options contain concise descriptions of the actual records, with stable
local labels mapped back to those records. Window descriptions use title, app,
visibility, minimization, and geometry. Controls use role, label/value, advertised
capabilities, available state, geometry, and up to two explicitly linked indexed
ancestors. Description text is capped at 240 characters (120 for ancestor labels);
the full observed records remain local and the original prompt is never shortened.
No PID, HWND, snapshot token, screenshot, or redundant full tree is sent as state.

Independent group questions are packed together; the next round begins only after
all batches in the preceding round finish. The default packing caps are 48,000
UTF-8 JSON bytes per request and 24,000 bytes for the state plus one question and
request envelope. `--max-request-bytes` and `--max-question-bytes` configure these
caps. These are conservative **byte heuristics, not exact token counts or a service
limit guarantee**. They leave headroom for the documented 64k total and 32k
state-plus-longest-question limits. An oversized group fails explicitly; the code
does not split its options or silently truncate the prompt. Actual input/output
token usage is retained per batch. Review it when adjusting caps.

Returned model identity, question keys, answer type, option labels, probability
shape, and usage are validated. Confidence and probabilities never rank winners
across groups or gate advancement. There are no scores, thresholds, beams, or
synthetic “none” options.

## Observation limits

The driver is called only through `list_windows({})` and `get_window_state` with
the selected record's exact `pid`/`window_id`, accessibility enabled and screenshots
disabled. The adapter separates stdout/stderr and validates JSON and success
shape, because some driver failures print plain text with exit code zero.

Controls come from one saved snapshot. `element_index` is not an array offset;
parent context comes only from explicit links to observed records. The structured
`elements` array omits some containers visible in `tree_markdown`, and the default
walk is bounded at 5,000 nodes / depth 25. This pipeline does not infer missing
ancestry or claim full coverage. Windows 0.28.2 commonly reports
`elements_complete:false`, including successful reads; that alone is not failure.

Window identities are live OS identities. Control indices/tokens are bound to
the recorded snapshot and its transport lifecycle. One-shot CLI transports end
after their response, so exported handles are observation evidence, not reusable
action authorization or a promise that a later call can use them. Any future
action milestone needs its own fresh observation and lifecycle design.

Implementation references: [TypeSafe Python usage](https://docs.typesafe.ai/sdk/python/usage),
[Choice](https://docs.typesafe.ai/primitives/choice),
[API](https://docs.typesafe.ai/api),
[verified CUA contracts](docs/cua-driver/0.28.2/README.md), and the
[CUA Driver skill](skills/cua-driver-skill-0.28.2/SKILL.md) with its
[Windows companion](skills/cua-driver-skill-0.28.2/WINDOWS.md).
