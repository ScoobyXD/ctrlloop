"""Portable launcher for the unchanged, pre-Hermes ctrlloop CLI."""

from __future__ import annotations

import argparse
import getpass
from importlib.metadata import version
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import tomllib
import uuid


ROOT = Path(__file__).resolve().parent
# demo.cmd uses isolated Python, so explicitly add only this bundle's source.
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def check_dependencies() -> str:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in config["project"]["dependencies"]:
        name, expected = requirement.split("==", 1)
        actual = version(name)
        if actual != expected:
            raise RuntimeError(f"Bundled {name} must be {expected}; found {actual}.")
    from src.selection import PINNED_MODEL

    if config["tool"]["ctrlloop"]["jev-model"] != PINNED_MODEL:
        raise RuntimeError("The bundled Jev model pin does not match its source.")
    return PINNED_MODEL


def configure_driver_environment(instance: str) -> None:
    # These are the pinned CUA driver's existing home and telemetry settings.
    home = ROOT / ".local" / instance / "cua-driver"
    home.mkdir(parents=True, exist_ok=True)
    for name in ("CUA_DRIVER_HOME", "CUA_DRIVER_RS_HOME", "CUA_DRIVER_LOCAL_HOME"):
        os.environ[name] = str(home)
    os.environ["CUA_DRIVER_TELEMETRY_HOME"] = str(home / "telemetry")
    os.environ["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
    os.environ["CUA_TELEMETRY_ENABLED"] = "0"
    os.environ["CUA_DRIVER_RS_UPDATE_CHECK"] = "false"
    os.environ["CUA_DRIVER_PERMISSION_MODE"] = "standard"
    os.environ["CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS"] = "0"
    # Windows serve uses LOCALAPPDATA for its PID file even with a custom
    # socket. Isolate that path for the daemon, MCP clients and stop client.
    local_app_data = home.parent / "local-app-data"
    local_app_data.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(local_app_data)


def require_api_key(arguments: list[str]) -> None:
    from dotenv import load_dotenv

    # Read the existing CLI option without inventing a second CLI definition.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    options, _ = parser.parse_known_args(arguments)
    load_dotenv(options.env_file, override=False, interpolate=False)
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        if not sys.stdin.isatty():
            raise RuntimeError("Set TYPESAFE_API_KEY or add your own key to the bundle's .env file.")
        key = getpass.getpass("Your TypeSafe API key (used for this run; not saved): ").strip()
        if not key:
            raise RuntimeError("A TypeSafe API key is required.")
        os.environ["TYPESAFE_API_KEY"] = key


def wait_for_driver(executable: Path, pipe: str, process: subprocess.Popen) -> None:
    from src.cua_mcp import McpTransport
    from src.driver import DriverError

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The bundled CUA driver exited during startup. See its .local log.")
        transport = McpTransport(str(executable), timeout=3.0, socket=pipe)
        try:
            # Reuse the pinned transport and its exact click-schema validation.
            # Initialization sets session cursor glide only; it does not move
            # the cursor or inspect/operate desktop applications.
            attempt_deadline = min(deadline, time.monotonic() + 3.0)
            transport._connect(attempt_deadline)
            transport._require_accessibility_click(attempt_deadline)
            return
        except DriverError as error:
            if error.code == "direct_accessibility_unsupported":
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError("The bundled CUA driver did not become ready. See its .local log.") from None
        finally:
            transport.close()
        time.sleep(0.15)
    raise RuntimeError("The bundled CUA driver did not become ready. See its .local log.")


def stop_driver(executable: Path, pipe: str, process: subprocess.Popen) -> None:
    # Ignore a second Ctrl+C only while releasing the process this launcher owns.
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if process.poll() is None:
            try:
                subprocess.run(
                    [str(executable), "stop", "--socket", pipe, "--expected-pid", str(process.pid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=8.0, check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                process.wait(timeout=3.0)
            except (OSError, subprocess.TimeoutExpired):
                # The retained Popen handle cannot target another daemon.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5.0)
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def main() -> int:
    arguments = sys.argv[1:]
    if os.name != "nt":
        raise RuntimeError("This demo requires Windows x64 and an interactive desktop.")
    from src.main import main as run_cli

    if "--help" in arguments or "-h" in arguments:
        print("Portable demo: demo.cmd [request] [original CLI options]")
        print("demo.cmd --check checks local dependencies and driver readiness without Jev or desktop actions.\n")
        return run_cli(["--help"])
    check_only = arguments == ["--check"]
    if "--check" in arguments and not check_only:
        raise RuntimeError("Use --check on its own.")
    model = check_dependencies()
    executable = ROOT / "runtime" / "cua-driver" / "cua-driver.exe"
    for name in ("cua-driver.exe", "cua-driver-uia.exe", "cua-cursor-theme.exe"):
        if not (executable.parent / name).is_file():
            raise RuntimeError(f"Missing bundled driver file: {name}. Extract the complete ZIP first.")
    if not check_only:
        require_api_key(arguments)
    if not arguments:
        print("ctrlloop desktop demo — Jev + CUA Driver")
        print("Keep the app you want to use open. Press Ctrl+C to stop.\n")
        request = input("What should I do? ").strip()
        if not request:
            return 0
        arguments = [request]
    instance = uuid.uuid4().hex
    configure_driver_environment(instance)
    pipe = rf"\\.\pipe\ctrlloop-demo-{instance}"
    log_path = ROOT / ".local" / f"cua-driver-{instance}.log"
    print(f"Starting bundled CUA Driver. Log: {log_path}", file=sys.stderr)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(executable), "serve", "--socket", pipe, "--permission-mode", "standard"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            wait_for_driver(executable, pipe, process)
            if check_only:
                print(f"Ready: bundled Python dependencies, {model}, and CUA direct-accessibility schema.")
                print("No Jev request or desktop observation/action was performed.")
                return 0
            # The final option values select only our private daemon, even if
            # callers supplied the original CLI's driver options themselves.
            boundary = arguments.index("--") if "--" in arguments else len(arguments)
            arguments[boundary:boundary] = ["--driver", str(executable), "--driver-socket", pipe]
            return run_cli(arguments)
        finally:
            stop_driver(executable, pipe, process)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        # Authored startup messages are safe; dependency/OS details can contain
        # private paths or configuration, so keep unexpected failures concise.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"Demo could not run: {message}", file=sys.stderr)
        raise SystemExit(1)
