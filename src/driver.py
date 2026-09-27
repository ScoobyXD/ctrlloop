"""Native Cua Driver MCP over an existing Fleet claim; no parallel wire model.

Fleet handles authentication/routing. Closing ends this request's Driver session
and disconnects, without releasing the desktop or changing its expiry.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from cua_sandbox import Sandbox


PINNED_DRIVER_VERSION = "0.24.0"
_PROTOCOLS = {"2025-06-18", "2025-03-26"}
_PROJECT = Path(__file__).resolve().parents[1]
_RESPONSE_LIMIT = 20 * 1024 * 1024


class DriverError(Exception):
    """A driver transport, refusal, or contract error with a safe summary."""

    def __init__(self, message: str, *, code: str = "driver_error",
                 tool: str | None = None, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.code, self.tool, self.exit_code = code, tool, exit_code
        self.context: dict[str, Any] = {}

    def details(self) -> dict[str, Any]:
        details = {"code": self.code, **self.context}
        if self.tool is not None:
            details["tool"] = self.tool
        if self.exit_code is not None:
            details["exit_code"] = self.exit_code
        return details


def _reject_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON numeric constant")


def _json(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


class CuaDriver:
    """Serialize calls, retaining one fresh native snapshot for one action.

    The advisory lock excludes ctrlloop processes using this checkout and claim.
    It cannot exclude a noVNC user or controllers on other computers.
    Actions are never retried. Uncertain transport failures end this run.
    """

    def __init__(self, claim_file: Path, *, service: str = "mcp", timeout: float = 60.0) -> None:
        if not isinstance(service, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", service):
            raise ValueError("service must name one Fleet service")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.claim_file = Path(claim_file).expanduser().resolve()
        self.service, self.timeout = service, timeout
        self.session = f"ctrlloop-{uuid4().hex[:16]}"
        self.session_info: dict[str, Any] = {
            "provider": "fleet", "service": service, "session": self.session,
            "close_behavior": "disconnect_only", "claim_preserved": True,
            "local_lock_scope": "this_checkout_and_claim",
        }
        self.desktop_url: str | None = None
        self.server_info: dict[str, Any] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self._sandbox: Any = None
        self._headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._claim_lock: Any = None
        self._started = self._closed = self._broken = self._session_started = False
        self._snapshot: dict[str, Any] | None = None

    def _acquire_claim_lock(self, reference: dict[str, Any]) -> None:
        identity = json.dumps([reference["namespace"], reference["claim"]], separators=(",", ":"))
        directory = _PROJECT / ".local" / "fleet-locks"
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / f"{hashlib.sha256(identity.encode()).hexdigest()}.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise DriverError("Another local control loop is using this Fleet claim.", code="desktop_in_use") from None
        self._claim_lock = handle

    def _release_claim_lock(self) -> None:
        handle, self._claim_lock = self._claim_lock, None
        if handle is not None:
            # Closing releases the OS lock, including after a process crash.
            # Keep the stable lock file so another owner's lock is never unlinked.
            handle.close()

    async def start(self, url: str | None = None) -> None:
        if self._started or self._closed:
            raise DriverError("This Cua client cannot be started twice.", code="driver_lifecycle_error")
        if url is not None:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError("The starting URL must be an absolute HTTP or HTTPS URL without credentials.")
        self._started = True
        try:
            try:
                reference = _json(self.claim_file.read_text(encoding="utf-8"))
                if not isinstance(reference, dict) or any(
                    not isinstance(reference.get(key), str) or not reference[key]
                    for key in ("namespace", "pool", "claim")
                ):
                    raise ValueError
                self._acquire_claim_lock(reference)
            except (OSError, ValueError, KeyError):
                raise DriverError("The saved Fleet claim reference is missing or invalid.", code="claim_reference_invalid") from None
            self._sandbox = await asyncio.wait_for(Sandbox.from_dict(reference), self.timeout)
            self.session_info.update(namespace=reference["namespace"], pool=reference["pool"],
                                     claim=reference["claim"], sandbox=self._sandbox.name)
            base = (f"https://run.cua.ai/api/svc/{quote(reference['namespace'], safe='')}/"
                    f"{quote(self._sandbox.name, safe='')}")
            self.desktop_url = base + "-vnc/vnc.html?autoconnect=1"
            initialized = await self._rpc("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ctrlloop", "version": "0.0.0"},
            })
            if initialized.get("protocolVersion") not in _PROTOCOLS:
                raise DriverError("Driver advertised an unsupported MCP protocol.", code="driver_protocol_unsupported")
            self.server_info = initialized.get("serverInfo", {})
            if (not isinstance(self.server_info, dict) or self.server_info.get("name") != "cua-driver"
                    or self.server_info.get("version") != PINNED_DRIVER_VERSION):
                raise DriverError(f"This adapter requires Cua Driver {PINNED_DRIVER_VERSION}.", code="driver_version_unsupported")
            self._headers["mcp-protocol-version"] = initialized["protocolVersion"]
            await self._rpc("notifications/initialized", notification=True)
            listing = await self._rpc("tools/list", {})
            schemas = listing.get("tools")
            if not isinstance(schemas, list):
                raise DriverError("Driver returned an invalid tool catalog.", code="invalid_driver_response")
            for tool in schemas:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        or not isinstance(tool.get("inputSchema"), dict) or tool["name"] in self.tools):
                    raise DriverError("Driver returned an invalid tool schema.", code="invalid_driver_response")
                self.tools[tool["name"]] = tool
            required = {"list_windows", "get_window_state", "click", "type_text", "bring_to_front",
                        "get_desktop_state", "start_session", "end_session"}
            if not required.issubset(self.tools):
                raise DriverError("Driver lacks a required desktop tool.", code="driver_schema_unsupported")
            # Driver lifecycle labels and MCP transport sessions are separate.
            # Cleanup this known label even if its creation response is lost.
            self._session_started = True
            await self.call("start_session", {"session": self.session})
            self.session_info["driver"] = dict(self.server_info)
            if url is not None:
                await self.call("launch_app", {"urls": [url]})
        except BaseException as error:
            try:
                await self.close()
            except Exception:
                pass
            if isinstance(error, (DriverError, asyncio.CancelledError, KeyboardInterrupt)):
                raise
            raise DriverError("Could not connect to the saved Fleet desktop; check its expiry and credentials.",
                              code="fleet_connection_failed") from None

    async def _rpc(self, method: str, params: dict[str, Any] | None = None,
                   *, notification: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        self._sequence += 1
        request_id = self._sequence
        if not notification:
            body["id"] = request_id
        try:
            response = await asyncio.wait_for(self._sandbox.services.request(
                self.service, method="POST", path="/mcp", headers=self._headers, json=body,
            ), self.timeout)
            if not 200 <= response.status_code < 300:
                code = "fleet_authorization_denied" if response.status_code in (401, 403) else "driver_transport_failed"
                raise DriverError("The Fleet Driver request failed; completion is unknown and was not retried.", code=code)
            if session := response.headers.get("mcp-session-id"):
                if previous := self._headers.get("mcp-session-id"):
                    if session != previous:
                        raise DriverError("Driver changed its MCP session unexpectedly.", code="invalid_driver_response")
                self._headers["mcp-session-id"] = session
            if notification:
                return {}
            if len(response.content) > _RESPONSE_LIMIT:
                raise DriverError("Driver response exceeds the allowed size.", code="invalid_driver_response")
            if "text/event-stream" in response.headers.get("content-type", ""):
                messages = []
                for event in response.text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
                    data = "\n".join(line[5:].removeprefix(" ") for line in event.splitlines() if line.startswith("data:"))
                    if data:
                        messages.append(_json(data))
            else:
                messages = [_json(response.text)]
            matched = [item for item in messages if isinstance(item, dict)
                       and type(item.get("id")) is int and item["id"] == request_id]
            if len(matched) != 1:
                raise DriverError("Driver returned no unique matching MCP response.", code="invalid_driver_response")
            message = matched[0]
            if message.get("jsonrpc") != "2.0" or ("result" in message) == ("error" in message):
                raise DriverError("Driver returned an invalid MCP response.", code="invalid_driver_response")
            if "error" in message:
                raise DriverError("Driver rejected the MCP request; it was not retried.", code="driver_call_failed")
            if not isinstance(message["result"], dict):
                raise DriverError("Driver returned a non-object MCP result.", code="invalid_driver_response")
            return message["result"]
        except BaseException as error:
            self._broken = True
            self._snapshot = None
            if isinstance(error, (DriverError, asyncio.CancelledError, KeyboardInterrupt)):
                raise
            code = "driver_timeout" if isinstance(error, TimeoutError) else "driver_transport_failed"
            raise DriverError("The Driver connection failed; completion is unknown and was not retried.", code=code) from None

    def _arguments(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        schema = self.tools.get(tool, {}).get("inputSchema")
        if not isinstance(schema, dict):
            raise DriverError("This tool was not advertised by the Driver.", code="driver_schema_unsupported", tool=tool)
        args = dict(arguments)
        properties = schema.get("properties", {})
        if "session" in properties:
            if "session" in args and args["session"] != self.session:
                raise DriverError("This client only uses its own Driver session.", code="invalid_driver_arguments", tool=tool)
            args["session"] = self.session
        if any(key not in args for key in schema.get("required", [])) or (
            schema.get("additionalProperties") is False and set(args) - set(properties)
        ):
            raise DriverError("Arguments do not match the Driver's advertised fields.", code="invalid_driver_arguments", tool=tool)
        # Validate the primitive constraints used by the loop using native
        # schemas. Rich unions remain authoritative on the Driver server.
        for key, value in args.items():
            spec = properties.get(key, {})
            kind = spec.get("type")
            valid_type = {"string": isinstance(value, str), "integer": type(value) is int,
                          "number": type(value) in (int, float) and math.isfinite(value),
                          "boolean": type(value) is bool, "array": isinstance(value, list),
                          "object": isinstance(value, dict), "null": value is None}
            if isinstance(kind, str) and not valid_type.get(kind, True):
                raise DriverError("A Driver argument has an invalid type.", code="invalid_driver_arguments", tool=tool)
            if "enum" in spec and value not in spec["enum"]:
                raise DriverError("A Driver argument has an unsupported value.", code="invalid_driver_arguments", tool=tool)
            if type(value) in (int, float) and (
                ("minimum" in spec and value < spec["minimum"]) or ("maximum" in spec and value > spec["maximum"])
            ):
                raise DriverError("A Driver argument is outside its supported range.", code="invalid_driver_arguments", tool=tool)
        return args

    def _validate_target(self, tool: str, args: dict[str, Any]) -> None:
        snapshot = self._snapshot
        if not snapshot or any(args.get(key) != snapshot.get(key) for key in ("pid", "window_id")):
            raise DriverError("Observe and select a fresh window control before acting.", code="stale_element_token", tool=tool)
        elements = snapshot.get("elements") or []
        if "element_token" in args:
            matching = [row for row in elements if row.get("element_token") == args["element_token"]]
            valid = isinstance(args["element_token"], str) and bool(args["element_token"]) and bool(matching)
            if valid and "element_index" in args:
                valid = matching[0].get("element_index") == args["element_index"]
            if "snapshot_id" in args:
                valid = valid and args["snapshot_id"] == snapshot.get("snapshot_id")
        else:
            valid = (isinstance(args.get("snapshot_id"), str) and bool(args["snapshot_id"])
                     and args["snapshot_id"] == snapshot.get("snapshot_id")
                     and type(args.get("element_index")) is int
                     and any(row.get("element_index") == args["element_index"] for row in elements))
        if not valid or any(key in args for key in ("x", "y", "target", "scope")):
            raise DriverError("The action must use a control from the latest window snapshot.", code="stale_element_token", tool=tool)

    @staticmethod
    def _validate_snapshot(payload: dict[str, Any], args: dict[str, Any]) -> None:
        """Validate native identities used by candidate selection, retaining all fields."""
        def invalid() -> None:
            raise DriverError("Driver returned an invalid native window observation.",
                              code="invalid_driver_response", tool="get_window_state")

        if any(type(payload.get(key)) is not int or payload[key] != args.get(key)
               for key in ("pid", "window_id")):
            invalid()
        snapshot_id = payload.get("snapshot_id")
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or not snapshot_id):
            invalid()
        elements = payload.get("elements")
        if elements is None:
            return
        if not isinstance(elements, list):
            invalid()
        indices: set[int] = set()
        for row in elements:
            if (not isinstance(row, dict) or type(row.get("element_index")) is not int
                    or row["element_index"] < 0 or row["element_index"] in indices
                    or not isinstance(row.get("role"), str)):
                invalid()
            indices.add(row["element_index"])
            parent = row.get("parent_index")
            if parent is not None and (type(parent) is not int or parent < 0):
                invalid()
            token = row.get("element_token")
            if token is not None and (not isinstance(token, str) or not token):
                invalid()
        returned_count = payload.get("returned_element_count")
        if returned_count is not None and (type(returned_count) is not int or returned_count != len(elements)):
            invalid()

    @staticmethod
    def _payload(tool: str, result: dict[str, Any]) -> dict[str, Any]:
        content = result.get("content", [])
        if not isinstance(content, list) or type(result.get("isError", False)) is not bool:
            raise DriverError("Driver returned an invalid tool envelope.", code="invalid_driver_response", tool=tool)
        payload = result.get("structuredContent")
        if payload is None:
            text = "\n".join(block["text"] for block in content if isinstance(block, dict)
                             and block.get("type") == "text" and isinstance(block.get("text"), str))
            try:
                payload = _json(text)
            except (ValueError, TypeError):
                payload = None
        refused = isinstance(payload, dict) and (
            any(key in payload for key in ("refusal", "error", "code"))
            or payload.get("effect") == "refused"
            or ("status" in payload and payload["status"] != "success")
        )
        if result.get("isError") or refused:
            code = "driver_call_failed"
            if isinstance(payload, dict):
                refusal = payload.get("refusal")
                candidates = [payload.get("code"), refusal.get("code") if isinstance(refusal, dict) else refusal]
                for known in ("stale_element_token", "snapshot_id_required", "background_unavailable", "session_ended", "permission_denied"):
                    if known in candidates:
                        code = known
                        break
            error = DriverError("Cua Driver refused or failed the call; it was not retried.", code=code, tool=tool)
            if isinstance(payload, dict):
                error.context["native_result"] = payload
            raise error
        if not isinstance(payload, dict):
            raise DriverError("Driver returned no native structured object.", code="invalid_driver_response", tool=tool)
        return payload

    async def _tool_result(self, tool: str, arguments: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._closed or not self._sandbox or self._broken:
            raise DriverError("This Driver connection is unavailable; start a new run and observe again.", code="driver_closed", tool=tool)
        args = self._arguments(tool, arguments)
        if tool in {"click", "type_text"}:
            self._validate_target(tool, args)
        if tool == "get_window_state" or self.tools[tool].get("annotations", {}).get("readOnlyHint") is not True:
            # Invalidate BEFORE dispatch: interrupted input may already land.
            self._snapshot = None
        try:
            result = await self._rpc("tools/call", {"name": tool, "arguments": args})
            payload = self._payload(tool, result)
            if tool == "get_window_state":
                self._validate_snapshot(payload, args)
            if tool in {"click", "type_text"}:
                # These are native ActionResult fields. A successful transport
                # without these facts cannot justify another control decision.
                effect, route = payload.get("effect"), payload.get("route")
                if (not isinstance(effect, str) or effect not in {"confirmed", "partial", "unverifiable", "suspected_noop"}
                        or not isinstance(route, str) or route not in {"accessibility", "synthetic_events", "global_input",
                                                                    "system_api", "dom", "trusted_input"}):
                    error = DriverError("Driver returned no recognized action facts; delivery is unknown.",
                                        code="invalid_driver_response", tool=tool)
                    error.context["native_result"] = payload
                    raise error
        except DriverError as error:
            error.tool = tool
            if error.code == "invalid_driver_response":
                self._broken = True
                self._snapshot = None
            raise
        if tool == "get_window_state":
            self._snapshot = deepcopy(payload)
        return payload, result

    async def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return native structuredContent, with no action replay or coercion."""
        if arguments is not None and not isinstance(arguments, Mapping):
            raise DriverError("Driver arguments must be a mapping.", code="invalid_driver_arguments", tool=tool)
        async with self._lock:
            payload, _ = await self._tool_result(tool, arguments or {})
            return payload

    async def screenshot(self) -> bytes:
        """Capture the desktop without reindexing any window's AT-SPI tree."""
        async with self._lock:
            _, result = await self._tool_result("get_desktop_state", {})
            images = [block for block in result.get("content", []) if isinstance(block, dict)
                      and block.get("type") == "image" and block.get("mimeType") == "image/png"]
            try:
                if len(images) != 1:
                    raise ValueError
                png = base64.b64decode(images[0]["data"], validate=True)
                if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError
                return png
            except (ValueError, KeyError, TypeError):
                raise DriverError("Driver returned no usable desktop PNG.", code="screenshot_unavailable") from None

    async def close(self) -> None:
        """End this run's Driver session, disconnect, and preserve the claim."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._snapshot = None
            failure: Exception | None = None
            try:
                if self._sandbox and self._session_started:
                    try:
                        result = await asyncio.wait_for(self._rpc("tools/call", {
                            "name": "end_session", "arguments": {"session": self.session},
                        }), 5.0)
                        self._payload("end_session", result)
                    except Exception as error:
                        failure = error
                if self._sandbox and "mcp-session-id" in self._headers:
                    try:
                        response = await asyncio.wait_for(self._sandbox.services.request(
                            self.service, method="DELETE", path="/mcp", headers=self._headers,
                        ), 5.0)
                        if response.status_code not in (200, 202, 204, 404, 405):
                            raise DriverError("MCP cleanup was not confirmed.", code="driver_cleanup_failed")
                    except Exception as error:
                        failure = failure or error
            finally:
                try:
                    if self._sandbox:
                        await asyncio.wait_for(self._sandbox.disconnect(), 5.0)
                except Exception as error:
                    failure = failure or error
                finally:
                    self._sandbox = None
                    self._release_claim_lock()
            if failure is not None:
                raise DriverError("Driver cleanup was not confirmed; the Fleet claim remains reserved.",
                                  code="driver_cleanup_failed") from None
