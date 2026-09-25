"""Hermes adapter for the preserved desktop demo and its original tool contract."""

import os
from pathlib import Path
import subprocess
import tempfile

from tools.registry import tool_error, tool_result


# The existing user-plugin junction stays valid. New Hermes imports launch the
# archived demo directly; already-loaded adapters are handled by src/main.py.
SOURCE_PROJECT_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = SOURCE_PROJECT_DIR.with_name("ctrlloop-hermes-cua-demo-2026-09-25")


def run(args: dict, **kwargs) -> str:
    """Return a Hermes JSON string; never wait for or summarize the child run."""
    if not isinstance(args, dict) or "request" not in args or set(args) - {"request", "text"}:
        return tool_error("Provide request (a nonempty string) and optionally text (a string).")
    request = args["request"]
    if not isinstance(request, str) or not request.strip():
        return tool_error("request must be a nonempty string.")
    text = args.get("text", "")
    if not isinstance(text, str):
        return tool_error("text must be a string when provided.")
    if os.name != "nt":
        return tool_error("This ctrlloop launcher requires Windows.")

    python = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    main = PROJECT_DIR / "src" / "main.py"
    driver = PROJECT_DIR / ".runtime" / "cua-driver" / "cua-driver.exe"
    if not python.is_file() or not main.is_file():
        return tool_error("ctrlloop requires .venv/Scripts/python.exe and src/main.py in its source checkout.")
    if not driver.is_file():
        return tool_error("The preserved ctrlloop demo requires its archived CUA driver.")

    try:
        request_bytes = request.encode("utf-8")
        text_bytes = text.encode("utf-8")
        launches = PROJECT_DIR / ".local" / "hermes"
        launches.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="launch-", dir=launches))
        request_file = directory / "request.txt"
        request_file.write_bytes(request_bytes)
        command = [str(python), str(main), "--request-file", str(request_file),
                   "--env-file", str(SOURCE_PROJECT_DIR / ".env"), "--driver", str(driver)]
        if text:
            text_file = directory / "text.txt"
            text_file.write_bytes(text_bytes)
            command.extend(["--text-file", str(text_file)])
        with (directory / "stdout.json").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    except (OSError, UnicodeError) as error:
        return tool_error(f"ctrlloop launch failed ({type(error).__name__}). Check the local checkout and file permissions.")

    return tool_result(status="launched", pid=process.pid, directory=str(directory))


def register(ctx) -> None:
    ctx.register_tool(
        name="ctrlloop_run",
        toolset="ctrlloop",
        schema={
            "name": "ctrlloop_run",
            "description": (
                "Use for desktop or app interaction. Launch receipt only; no completion reporting or Stop. "
                "Do not overlap other computer-use actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Turn the user's intent and conversation context into a concise English computer-use command.",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Prepare the exact text for the first typing step (search, URL, or message). "
                            "Required for typing; used once."
                        ),
                    },
                },
                "required": ["request"],
                "additionalProperties": False,
            },
        },
        handler=run,
    )
