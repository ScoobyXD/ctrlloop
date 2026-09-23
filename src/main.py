"""Application entry point: python src/main.py 'the original user request'."""

# Earliest practical Python boundary: excludes interpreter/process launch and
# importing time itself, but includes all remaining application/SDK imports.
from time import perf_counter_ns, time_ns

_MODULE_START_NS = perf_counter_ns()
_MODULE_WALL_NS = time_ns()

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
    from .pipeline import DEFAULT_MAX_ITERATIONS, run_pipeline
    from .reporting import RunReport
    from .selection import ChoiceSelector, PINNED_MODEL, SelectionError
else:
    # Direct script execution puts src on the import path.
    from driver import CuaDriver, DriverError
    from pipeline import DEFAULT_MAX_ITERATIONS, run_pipeline
    from reporting import RunReport
    from selection import ChoiceSelector, PINNED_MODEL, SelectionError

_IMPORT_FINISHED_NS = perf_counter_ns()


def main(argv: list[str] | None = None, *, _include_imports: bool = False) -> int:
    entered_ns = perf_counter_ns()
    origin_ns = _MODULE_START_NS if _include_imports else entered_ns
    origin_wall_ns = _MODULE_WALL_NS if _include_imports else time_ns()
    origin_label = "main_module_after_time_import" if _include_imports else "main_call"
    project_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Select a window, then repeat Jev control selection and CUA actions within an iteration limit.")
    parser.add_argument("request", nargs="?", help="Original user request, preserved in every Jev call")
    parser.add_argument("--request-file", type=Path, help="Read the exact request from a UTF-8 file")
    parser.add_argument("--text", help="Exact Windows Search text override; otherwise select words from the request")
    parser.add_argument("--delivery-mode", choices=("foreground", "background"),
                        help="Override targeted typing delivery (default: foreground for known browsers, native default otherwise); does not affect Windows Search desktop typing or clicks")
    parser.add_argument("--env-file", type=Path, default=project_dir / ".env")
    parser.add_argument("--driver", help="Path to cua-driver.exe")
    parser.add_argument("--driver-socket", help=r"Named pipe of an already-running CUA daemon (default: \\.\pipe\cua-driver)")
    parser.add_argument("--max-elements", type=int, default=5000)
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS,
                        help="Maximum observation/action cycles (default: %(default)s); not a task-completion check")
    args = parser.parse_args(argv)
    if (args.request is None) == (args.request_file is None):
        parser.error("provide exactly one request argument or --request-file")
    if args.max_iterations < 1:
        parser.error("--max-iterations must be a positive integer")
    if args.text is not None and not args.text.strip():
        parser.error("--text must contain non-whitespace text")
    # Keep JSON output usable for non-ASCII UI labels on Windows consoles/pipes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger(ChoiceSelector.__module__).setLevel(logging.INFO)
    # SDK debug logs can contain UI state; this CLI logs only the model identity.
    logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)
    report = None
    output = None
    exit_code = 1
    try:
        report_initialization_ns = perf_counter_ns()
        report = RunReport(
            project_dir / "reports", origin_ns=origin_ns,
            origin_wall_ns=origin_wall_ns, origin_label=origin_label,
        )
        report_ready_ns = perf_counter_ns()
        report.append(
            "timing_origin", _at_ns=origin_ns, origin=origin_label,
            clock="perf_counter_ns", wall_clock="UTC anchor projected with monotonic elapsed time",
            excludes="process/interpreter launch and imports before the origin",
        )
        if _include_imports:
            report.record_interval("startup.imports", _MODULE_START_NS, _IMPORT_FINISHED_NS)
        report.record_interval("startup.cli", entered_ns, report_initialization_ns)
        report.record_interval("startup.report_initialization", report_initialization_ns, report_ready_ns)
        print(f"Run report: {report.path}", file=sys.stderr)
        print(f"Jev input/output JSON: {report.requests_path}", file=sys.stderr)
        with report.span("initialization.request_read"):
            original_request = (
                args.request_file.read_bytes().decode("utf-8")
                if args.request_file is not None else args.request
            )
        report.append(
            "run_started", request=original_request,
            **({"text": args.text} if args.text is not None else {}),
            **({"delivery_mode": args.delivery_mode} if args.delivery_mode is not None else {}),
        )
        with report.span("initialization.configuration"):
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
        with report.span("initialization.driver"):
            driver = CuaDriver(
                executable=args.driver, max_elements=args.max_elements, max_depth=args.max_depth,
                socket=args.driver_socket,
            )
        try:
            http_initialization_ns = perf_counter_ns()
            with httpx2.Client(timeout=30.0, event_hooks={
                "request": [report.on_request], "response": [report.on_response],
            }) as http_client:
                report.record_interval("initialization.http_client", http_initialization_ns, perf_counter_ns())
                sdk_initialization_ns = perf_counter_ns()
                with TypeSafeClient(
                    api_key=api_key, base_url="https://api.typesafe.ai", model=model,
                    timeout=30.0, retry=RetryPolicy(max_retries=0), http_client=http_client,
                ) as client:
                    report.record_interval("initialization.typesafe_client", sdk_initialization_ns, perf_counter_ns())
                    with report.span("initialization.selector"):
                        selector = ChoiceSelector(client, model=model)
                    with report.span("pipeline"):
                        result = run_pipeline(
                            original_request, driver, selector,
                            report=report, max_iterations=args.max_iterations, text=args.text,
                            delivery_mode=args.delivery_mode,
                        )
                    cleanup_started_ns = perf_counter_ns()
            report.record_interval("client_cleanup", cleanup_started_ns, perf_counter_ns())
        finally:
            with report.span("driver_cleanup"):
                driver.close()
        # Finish preparing the operational result before generating the report.
        output_started_ns = perf_counter_ns()
        output = result.to_dict()
        output_finished_ns = perf_counter_ns()
        report.record_interval("result_preparation", output_started_ns, output_finished_ns)
        report.append("run_finished", result=output)
        # No question in this workflow establishes that the user's task is done.
        exit_code = 2
    except (DriverError, SelectionError, ValueError, OSError, TypeSafeError, KeyError) as error:
        # Raw SDK/OS messages may contain file content, request bodies, or credentials.
        if isinstance(error, (DriverError, SelectionError)) or type(error) is ValueError:
            message = str(error)
        else:
            message = f"Pipeline failed: {type(error).__name__}. Check local configuration and connectivity."
        details = error.details() if isinstance(error, DriverError) else {}
        output = {"status": "error", "error": message, **details}
        _report_failure(report, "run_error", **output)
        exit_code = 1
    except (KeyboardInterrupt, SystemExit) as error:
        _report_failure(report, "run_interrupted", error_type=type(error).__name__)
        raise
    except Exception as error:
        _report_failure(report, "run_error", error_type=type(error).__name__)
        raise
    finally:
        if report is not None:
            try:
                timing = report.close()
                if output is not None:
                    output["timing"] = timing
            except (OSError, ValueError, RuntimeError) as error:
                if output is not None and output.get("action_result") is not None:
                    print("Could not save all run-report files; returning the native action result.", file=sys.stderr)
                else:
                    print("Could not write all final run-report files.", file=sys.stderr)
                    if output is not None and output["status"] != "error":
                        output = {
                            "status": "error",
                            "error": f"Pipeline failed: {type(error).__name__}. Check local configuration and connectivity.",
                        }
                        exit_code = 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code


def _report_failure(report: RunReport | None, event: str, **details) -> None:
    """A failing report sink must not replace the original pipeline failure."""
    if report is not None:
        try:
            report.append(event, **details)
        except (OSError, ValueError):
            print("Could not buffer the final report entry.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(_include_imports=True))
