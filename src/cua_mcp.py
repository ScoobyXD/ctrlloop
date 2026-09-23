"""Run-scoped MCP stdio client for the existing Windows CUA daemon.

Only the small MCP client process belongs to this transport. The daemon is
never started or stopped here. Calls are serialized, and a failed call is
never replayed: the caller decides whether another observation is safe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

if __package__:
    from .driver import DriverError
else:  # python src/main.py
    from driver import DriverError


_PROTOCOL_VERSION = "2025-06-18"
# These revisions share the initialize/stdio/tools subset used here. The pinned
# CUA 0.28.2 installation advertises 2025-06-18 in its initialization response.
_SUPPORTED_PROTOCOLS = {_PROTOCOL_VERSION, "2025-03-26", "2024-11-05"}
_CLOSE_TIMEOUT = 1.0
# Windows CUA 0.28.2 otherwise waits for a distance/FPS-dependent cursor glide
# before clicking. A positive duration bounds that animation wait; zero selects
# the driver's slower speed-based mode rather than disabling animation.
_CURSOR_GLIDE_MS = 1


def _reject_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON numeric constant")


def _notification(response: Any) -> bool:
    return (
        isinstance(response, dict) and response.get("jsonrpc") == "2.0"
        and "id" not in response and isinstance(response.get("method"), str)
        and "result" not in response and "error" not in response
    )


def _receive(stdout: Any, responses: queue.Queue) -> None:
    """Block on stdout; this is response delivery, not health polling."""
    try:
        for line in stdout:
            try:
                response = json.loads(line, parse_constant=_reject_constant)
            except (ValueError, TypeError):
                responses.put(DriverError(
                    "CUA returned malformed MCP JSON.", code="invalid_driver_response"
                ))
                return
            responses.put(response)
    except UnicodeError:
        responses.put(DriverError(
            "CUA returned invalid MCP text.", code="invalid_driver_response"
        ))
    except (OSError, ValueError):
        responses.put(DriverError(
            "The CUA MCP connection could not be read.", code="transport_disconnected"
        ))
    finally:
        responses.put(DriverError(
            "The CUA MCP connection closed.", code="transport_disconnected"
        ))


class McpTransport:
    """Lazily open one MCP connection and retain it until close or failure."""

    def __init__(
        self, executable: str, timeout: float, *, process_factory: Callable | None = None,
        socket: str = r"\\.\pipe\cua-driver",
    ) -> None:
        if not isinstance(socket, str) or not socket.strip():
            raise ValueError("socket must be a nonempty string")
        self.executable = executable
        self.timeout = timeout
        self.socket = socket
        self._process_factory = process_factory or subprocess.Popen
        self._process: Any = None
        self._responses: queue.Queue | None = None
        self._reader: threading.Thread | None = None
        self._sequence = 0
        self._lock = threading.Lock()
        self._accessibility_click_supported = False

    def call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Return the native CallToolResult without flattening its error envelope."""
        with self._lock:
            deadline = time.monotonic() + self.timeout
            try:
                if self._process is None:
                    self._connect(deadline)
                if tool == "click" and not self._accessibility_click_supported:
                    self._require_accessibility_click(deadline)
                result = self._request(
                    "tools/call", {"name": tool, "arguments": dict(arguments)}, deadline
                )
                if not isinstance(result, dict):
                    raise DriverError(
                        "CUA returned a non-object MCP tool result.",
                        code="invalid_driver_response",
                    )
                return result
            except DriverError as error:
                self._discard()
                error.tool = tool
                raise
            except (OSError, UnicodeError, ValueError):
                self._discard()
                raise DriverError(
                    "The CUA MCP connection failed while sending the request.",
                    code="transport_disconnected", tool=tool,
                ) from None
            except BaseException:
                # Interrupts must also release the owned client/session.
                self._discard()
                raise

    def _connect(self, deadline: float) -> None:
        try:
            self._process = self._process_factory(
                [self.executable, "mcp", "--socket", self.socket],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1, shell=False,
                env={**os.environ, "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"},
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
        except FileNotFoundError:
            raise DriverError("CUA Driver executable was not found; supply its installed path.") from None
        except OSError:
            raise DriverError(
                "CUA Driver could not run; check executable and desktop permissions.",
                code="transport_disconnected",
            ) from None
        self._responses = queue.Queue()
        self._reader = threading.Thread(
            target=_receive, args=(self._process.stdout, self._responses),
            name="cua-mcp-responses", daemon=True,
        )
        self._reader.start()
        result = self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ctrlloop", "version": "0.0.0"},
        }, deadline)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("protocolVersion"), str)
            or result["protocolVersion"] not in _SUPPORTED_PROTOCOLS
            or not isinstance(result.get("capabilities"), dict)
            or not isinstance(result.get("serverInfo"), dict)
            or not isinstance(result["serverInfo"].get("name"), str)
            or not isinstance(result["serverInfo"].get("version"), str)
        ):
            raise DriverError(
                "CUA returned an unsupported MCP initialization response.",
                code="invalid_driver_response",
            )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # Configure once per connection, including reconnects, before any reads
        # or actions. Empty session selects this transport's implicit lifecycle
        # cursor in CUA 0.28.2, matching calls that omit session. This is not a
        # persistent/global setting and does not move or hide the cursor.
        result = self._request("tools/call", {
            "name": "set_agent_cursor_motion",
            "arguments": {"session": "", "glide_duration_ms": _CURSOR_GLIDE_MS},
        }, deadline)
        payload = result.get("structuredContent") if isinstance(result, dict) else None
        motion = payload.get("motion") if isinstance(payload, dict) else None
        if (
            not isinstance(result, dict) or result.get("isError", False) is not False
            or not isinstance(motion, dict)
            or motion.get("glide_duration_ms") != _CURSOR_GLIDE_MS
        ):
            raise DriverError(
                f"CUA did not confirm the session's {_CURSOR_GLIDE_MS} ms cursor glide configuration.",
                code="cursor_configuration_failed",
            )

    def _require_accessibility_click(self, deadline: float) -> None:
        """Check the connected daemon's native contract before any click.

        A matching executable version does not identify the daemon behind the
        pipe. Older Windows CUA releases can send mouse input for element tokens,
        so absence of the direct-only route contract must stop before dispatch.
        Reads, text entry and hotkeys do not depend on this click capability.
        """
        result = self._request("tools/list", {}, deadline)
        definitions = result.get("tools") if isinstance(result, dict) else None
        clicks = [item for item in definitions
                  if isinstance(item, dict) and item.get("name") == "click"] if isinstance(definitions, list) else []
        schema = clicks[0].get("inputSchema") if len(clicks) == 1 else None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        route = properties.get("route") if isinstance(properties, dict) else None
        if (
            not isinstance(route, dict) or route.get("type") != "string"
            or route.get("enum") != ["accessibility"]
            or route.get("default") != "accessibility"
        ):
            raise DriverError(
                "The connected CUA daemon does not advertise direct-only Windows accessibility clicks. Run the ctrlloop patched driver; no click was sent.",
                code="direct_accessibility_unsupported", tool="click",
            )
        self._accessibility_click_supported = True

    def _write(self, message: dict[str, Any]) -> None:
        self._process.stdin.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
        self._process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any], deadline: float) -> Any:
        # Consume already-delivered EOF/errors before sending an action. This
        # checks existing reader events only; it sends no health probe.
        while True:
            if time.monotonic() >= deadline:
                raise DriverError("CUA exceeded the client timeout.", code="client_timeout")
            try:
                pending = self._responses.get_nowait()
            except queue.Empty:
                break
            if isinstance(pending, DriverError):
                raise pending
            if not _notification(pending):
                raise DriverError("CUA sent an unsolicited MCP response.", code="invalid_driver_response")
        self._sequence += 1
        request_id = self._sequence
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DriverError("CUA exceeded the client timeout.", code="client_timeout")
            try:
                response = self._responses.get(timeout=remaining)
            except queue.Empty:
                raise DriverError("CUA exceeded the client timeout.", code="client_timeout") from None
            if isinstance(response, DriverError):
                raise response
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise DriverError("CUA returned an invalid MCP response.", code="invalid_driver_response")
            if "id" not in response:
                # Log/progress notifications may arrive between any replies.
                if _notification(response):
                    continue
                raise DriverError("CUA returned an invalid MCP notification.", code="invalid_driver_response")
            if (
                type(response["id"]) is not int or response["id"] != request_id
                or "method" in response or ("result" in response) == ("error" in response)
            ):
                raise DriverError("CUA returned an unmatched MCP response.", code="invalid_driver_response")
            if "error" in response:
                error = response["error"]
                if (
                    not isinstance(error, dict) or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)
                ):
                    raise DriverError("CUA returned an invalid MCP error.", code="invalid_driver_response")
                # Never copy a server diagnostic, arguments or UI text into logs.
                # The pinned proxy emits this specific internal error when its
                # per-tool named-pipe forwarding to the daemon loses transport.
                if error["code"] == -32603 and error["message"].startswith("daemon transport error forwarding"):
                    raise DriverError(
                        "The CUA MCP client lost its connection to the daemon.",
                        code="transport_disconnected",
                    )
                raise DriverError("CUA rejected the MCP request.", code="driver_call_failed")
            return response["result"]

    def close(self) -> None:
        """End this client's session; never send a daemon shutdown command."""
        with self._lock:
            self._discard()

    def _discard(self) -> None:
        process, reader = self._process, self._reader
        self._process = self._responses = self._reader = None
        self._accessibility_click_supported = False
        if process is None:
            return
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        # EOF normally lets the MCP proxy clean up its daemon session itself.
        # A stuck client is terminated by handle, never by executable name.
        try:
            process.wait(timeout=_CLOSE_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=_CLOSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=_CLOSE_TIMEOUT)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
        except OSError:
            pass
        if reader is not None and reader.ident is not None:
            reader.join(timeout=_CLOSE_TIMEOUT)
        # Closing TextIOWrapper while another thread is blocked reading it can
        # itself block indefinitely. A stubborn inherited pipe is left to the
        # daemon reader thread; telemetry is disabled to prevent this normally.
        if reader is None or not reader.is_alive():
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
