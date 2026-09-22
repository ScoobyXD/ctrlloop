"""Application entry point: python src/main.py 'the original user request'."""

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tomllib

import httpx2
from dotenv import load_dotenv
from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError

if __package__:
    from .driver import CuaDriver, DriverError
    from .pipeline import run_pipeline
    from .reporting import RunReport
    from .selection import ChoiceSelector, PINNED_MODEL, RequestBudget, SelectionError
else:
    # Direct script execution puts src on the import path.
    from driver import CuaDriver, DriverError
    from pipeline import run_pipeline
    from reporting import RunReport
    from selection import ChoiceSelector, PINNED_MODEL, RequestBudget, SelectionError


def main(argv: list[str] | None = None) -> int:
    project_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Select a window and control, optionally executing one CUA click.")
    parser.add_argument("request", nargs="?", help="Original user request, passed unchanged to every Jev call")
    parser.add_argument("--request-file", type=Path, help="Read the exact request from a UTF-8 file")
    parser.add_argument("--env-file", type=Path, default=project_dir / ".env")
    parser.add_argument("--driver", help="Path to the installed cua-driver.exe")
    parser.add_argument("--click", action="store_true", help="Execute one click on the selected real control, return CUA's result, then stop")
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
    report = None
    try:
        report = RunReport(project_dir / "reports")
        print(f"Run report: {report.path}", file=sys.stderr)
        original_request = (
            args.request_file.read_bytes().decode("utf-8")
            if args.request_file is not None else args.request
        )
        report.append("run_started", request=original_request, execute_click=args.click)
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
        with httpx2.Client(timeout=30.0, event_hooks={
            "request": [report.on_request], "response": [report.on_response],
        }) as http_client:
            with TypeSafeClient(
                api_key=api_key, base_url="https://api.typesafe.ai", model=model,
                timeout=30.0, retry=RetryPolicy(max_retries=0), http_client=http_client,
            ) as client:
                result = run_pipeline(
                    original_request, driver, ChoiceSelector(client, model=model, budget=budget),
                    report=report, execute_click=args.click,
                )
        try:
            report.append("run_finished", result=result.to_dict())
        except (OSError, ValueError):
            if result.status != "action_returned":
                raise
            print("Could not save the completed run report; returning the native action result.", file=sys.stderr)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.status in {"selected", "action_returned"} else 2
    except (DriverError, SelectionError, ValueError, OSError, TypeSafeError, KeyError) as error:
        # Raw SDK/OS messages may contain file content, request bodies, or credentials.
        if isinstance(error, (DriverError, SelectionError)) or type(error) is ValueError:
            message = str(error)
        else:
            message = f"Pipeline failed: {type(error).__name__}. Check local configuration and connectivity."
        details = error.details() if isinstance(error, DriverError) else {}
        output = {"status": "error", "error": message, **details}
        _report_failure(report, "run_error", **output)
        print(json.dumps(output, ensure_ascii=False))
        return 1
    except (KeyboardInterrupt, SystemExit) as error:
        _report_failure(report, "run_interrupted", error_type=type(error).__name__)
        raise
    except Exception as error:
        _report_failure(report, "run_error", error_type=type(error).__name__)
        raise
    finally:
        if report is not None:
            try:
                report.close()
            except OSError:
                print("Could not close the run report cleanly; inspect its last complete entry.", file=sys.stderr)


def _report_failure(report: RunReport | None, event: str, **details) -> None:
    """A failing report sink must not replace the original pipeline failure."""
    if report is not None:
        try:
            report.append(event, **details)
        except (OSError, ValueError):
            print("Could not append the final report entry; earlier entries remain on disk.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
