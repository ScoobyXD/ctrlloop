"""Check TypeSafe access with the production selection and timer questions."""

import os
from pathlib import Path
import sys
import tomllib

from dotenv import load_dotenv
from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError

if not __package__:
    # Direct script execution puts scripts, rather than the project root, on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.selection import Candidate, ChoiceSelector, PINNED_MODEL, SelectionError


def main() -> int:
    project_dir = Path(__file__).resolve().parents[1]
    load_dotenv(project_dir / ".env", override=False, interpolate=False)
    config = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    model = config["tool"]["ctrlloop"]["jev-model"]
    if model != PINNED_MODEL:
        print(f"pyproject.toml must pin {PINNED_MODEL}.", file=sys.stderr)
        return 1

    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        print(
            "TYPESAFE_API_KEY is missing. Add it to the local .env file "
            "or set it in your process environment.",
            file=sys.stderr,
        )
        return 1

    # One fictional control yields exactly one selection and one timer question.
    # No desktop observation or action is performed by this connection check.
    window = {"app_name": "connection_check", "title": "Connection check fixture"}
    state = {
        "original_request": "Click the Test button in the connection check fixture.",
        "iteration": 1,
        "current_window": window,
        "last_control": None,
    }
    candidate = Candidate(
        "test_button", {"role": "Button", "label": "Test", "actions": ["click"]},
        {"role": "Button", "label": "Test", "synthetic": True},
    )
    try:
        with TypeSafeClient(
            api_key=api_key,
            base_url="https://api.typesafe.ai",
            model=model,
            timeout=30.0,
            retry=RetryPolicy(max_retries=0),
        ) as client:
            selection = ChoiceSelector(client, model=model).select(
                state, [candidate], stage="controls", settling_context=window,
            )
    except SelectionError as error:
        # Production validation reports only safe structural/request diagnostics.
        print(f"TypeSafe check failed: {error}", file=sys.stderr)
        return 1
    except TypeSafeError as error:
        # Report only the error class/status, never credentials or raw HTTP content.
        status = getattr(error, "status", None)
        print(f"TypeSafe check failed: {type(error).__name__} (HTTP {status}).", file=sys.stderr)
        return 1

    trace = selection.rounds[0]["api_calls"][0]
    print(f"Requested model: {model}; response model: {trace['response_model']}")
    for name, answer in trace["answers"].items():
        print(f"{name}: {answer['choice']}")
    print(f"Tokens: {trace['input_tokens']} input, {trace['output_tokens']} output")
    print("Connection check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
