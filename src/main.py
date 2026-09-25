"""Route cached Hermes launches to the preserved demo; run the browser CLI otherwise."""

import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEMO_PROJECT_DIR = PROJECT_DIR.with_name("ctrlloop-hermes-cua-demo-2026-09-25")


def _is_cached_hermes_launch(arguments: list[str]) -> bool:
    """Recognize only the unchanged, already-loaded Hermes adapter's command."""
    if len(arguments) not in (2, 4) or arguments[0] != "--request-file":
        return False
    if len(arguments) == 4 and arguments[2] != "--text-file":
        return False
    try:
        request_file = Path(arguments[1]).resolve(strict=True)
        launches = (PROJECT_DIR / ".local" / "hermes").resolve(strict=True)
        directory = request_file.parent
        if (
            not request_file.is_file()
            or request_file.name != "request.txt"
            or directory.parent != launches
            or not directory.name.startswith("launch-")
        ):
            return False
        if len(arguments) == 4:
            text_file = Path(arguments[3]).resolve(strict=True)
            if not text_file.is_file() or text_file.name != "text.txt" or text_file.parent != directory:
                return False
    except (OSError, RuntimeError):
        return False
    return True


def _run_preserved_demo(arguments: list[str]) -> int:
    forwarded = list(arguments)
    for position in (1, 3):
        if position < len(forwarded):
            forwarded[position] = str(Path(forwarded[position]).resolve())
    python = DEMO_PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    entrypoint = DEMO_PROJECT_DIR / "src" / "main.py"
    driver = DEMO_PROJECT_DIR / ".runtime" / "cua-driver" / "cua-driver.exe"
    try:
        if not all(path.is_file() for path in (python, entrypoint, driver)):
            raise FileNotFoundError
        result = subprocess.run(
            [str(python), str(entrypoint), *forwarded,
             "--env-file", str(PROJECT_DIR / ".env"), "--driver", str(driver)],
            cwd=DEMO_PROJECT_DIR,
            # Explicit handles are needed when Windows detaches the console.
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout,
            stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        return result.returncode
    except OSError as error:
        print(json.dumps({
            "status": "error",
            "error": f"Preserved desktop demo launch failed ({type(error).__name__}). Check the archived runtime.",
        }))
        return 1


def main(argv: list[str] | None = None, *, _include_imports: bool = False) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # This must precede all current SDK/browser imports: a running Hermes process
    # can still hold the original checkout path in its imported plugin module.
    if _is_cached_hermes_launch(arguments):
        return _run_preserved_demo(arguments)
    if __package__:
        from .browser_cli import main as browser_main
    else:
        from browser_cli import main as browser_main
    return browser_main(arguments, _include_imports=_include_imports)


if __name__ == "__main__":
    raise SystemExit(main(_include_imports=True))
