"""Persistent, detached Chrome processes shared by successive ctrlloop commands.

Only the command lease is released at command end. Chrome owns its durable
profile and remains running independently of Python until Windows shuts down.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import psutil


_ROOT = Path(__file__).resolve().parents[1]
_PROFILES = _ROOT / ".local" / "browsers"
# Discovery/error paths can import Browser Use; the login helper stays quiet.
os.environ.setdefault("BROWSER_USE_SETUP_LOGGING", "false")
os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(_ROOT / ".local" / "browser-use"))
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "CtrlloopBrowsers"
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def _failure(message: str, code: str):
    # Reuse the adapter's public error shape without loading Browser Use at login.
    if __package__:
        from .driver import DriverError
    else:
        from driver import DriverError
    return DriverError(message, code=code)


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def _flag(arguments: list[str], name: str) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.startswith(name + "="):
            return argument.partition("=")[2].strip('"')
        if argument == name and index + 1 < len(arguments):
            return arguments[index + 1].strip('"')
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_directory(path: Path) -> None:
    """Reject redirected managed directories before creating or using files."""
    for directory in (path, *path.parents):
        if directory == _ROOT:
            break
        if directory.exists():
            attributes = getattr(directory.lstat(), "st_file_attributes", 0)
            if directory.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise _failure("The managed browser directory cannot be a link or junction.",
                               "browser_runtime_directory_invalid")
    if not path.resolve().is_relative_to(_PROFILES.resolve()):
        raise _failure("The managed browser directory is outside ctrlloop's profile root.",
                       "browser_runtime_directory_invalid")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


class BrowserRuntime:
    """Lease one named profile and start or reconnect its independent browser."""

    def __init__(self, profile: str = "default", executable_path: str | Path | None = None,
                 viewport: dict[str, int] | None = None) -> None:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", profile) or profile in _RESERVED:
            raise _failure("Browser profile names must be lowercase letters, numbers, or hyphens "
                           "(1-64 characters), excluding Windows device names.",
                           "browser_runtime_profile_invalid")
        self.profile = profile
        self.directory = _PROFILES / profile
        self.profile_path = self.directory / "user-data"
        self._state_path = self.directory / "runtime.json"
        self._lease: BinaryIO | None = None
        self._executable = str(Path(executable_path).resolve()) if executable_path is not None else None
        self._viewport = viewport
        self.metadata: dict[str, Any] = {
            "automation_profile": profile, "user_data_dir": str(self.profile_path),
            "persistent": True, "headless": True,
        }
        _check_directory(self.profile_path)

    def acquire(self) -> None:
        """Fail fast when another command controls this profile; crash releases it."""
        if self._lease is not None:
            return
        _check_directory(self.profile_path)
        self.directory.mkdir(parents=True, exist_ok=True)
        lease = (self.directory / "command.lock").open("a+b")
        try:
            lease.seek(0, os.SEEK_END)
            if lease.tell() == 0:
                lease.write(b"\0")
                lease.flush()
            lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lease.close()
            raise _failure(f"Browser profile '{self.profile}' is already in use by another command. "
                           "Wait for that command to finish, or choose a different --browser-profile.",
                           "browser_runtime_busy") from None
        self._lease = lease

    def release(self) -> None:
        """Release only this command's lease. Never terminate Chrome or erase data."""
        lease, self._lease = self._lease, None
        if lease is None:
            return
        try:
            lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        finally:
            lease.close()

    def read_state(self) -> dict[str, Any]:
        _check_directory(self.profile_path)
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise _failure("The persistent browser's runtime metadata cannot be read. "
                           "Its browser profile has been preserved.", "browser_runtime_state_invalid") from None
        if not isinstance(value, dict):
            raise _failure("The persistent browser's runtime metadata is invalid.",
                           "browser_runtime_state_invalid")
        return value

    def update_state(self, **kwargs: Any) -> dict[str, Any]:
        if self._lease is None:
            raise _failure("Acquire the browser profile before updating runtime metadata.",
                           "browser_runtime_lease_required")
        state = self.read_state()
        state.update(kwargs)
        state.setdefault("profile", self.profile)
        state.setdefault("created_at", _utc_now())
        _atomic_json(self._state_path, state)
        return state

    def _configuration(self) -> tuple[str, dict[str, int]]:
        state = self.read_state()
        executable = self._executable or state.get("executable_path")
        if executable is None:
            from browser_use.browser.chrome import find_chrome_executable
            executable = find_chrome_executable()
        if not isinstance(executable, str) or not Path(executable).is_file():
            raise _failure("Chrome was not found. Install Chrome or provide --executable-path.",
                           "browser_executable_missing")
        executable = str(Path(executable).resolve())
        viewport = self._viewport or state.get("viewport") or {"width": 1280, "height": 900}
        if not isinstance(viewport, dict) or any(
            type(viewport.get(key)) is not int or not 1 <= viewport[key] <= 16384
            for key in ("width", "height")
        ):
            raise _failure("Browser viewport dimensions must be integers between 1 and 16384.",
                           "browser_runtime_configuration_invalid")
        return executable, {key: viewport[key] for key in ("width", "height")}

    def _matching_process(self, process: psutil.Process, executable: str) -> bool:
        arguments = process.cmdline()
        root = _flag(arguments, "--user-data-dir")
        return bool(root and _same_path(root, self.profile_path)
                    and _same_path(process.exe(), executable)
                    and not _flag(arguments, "--type"))

    def _profile_processes(self) -> list[psutil.Process]:
        """Find every main process using this directory, regardless of executable."""
        matches = []
        for process in psutil.process_iter():
            try:
                arguments = process.cmdline()
                root = _flag(arguments, "--user-data-dir")
                if root and _same_path(root, self.profile_path) and not _flag(arguments, "--type"):
                    matches.append(process)
            except (psutil.Error, OSError):
                continue
        return matches

    def _endpoint(self, executable: str) -> tuple[str, int] | None:
        try:
            lines = (self.profile_path / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
        if (len(lines) != 2 or not lines[0].isdigit() or not 0 < int(lines[0]) < 65536
                or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", lines[1])):
            # Interrupted Chrome startup can leave an incomplete endpoint file.
            return None
        port = int(lines[0])
        listeners = [connection for connection in psutil.net_connections(kind="tcp")
                     if connection.status == psutil.CONN_LISTEN and connection.laddr.port == port]
        if not listeners:
            return None
        if any(not ipaddress.ip_address(connection.laddr.ip).is_loopback for connection in listeners):
            raise _failure("The persistent browser endpoint is not restricted to loopback.",
                           "browser_runtime_endpoint_unverified")
        owners = {connection.pid for connection in listeners}
        if len(owners) != 1 or None in owners:
            raise _failure("The persistent browser endpoint's process cannot be verified.",
                           "browser_runtime_endpoint_unverified")
        pid = owners.pop()
        try:
            matches = self._matching_process(psutil.Process(pid), executable)
        except psutil.NoSuchProcess:
            return None
        if not matches:
            # A saved port can be reused after shutdown. Never connect to its new owner.
            return None
        host = "127.0.0.1" if any(c.laddr.ip == "127.0.0.1" for c in listeners) else "::1"
        connection = http.client.HTTPConnection(host, port, timeout=2)
        try:
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            if response.status != 200:
                return None
            details = json.loads(response.read(65536))
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            connection.close()
        endpoint = details.get("webSocketDebuggerUrl", "")
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parsed.port != port or parsed.path != lines[1] or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise _failure("Chrome's current endpoint does not match its managed profile metadata.",
                           "browser_runtime_endpoint_unverified")
        address = "127.0.0.1" if host == "127.0.0.1" else "[::1]"
        return f"ws://{address}:{port}{lines[1]}", pid

    def _launch(self, executable: str, viewport: dict[str, int]) -> subprocess.Popen:
        arguments = [executable, f"--user-data-dir={self.profile_path}", "--profile-directory=Default",
                     "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                     "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble",
                     "--restore-last-session", f"--window-size={viewport['width']},{viewport['height']}"]
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                   "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": str(_ROOT)}
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
            kernel.IsProcessInJob.restype = wintypes.BOOL
            in_job = wintypes.BOOL()
            if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(in_job)):
                raise ctypes.WinError(ctypes.get_last_error())
            options["creationflags"] = subprocess.DETACHED_PROCESS
            if in_job.value:
                options["creationflags"] |= subprocess.CREATE_BREAKAWAY_FROM_JOB
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            options["startupinfo"] = startup
        else:
            options["start_new_session"] = True
        try:
            return subprocess.Popen(arguments, **options)
        except PermissionError:
            raise _failure("Windows blocked launching Chrome independently of this command. "
                           "Run ctrlloop from your normal PowerShell session or its Windows login startup entry.",
                           "browser_runtime_detach_blocked") from None

    def ensure_started(self) -> str:
        if self._lease is None:
            raise _failure("Acquire the browser profile before starting its browser.",
                           "browser_runtime_lease_required")
        _check_directory(self.profile_path)
        self.profile_path.mkdir(parents=True, exist_ok=True)
        executable, viewport = self._configuration()
        existing = self._profile_processes()
        if len(existing) > 1:
            raise _failure("More than one browser process is using this persistent profile. "
                           "No additional browser will be launched.", "browser_runtime_profile_conflict")
        for process in existing:
            try:
                if not _same_path(process.exe(), executable):
                    raise _failure("This persistent profile is already open in a different browser executable. "
                                   "Use its current executable to reconnect.", "browser_runtime_profile_conflict")
            except psutil.NoSuchProcess:
                existing = []
        self.update_state(executable_path=executable, viewport=viewport)
        endpoint = self._endpoint(executable)
        reused = endpoint is not None
        launched = None
        if endpoint is None:
            if not existing:
                launched = self._launch(executable, viewport)
            deadline = time.monotonic() + 30
            while endpoint is None and time.monotonic() < deadline:
                endpoint = self._endpoint(executable)
                if endpoint is not None:
                    break
                if launched is not None and launched.poll() is not None:
                    raise _failure("The persistent Chrome process exited before its local connection was ready. "
                                   "Its profile has been preserved.", "browser_runtime_launch_failed")
                time.sleep(0.1)
            if endpoint is None:
                raise _failure("The persistent Chrome process did not expose its verified local connection "
                               "within 30 seconds. Its process and profile have been preserved.",
                               "browser_runtime_start_timeout")
        websocket_url, pid = endpoint
        self.metadata.update({"pid": pid, "reused_browser": reused, "executable_path": executable})
        self.update_state(pid=pid, last_connected_at=_utc_now())
        return websocket_url

    def status(self) -> dict[str, Any]:
        executable, _ = self._configuration()
        endpoint = self._endpoint(executable)
        return {**self.metadata, "running": endpoint is not None,
                "pid": endpoint[1] if endpoint else None,
                "initialized": bool(self.read_state().get("initialized"))}


def start_all() -> list[dict[str, Any]]:
    """Start registered profiles at user login without running tasks or importing logins."""
    _check_directory(_PROFILES)
    results = []
    for path in sorted(_PROFILES.glob("*/runtime.json")):
        runtime = None
        try:
            runtime = BrowserRuntime(profile=path.parent.name)
            runtime.acquire()
            runtime.ensure_started()
            results.append({**runtime.metadata, "status": "running"})
        except Exception as error:
            code = getattr(error, "code", "browser_runtime_start_failed")
            results.append({"automation_profile": path.parent.name,
                            "status": "busy" if code == "browser_runtime_busy" else "error",
                            "code": code, "error": str(error)})
        finally:
            if runtime is not None:
                runtime.release()
    return results


def _startup_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise _failure("The active Python environment does not contain pythonw.exe.",
                       "browser_runtime_startup_unavailable")
    command = subprocess.list2cmdline([str(pythonw.resolve()), str(Path(__file__).resolve()), "--start-all"])
    if len(command) > 260:
        raise _failure("The Windows login startup command exceeds the Run key's 260-character limit.",
                       "browser_runtime_startup_unavailable")
    return command


def startup_status() -> dict[str, Any]:
    if os.name != "nt":
        return {"installed": False, "supported": False}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, _RUN_NAME)
    except FileNotFoundError:
        return {"installed": False, "supported": True, "trigger": "user_logon"}
    return {"installed": command == _startup_command(), "supported": True,
            "trigger": "user_logon", "command": command}


def install_startup() -> dict[str, Any]:
    if os.name != "nt":
        raise _failure("Automatic browser startup is currently supported on Windows.",
                       "browser_runtime_startup_unavailable")
    import winreg
    command = _startup_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, command)
    return startup_status()


def uninstall_startup() -> dict[str, Any]:
    if os.name != "nt":
        raise _failure("Automatic browser startup is currently supported on Windows.",
                       "browser_runtime_startup_unavailable")
    import winreg
    with suppress(FileNotFoundError):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _RUN_NAME)
    return startup_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--start-all", action="store_true", help="Start every configured persistent browser")
    actions.add_argument("--status", action="store_true", help="Show browser and Windows login startup status")
    actions.add_argument("--install-startup", action="store_true", help="Start configured browsers at Windows login")
    actions.add_argument("--uninstall-startup", action="store_true", help="Remove only ctrlloop's login startup entry")
    args = parser.parse_args()
    try:
        if args.start_all:
            profiles = start_all()
            result = {"profiles": profiles}
            # pythonw has no console, so keep a credential-free startup result.
            _atomic_json(_PROFILES / "startup-status.json", {"at": _utc_now(), **result})
        elif args.install_startup:
            result = {"startup": install_startup()}
        elif args.uninstall_startup:
            result = {"startup": uninstall_startup()}
        else:
            _check_directory(_PROFILES)
            profiles = []
            for path in sorted(_PROFILES.glob("*/runtime.json")):
                try:
                    profiles.append(BrowserRuntime(profile=path.parent.name).status())
                except Exception as error:
                    profiles.append({"automation_profile": path.parent.name, "status": "error",
                                     "code": getattr(error, "code", "browser_runtime_status_failed"),
                                     "error": str(error)})
            result = {"profiles": profiles, "startup": startup_status()}
        if sys.stdout is not None:
            print(json.dumps(result, indent=2))
        return int(any(profile.get("status") == "error" for profile in result.get("profiles", [])))
    except Exception as error:
        if sys.stdout is not None:
            print(json.dumps({"status": "error", "error": str(error),
                              "code": getattr(error, "code", "browser_runtime_failed")}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
