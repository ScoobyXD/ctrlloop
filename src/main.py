"""Application entry point: python src/main.py 'the original user request'."""

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tomllib

from dotenv import load_dotenv
from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError

if __package__:
    from .driver import CuaDriver, DriverError
    from .pipeline import run_pipeline
    from .selection import ChoiceSelector, PINNED_MODEL, RequestBudget, SelectionError
else:
    # Direct script execution puts src on the import path.
    from driver import CuaDriver, DriverError
    from pipeline import run_pipeline
    from selection import ChoiceSelector, PINNED_MODEL, RequestBudget, SelectionError


def main(argv: list[str] | None = None) -> int:
    project_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Read and select one window and control without acting.")
    parser.add_argument("request", nargs="?", help="Original user request, passed unchanged to every Jev call")
    parser.add_argument("--request-file", type=Path, help="Read the exact request from a UTF-8 file")
    parser.add_argument("--env-file", type=Path, default=project_dir / ".env")
    parser.add_argument("--driver", help="Path to the installed cua-driver.exe")
    parser.add_argument("--max-elements", type=int, default=5000)
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-request-bytes", type=int, default=48_000)
    parser.add_argument("--max-question-bytes", type=int, default=24_000)
    args = parser.parse_args(argv)
    if (args.request is None) == (args.request_file is None):
        parser.error("provide exactly one request argument or --request-file")
    # Keep JSON output usable for non-ASCII UI labels on Windows consoles/pipes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger(ChoiceSelector.__module__).setLevel(logging.INFO)
    # SDK debug logs can contain UI state; this CLI logs only the model identity.
    logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)
    try:
        original_request = (
            args.request_file.read_bytes().decode("utf-8")
            if args.request_file is not None else args.request
        )
        if not original_request.strip():
            raise ValueError("The original request must be nonempty.")
        config = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
        model = config["tool"]["ctrlloop"]["jev-model"]
        if model != PINNED_MODEL:
            raise ValueError(f"pyproject.toml must retain the experiment's {PINNED_MODEL} pin.")
        load_dotenv(args.env_file, override=False, interpolate=False)
        api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY is missing; set it in the environment or --env-file.")
        budget = RequestBudget(args.max_request_bytes, args.max_question_bytes)
        driver = CuaDriver(
            executable=args.driver, max_elements=args.max_elements, max_depth=args.max_depth
        )
        with TypeSafeClient(
            api_key=api_key, base_url="https://api.typesafe.ai", model=model,
            timeout=30.0, retry=RetryPolicy(max_retries=0),
        ) as client:
            result = run_pipeline(original_request, driver, ChoiceSelector(client, model=model, budget=budget))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.status == "selected" else 2
    except (DriverError, SelectionError, ValueError, OSError, TypeSafeError, KeyError) as error:
        # Raw SDK/OS messages may contain file content, request bodies, or credentials.
        if isinstance(error, (DriverError, SelectionError)) or type(error) is ValueError:
            message = str(error)
        else:
            message = f"Pipeline failed: {type(error).__name__}. Check local configuration and connectivity."
        print(json.dumps({"status": "error", "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
