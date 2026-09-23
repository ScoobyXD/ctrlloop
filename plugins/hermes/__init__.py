"""Minimal Hermes adapter: launch the existing CLI and acknowledge the launch."""

import os
from pathlib import Path
import subprocess
import tempfile

from tools.registry import tool_error, tool_result


# resolve() follows the user-plugin junction back to this editable checkout.
PROJECT_DIR = Path(__file__).resolve().parents[2]


def run(args: dict, **kwargs) -> str:
    """Return a Hermes JSON string; never wait for or summarize the child run."""
    if not isinstance(args, dict) or set(args) != {"request"}:
        return tool_error("Provide exactly one field: request (a nonempty string).")
    request = args["request"]
    if not isinstance(request, str) or not request.strip():
        return tool_error("request must be a nonempty string.")
    if os.name != "nt":
        return tool_error("This ctrlloop launcher requires Windows.")

    python = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    main = PROJECT_DIR / "src" / "main.py"
    if not python.is_file() or not main.is_file():
        return tool_error("ctrlloop requires .venv/Scripts/python.exe and src/main.py in its source checkout.")

    try:
        # Use the existing --request-file contract, preserving UTF-8 bytes and
        # line endings without putting user text into a shell or command line.
        request_bytes = request.encode("utf-8")
        launches = PROJECT_DIR / ".local" / "hermes"
        launches.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="launch-", dir=launches))
        request_file = directory / "request.txt"
        request_file.write_bytes(request_bytes)
        with (directory / "stdout.json").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                [str(python), str(main), "--request-file", str(request_file)],
                cwd=PROJECT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    except (OSError, UnicodeError) as error:
        # OS errors can embed paths or request contents; return only the type.
        return tool_error(f"ctrlloop launch failed ({type(error).__name__}). Check the local checkout and file permissions.")

    return tool_result(status="launched", pid=process.pid, directory=str(directory))


def register(ctx) -> None:
    ctx.register_tool(
        name="ctrlloop_run",
        toolset="ctrlloop",
        schema={
            "name": "ctrlloop_run",
            "description": (
                "Launch a computer-use task in the local ctrlloop program; Jev selects the UI actions. "
                "Pass the user's original task text unchanged. Returns only a process launch receipt, "
                "not execution success or task completion. No result retrieval or Stop forwarding is connected. "
                "Do not launch another task or perform other computer-use actions while it may still be running."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The original user task text, passed verbatim to ctrlloop. No conversation history is added.",
                    },
                },
                "required": ["request"],
                "additionalProperties": False,
            },
        },
        handler=run,
    )
