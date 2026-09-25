"""Headless browser entry point with the existing Jev and run-report contracts."""

from time import perf_counter_ns, time_ns

_MODULE_START_NS = perf_counter_ns()
_MODULE_WALL_NS = time_ns()

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import tomllib
from urllib.parse import urlsplit

# Browser Use configures global logging on import unless disabled. Own the CLI
# handler before importing it so stdout remains reserved for the final JSON.
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"
logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
logging.getLogger("browser_use").setLevel(logging.WARNING)
logging.getLogger("bubus").setLevel(logging.WARNING)
logging.getLogger("cdp_use").setLevel(logging.WARNING)

import httpx2
from dotenv import load_dotenv
from typesafe_sdk import RetryPolicy, TypeSafeClient, TypeSafeError

if __package__:
    from .browser_preview import BrowserPreview
    from .driver import BrowserDriver, DriverError
    from .pipeline import DEFAULT_MAX_ITERATIONS, run_pipeline
    from .reporting import RunReport
    from .selection import ChoiceSelector, PINNED_MODEL, SelectionError
else:
    from browser_preview import BrowserPreview
    from driver import BrowserDriver, DriverError
    from pipeline import DEFAULT_MAX_ITERATIONS, run_pipeline
    from reporting import RunReport
    from selection import ChoiceSelector, PINNED_MODEL, SelectionError

_IMPORT_FINISHED_NS = perf_counter_ns()


async def _run_browser(args, original_request, text, model, api_key, report):
    with report.span("initialization.driver"):
        driver = BrowserDriver(
            executable_path=args.executable_path, user_data_dir=args.user_data_dir,
            profile_directory=args.profile_directory, fresh_profile=args.fresh_profile,
            browser_profile=args.browser_profile,
        )
    result = None
    preview = None
    try:
        report.append("browser_session_configured", **driver.session_info)
        print(f"Browser profile: {args.browser_profile} (persistent; left running after this command).", file=sys.stderr)
        if not driver.runtime.read_state().get("initialized") and not args.fresh_profile:
            print("First use: importing the starting site's login from Chrome. Allow Chrome's debugging prompt if shown.", file=sys.stderr)
        with report.span("initialization.browser"):
            try:
                await driver.start(args.url)
                report.append("browser_session_loaded", **driver.session_info)
            except DriverError as error:
                if args.screenshots:
                    screenshot = await report.capture_screenshot(driver, phase="navigation_error")
                    if screenshot is not None:
                        error.context["screenshot"] = screenshot
                raise
        preview = BrowserPreview(driver.browser) if args.preview else None
        if preview is not None:
            try:
                with report.span("initialization.preview"):
                    preview_url = await preview.start()
                report.append("preview_started", url=preview_url)
                print(f"Live page preview (while running): {preview_url}", file=sys.stderr)
            except Exception as error:
                report.append("preview_error", error_type=type(error).__name__)
                print("Live preview could not start; continuing the run.", file=sys.stderr)
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
                    result = await run_pipeline(
                        original_request, driver, selector,
                        report=report, max_iterations=args.max_iterations, text=text,
                        screenshots=args.screenshots,
                    )
                cleanup_started_ns = perf_counter_ns()
        report.record_interval("client_cleanup", cleanup_started_ns, perf_counter_ns())
        return result
    finally:
        if preview is not None:
            try:
                with report.span("preview_cleanup"):
                    await preview.close()
                report.append("preview_stopped")
            except Exception as error:
                report.append("preview_cleanup_error", error_type=type(error).__name__)
                print("Live preview cleanup failed; see the run report.", file=sys.stderr)
        with report.span("driver_cleanup"):
            try:
                await driver.close()
                report.append("browser_disconnected", browser_profile=args.browser_profile, browser_shutdown_requested=False, profile_preserved=True)
            except Exception as error:
                # Teardown must not hide the action outcome or the original error.
                details = {"error_type": type(error).__name__}
                if isinstance(error, DriverError):
                    details.update(error.details())
                if result is not None:
                    result.observation["cleanup_error"] = details
                _report_failure(report, "driver_cleanup_error", **details)
                print("Browser disconnect failed; the persistent browser was left running. See the run report.", file=sys.stderr)


def main(argv: list[str] | None = None, *, _include_imports: bool = False) -> int:
    entered_ns = perf_counter_ns()
    origin_ns = _MODULE_START_NS if _include_imports else entered_ns
    origin_wall_ns = _MODULE_WALL_NS if _include_imports else time_ns()
    origin_label = "browser_cli_module_after_time_import" if _include_imports else "main_call"
    project_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Read a headless browser, let Jev choose a control and action, and repeat within an iteration limit.",
    )
    parser.add_argument("request", nargs="?", help="Original user request, preserved in every Jev call")
    parser.add_argument("--request-file", type=Path, help="Read the exact request from a UTF-8 file")
    text_source = parser.add_mutually_exclusive_group()
    text_source.add_argument("--text", help="Exact text for the first typing operation; typing stops if omitted, empty, or already used")
    text_source.add_argument("--text-file", type=Path, help="Read first-typing text from a UTF-8 file, preserving whitespace and line endings")
    parser.add_argument("--url", help="Navigate the persistent browser to this HTTP(S) URL; omit to continue its current page")
    parser.add_argument("--browser-profile", default="default",
                        help="Named persistent automation profile (default: %(default)s)")
    parser.add_argument("--executable-path", type=Path, help="Optional Chrome/Chromium executable path")
    parser.add_argument("--user-data-dir", type=Path,
                        help="Source Chrome user-data directory for the first login import only")
    parser.add_argument("--profile-directory",
                        help="Source Chrome profile for the first login import, e.g. Default or 'Profile 2'")
    parser.add_argument("--fresh-profile", action="store_true",
                        help="Initialize a NEW persistent profile signed out; never resets an existing profile")
    parser.add_argument("--screenshots", action=argparse.BooleanOptionalAction, default=True,
                        help="Save viewport PNGs before/after actions and on navigation failure (default: enabled)")
    parser.add_argument("--preview", action=argparse.BooleanOptionalAction, default=True,
                        help="Serve a local live page viewer while the run is active (default: enabled)")
    parser.add_argument("--env-file", type=Path, default=project_dir / ".env")
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS,
                        help="Maximum observation/action cycles (default: %(default)s); not a task-completion check")
    args = parser.parse_args(argv)
    if (args.request is None) == (args.request_file is None):
        parser.error("provide exactly one request argument or --request-file")
    if args.max_iterations < 1:
        parser.error("--max-iterations must be a positive integer")
    if args.fresh_profile and (args.user_data_dir is not None or args.profile_directory is not None):
        parser.error("--fresh-profile cannot be combined with --user-data-dir or --profile-directory")
    try:
        parsed_url = urlsplit(args.url or "")
        valid_url = parsed_url.scheme.lower() in {"http", "https"} and bool(parsed_url.hostname)
        parsed_url.port  # Also reject malformed or out-of-range port numbers.
    except ValueError:
        valid_url = False
    if args.url is not None and not valid_url:
        parser.error("--url must be an absolute HTTP or HTTPS URL")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.getLogger(ChoiceSelector.__module__).setLevel(logging.INFO)
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
        if args.screenshots:
            print(f"Screenshots: {report.requests_path / 'screenshots'}", file=sys.stderr)
        with report.span("initialization.request_read"):
            original_request = (
                args.request_file.read_bytes().decode("utf-8")
                if args.request_file is not None else args.request
            )
        text = args.text
        if args.text_file is not None:
            with report.span("initialization.text_read"):
                text = args.text_file.read_bytes().decode("utf-8")
        report.append(
            "run_started", request=original_request, url=args.url, headless=True,
            screenshots=args.screenshots, preview=args.preview,
            fresh_profile=args.fresh_profile, browser_profile=args.browser_profile,
            **({"text": text} if text is not None else {}),
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
        result = asyncio.run(_run_browser(args, original_request, text, model, api_key, report))
        output_started_ns = perf_counter_ns()
        output = result.to_dict()
        output_finished_ns = perf_counter_ns()
        report.record_interval("result_preparation", output_started_ns, output_finished_ns)
        report.append("run_finished", result=output)
        # No existing Jev question establishes task completion. Native action
        # failures stop with exit 1; bounded/no-action outcomes retain exit 2.
        exit_code = 1 if result.status in {"error", "action_error", "observation_unavailable"} else 2
    except (DriverError, SelectionError, ValueError, OSError, TypeSafeError, KeyError) as error:
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
        output = {
            "status": "error",
            "error": f"Pipeline failed: {type(error).__name__}. Check local configuration and connectivity.",
        }
        _report_failure(report, "run_error", **output)
        exit_code = 1
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
    if report is not None:
        try:
            report.append(event, **details)
        except (OSError, ValueError):
            print("Could not buffer the final report entry.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(_include_imports=True))
