"""Persistent-client transport tests; no daemon or desktop access required."""

from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.cua_mcp import McpTransport
from src.driver import CuaDriver, DriverError


_EOF = object()
_SILENT = object()


def initialization(version="2025-06-18"):
    return {
        "protocolVersion": version, "capabilities": {"tools": {}},
        "serverInfo": {"name": "fake-cua", "version": "0.28.2"},
    }


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        line = self.lines.get()
        if line is _EOF:
            raise StopIteration
        return line

    def close(self):
        self.closed = True


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, text):
        if self.closed:
            raise BrokenPipeError("private diagnostic")
        message = json.loads(text)
        self.process.messages.append(message)
        method = message["method"]
        if method == "notifications/initialized":
            return len(text)
        if method == "initialize":
            response = self.process.initialization
        else:
            response = self.process.results.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response(message, self.process)
        elif response is _EOF:
            self.process.eof()
        elif response is _SILENT:
            pass
        elif isinstance(response, str):
            self.process.stdout.lines.put(response)
        else:
            self.process.send({"jsonrpc": "2.0", "id": message["id"], "result": response})
        return len(text)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        self.process.eof()


class FakeProcess:
    def __init__(self, *results, initialize=None, wait_failures=0):
        self.results = deque(results)
        self.initialization = initialization() if initialize is None else initialize
        self.messages = []
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.waits = []
        self.wait_failures = wait_failures
        self.terminated = self.killed = False
        self.returncode = None

    def send(self, response):
        self.stdout.lines.put(json.dumps(response) + "\n")

    def eof(self):
        self.returncode = 0
        self.stdout.lines.put(_EOF)

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.wait_failures:
            self.wait_failures -= 1
            raise subprocess.TimeoutExpired("owned fake client", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.eof()

    def kill(self):
        self.killed = True
        self.eof()


class Factory:
    def __init__(self, *processes):
        self.processes = deque(processes)
        self.calls = []

    def __call__(self, args, **options):
        self.calls.append((args, options))
        result = self.processes.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class McpTransportTests(unittest.TestCase):
    def transport(self, *processes, timeout=1):
        factory = Factory(*processes)
        transport = McpTransport("fake-cua", timeout, process_factory=factory)
        self.addCleanup(transport.close)
        return transport, factory

    def test_reuses_one_client_and_handshake_for_native_calls(self):
        first = {"content": [], "structuredContent": {"windows": []}}
        second = {"content": [], "structuredContent": {"effect": "unverifiable"}}
        process = FakeProcess(first, second)
        transport, factory = self.transport(process)
        self.assertEqual(factory.calls, [])  # lazy; unused runs start no client
        self.assertEqual(transport.call("list_windows", {}), first)
        args = {"pid": 12, "window_id": 45, "element_token": "observed-token"}
        self.assertEqual(transport.call("click", args), second)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual([m["method"] for m in process.messages], [
            "initialize", "notifications/initialized", "tools/call", "tools/call",
        ])
        self.assertEqual([m["id"] for m in process.messages if "id" in m], [1, 2, 3])
        self.assertEqual(process.messages[-1]["params"], {"name": "click", "arguments": args})
        transport.close()
        self.assertTrue(process.stdin.closed)
        self.assertFalse(process.terminated)
        self.assertEqual(len(process.messages), 4)  # no ping, list, stop or shutdown
        transport.close()
        self.assertEqual(len(process.waits), 1)

    def test_child_is_hidden_telemetry_disabled_and_attached_to_existing_daemon(self):
        process = FakeProcess({"content": []})
        transport, factory = self.transport(process)
        with patch.dict(os.environ, {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "1", "KEPT": "yes"}):
            transport.call("list_windows", {})
            self.assertEqual(os.environ["CUA_DRIVER_RS_TELEMETRY_ENABLED"], "1")
        args, options = factory.calls[0]
        self.assertEqual(args, ["fake-cua", "mcp", "--socket", r"\\.\pipe\cua-driver"])
        self.assertFalse(options["shell"])
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["env"]["CUA_DRIVER_RS_TELEMETRY_ENABLED"], "0")
        self.assertEqual(options["env"]["KEPT"], "yes")
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_native_tool_refusal_envelope_is_preserved_without_disconnect(self):
        refused = {
            "isError": True, "content": [{"type": "text", "text": "private refusal"}],
            "structuredContent": {"code": "approval_required"},
        }
        process = FakeProcess(refused, {"content": []})
        transport, factory = self.transport(process)
        self.assertEqual(transport.call("click", {}), refused)
        self.assertFalse(process.stdin.closed)
        transport.call("list_windows", {})
        self.assertEqual(len(factory.calls), 1)

    def test_progress_notifications_are_ignored(self):
        def reply(message, process):
            process.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            process.send({"jsonrpc": "2.0", "id": message["id"], "result": {"content": []}})
        process = FakeProcess(reply)
        transport, _ = self.transport(process)
        self.assertEqual(transport.call("list_windows", {}), {"content": []})

    def test_eof_discards_without_replay_and_next_call_gets_fresh_handshake(self):
        first = FakeProcess(_EOF)
        second = FakeProcess({"content": []})
        transport, factory = self.transport(first, second)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "transport_disconnected")
        self.assertEqual(caught.exception.tool, "click")
        self.assertTrue(first.stdin.closed)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(sum(m["method"] == "tools/call" for m in first.messages), 1)
        transport.call("list_windows", {})
        self.assertEqual(len(factory.calls), 2)
        self.assertEqual(second.messages[0]["method"], "initialize")

    def test_timeout_discards_client_without_replaying_action(self):
        process = FakeProcess(_SILENT)
        transport, factory = self.transport(process, timeout=0.03)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "client_timeout")
        self.assertTrue(process.stdin.closed)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(sum(m["method"] == "tools/call" for m in process.messages), 1)

    def test_eof_while_idle_is_reported_before_sending_an_action(self):
        process = FakeProcess({"content": []})
        transport, factory = self.transport(process)
        transport.call("list_windows", {})
        process.eof()
        transport._reader.join(timeout=1)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "transport_disconnected")
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(sum(m["method"] == "tools/call" for m in process.messages), 1)

    def test_initialization_timeout_never_sends_tool(self):
        process = FakeProcess(initialize=_SILENT)
        transport, _ = self.transport(process, timeout=0.03)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "client_timeout")
        self.assertEqual([m["method"] for m in process.messages], ["initialize"])

    def test_wrong_ids_and_malformed_frames_discard_client(self):
        frames = [
            {"jsonrpc": "2.0", "id": 999, "result": {}},
            {"jsonrpc": "2.0", "id": "2", "result": {}},
            {"jsonrpc": "2.0", "id": True, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "result": {}, "error": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "server-request", "result": {}},
            {"jsonrpc": "1.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "result": {}},
            {"jsonrpc": "2.0", "id": 2, "error": "private bad error"},
            [],
        ]
        for frame in frames:
            with self.subTest(frame=frame):
                process = FakeProcess(json.dumps(frame) + "\n")
                transport, _ = self.transport(process)
                with self.assertRaises(DriverError) as caught:
                    transport.call("click", {})
                self.assertEqual(caught.exception.code, "invalid_driver_response")
                self.assertTrue(process.stdin.closed)

    def test_invalid_json_and_nonstandard_numbers_are_rejected(self):
        for text in ("not JSON\n", '{"jsonrpc":"2.0","id":2,"result":{"value":NaN}}\n'):
            with self.subTest(text=text):
                process = FakeProcess(text)
                transport, _ = self.transport(process)
                with self.assertRaises(DriverError) as caught:
                    transport.call("list_windows", {})
                self.assertEqual(caught.exception.code, "invalid_driver_response")

    def test_valid_rpc_error_is_safe_and_discards_connection(self):
        process = FakeProcess(json.dumps({
            "jsonrpc": "2.0", "id": 2,
            "error": {"code": -32603, "message": "private diagnostic"},
        }) + "\n")
        transport, _ = self.transport(process)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "driver_call_failed")
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(process.stdin.closed)

    def test_non_object_tool_result_is_rejected(self):
        transport, _ = self.transport(FakeProcess([]))
        with self.assertRaises(DriverError) as caught:
            transport.call("list_windows", {})
        self.assertEqual(caught.exception.code, "invalid_driver_response")

    def test_proxy_daemon_transport_failure_is_classified_without_exposing_diagnostic(self):
        for code, message, expected in (
            (-32603, "daemon transport error forwarding list_windows: private pipe details", "transport_disconnected"),
            (-32603, "other failure: daemon transport error forwarding private", "driver_call_failed"),
            (-32602, "daemon transport error forwarding private", "driver_call_failed"),
        ):
            with self.subTest(code=code, message=message):
                process = FakeProcess(json.dumps({
                    "jsonrpc": "2.0", "id": 2, "error": {"code": code, "message": message},
                }) + "\n")
                transport, _ = self.transport(process)
                with self.assertRaises(DriverError) as caught:
                    transport.call("list_windows", {})
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("private", str(caught.exception))
                self.assertTrue(process.stdin.closed)

    def test_adapter_retries_daemon_forwarding_failure_only_for_reads(self):
        failure = json.dumps({
            "jsonrpc": "2.0", "id": 2,
            "error": {"code": -32603, "message": "daemon transport error forwarding: private"},
        }) + "\n"
        first = FakeProcess(failure)
        second = FakeProcess({"content": [], "structuredContent": {"windows": []}})
        transport, factory = self.transport(first, second)
        driver = CuaDriver(transport=transport)
        self.addCleanup(driver.close)
        self.assertEqual(driver.list_windows(), [])
        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(first.stdin.closed)
        self.assertEqual(sum(m["method"] == "tools/call" for m in first.messages), 1)
        self.assertEqual(sum(m["method"] == "tools/call" for m in second.messages), 1)

        failed_action = FakeProcess(failure)
        action_transport, action_factory = self.transport(failed_action)
        action_driver = CuaDriver(transport=action_transport)
        self.addCleanup(action_driver.close)
        with self.assertRaises(DriverError) as caught:
            action_driver.click({"pid": 12, "window_id": 34, "element_token": "observed-token"})
        self.assertEqual(caught.exception.code, "transport_disconnected")
        self.assertEqual(len(action_factory.calls), 1)
        self.assertEqual(sum(m["method"] == "tools/call" for m in failed_action.messages), 1)

    def test_protocol_negotiation_accepts_supported_version_and_rejects_unknown(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18"):
            with self.subTest(version=version):
                transport, _ = self.transport(FakeProcess({"content": []}, initialize=initialization(version)))
                self.assertEqual(transport.call("list_windows", {}), {"content": []})
        process = FakeProcess(initialize=initialization("unknown"))
        transport, _ = self.transport(process)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "invalid_driver_response")
        self.assertEqual([m["method"] for m in process.messages], ["initialize"])

    def test_broken_write_discards_client_and_hides_diagnostics(self):
        process = FakeProcess(BrokenPipeError("private diagnostic"))
        transport, _ = self.transport(process)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "transport_disconnected")
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(process.stdin.closed)

    def test_spawn_failure_does_not_leave_a_connection(self):
        process = FakeProcess({"content": []})
        transport, factory = self.transport(FileNotFoundError("private path"), process)
        with self.assertRaises(DriverError) as caught:
            transport.call("list_windows", {})
        self.assertNotIn("private", str(caught.exception))
        transport.call("list_windows", {})
        self.assertEqual(len(factory.calls), 2)

    def test_close_escalates_only_owned_client_and_does_not_mask_call_failure(self):
        process = FakeProcess(_EOF, wait_failures=3)
        transport, _ = self.transport(process)
        with self.assertRaises(DriverError) as caught:
            transport.call("click", {})
        self.assertEqual(caught.exception.code, "transport_disconnected")
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.waits, [1.0, 1.0, 1.0])

    def test_close_is_lazy_and_a_closed_transport_can_be_reconnected(self):
        first = FakeProcess({"content": []})
        second = FakeProcess({"content": []})
        transport, factory = self.transport(first, second)
        transport.close()
        self.assertEqual(factory.calls, [])
        transport.call("list_windows", {})
        transport.close()
        transport.call("list_windows", {})
        self.assertEqual(len(factory.calls), 2)

    def test_real_subprocess_reuses_streams_and_exits_on_stdin_eof(self):
        # A real pipe/process smoke test, but the server is Python and never
        # touches CUA, the desktop, or the named pipe supplied to the factory.
        server = '''
import json, os, sys
initialized = False
calls = 0
for line in sys.stdin:
    message = json.loads(line)
    if message["method"] == "notifications/initialized":
        initialized = True
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "test", "version": "1"}}
    else:
        assert initialized
        calls += 1
        result = {"content": [], "structuredContent": {"pid": os.getpid(), "calls": calls}}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''
        processes = []
        def factory(args, **options):
            process = subprocess.Popen([sys.executable, "-u", "-c", server], **options)
            processes.append(process)
            return process
        transport = McpTransport("fake-cua", 5, process_factory=factory)
        self.addCleanup(transport.close)
        first = transport.call("list_windows", {})["structuredContent"]
        second = transport.call("get_window_state", {})["structuredContent"]
        self.assertEqual(len(processes), 1)
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual((first["calls"], second["calls"]), (1, 2))
        transport.close()
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(processes[0].stdin.closed)
        self.assertTrue(processes[0].stdout.closed)

    def test_direct_script_import_supports_lazy_driver_construction(self):
        result = subprocess.run(
            [sys.executable, "-c", (
                "from driver import CuaDriver; "
                "from cua_mcp import McpTransport; "
                "driver = CuaDriver(executable='not-an-installed-program'); "
                "assert isinstance(driver._transport, McpTransport); "
                "driver.close()"
            )],
            cwd=Path(__file__).resolve().parents[1] / "src",
            capture_output=True, text=True, timeout=5, shell=False,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
