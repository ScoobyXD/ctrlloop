"""Durable run evidence and monotonic timing, using the existing event format."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import logging
import math
import os
from pathlib import Path
from threading import RLock
from time import perf_counter_ns, time_ns
from types import TracebackType
from typing import Any, Self
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

import httpx2


_REQUEST_ID_EXTENSION = "ctrlloop_report_request_id"


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON number")
    return number


def _reject_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON constant")


def _body_fields(content: bytes) -> dict[str, Any]:
    try:
        return {"body": json.loads(content, parse_float=_finite_float, parse_constant=_reject_constant)}
    except (ValueError, UnicodeError):
        return {"body_text": content.decode("utf-8", errors="replace")}


def _format_payload_json(body: Any) -> str:
    """Indent inputs and outputs, with space between top-level sections."""
    if isinstance(body, dict):
        ordered = {key: body[key] for key in ("state", "questions") if key in body}
        ordered.update(body)
        body = ordered
    lines = json.dumps(body, ensure_ascii=False, allow_nan=False, indent=2).splitlines()
    return "\n".join(
        "\n" + line if index > 1 and line.startswith('  "') else line
        for index, line in enumerate(lines)
    ) + "\n"


class RunReport:
    """Journal each observation, then render the existing report at close.

    Reports intentionally include request and observed UI content. HTTP headers,
    URL credentials/query strings, and exception messages are never recorded by
    these hooks. The caller supplies run-level progress and terminal events.
    """

    def __init__(
        self, directory: Path, *, origin_ns: int | None = None,
        origin_wall_ns: int | None = None, origin_label: str = "report_construction",
        run_id: str | None = None,
    ) -> None:
        self._origin_ns = perf_counter_ns() if origin_ns is None else origin_ns
        self._origin_wall_ns = time_ns() if origin_wall_ns is None else origin_wall_ns
        self.origin_label = origin_label
        self.run_id = run_id
        self._lock = RLock()
        self._span_count = 0
        # Workers inherit the round context, then replace their own span stack.
        self._contexts: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
            f"report_context_{id(self)}", default=(),
        )
        self._pending_requests: dict[int, dict[str, Any]] = {}
        self._first_request_ns: int | None = None
        self._write_ns = 0
        self._write_count = 0
        self._request_write_ns = 0
        self._request_file_count = 0
        self._response_write_ns = 0
        self._response_file_count = 0
        self._finished_ns: int | None = None
        self._completion_timing: dict[str, Any] = {}
        directory = directory.resolve()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.requests_path = directory / f"run-{stamp}-{uuid4().hex}"
        self.path = self.requests_path / "total_run.json"
        self.requests_path.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.journal_path = self.requests_path / "events.jsonl"
        self._journal = os.fdopen(os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
        self._journal_write_ns = 0
        self._loops: dict[int, dict[str, Any]] = {}
        self._native_dispatches: dict[int, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._closed = False
        self._close_error: BaseException | None = None
        self._request_count = 0
        self.append("report_created", run_id=run_id, origin=origin_label,
                    loop_boundary="current_state_start_to_last_input_command_dispatch",
                    clock_scope="worker_monotonic; server timestamps are a separate clock")

    def _write(self, text: str) -> None:
        started_ns = perf_counter_ns()
        try:
            self._write_document(text)
        finally:
            self._write_ns += perf_counter_ns() - started_ns
            self._write_count += 1

    async def capture_screenshot(self, driver, *, phase: str, iteration: int | None = None) -> str | None:
        """Save a desktop image without refreshing the selected window's tokens.

        Images are local report artifacts, never Jev inputs. Capture failures are
        diagnostic and must not hide an action result or cause an action replay.
        """
        if phase not in {"before", "after", "navigation_error"}:
            raise ValueError("Unknown screenshot phase")
        name = f"loop-{iteration:04d}-{phase}.png" if iteration is not None else f"{phase}.png"
        destination = self.requests_path / "screenshots" / name
        try:
            with self.span("screenshot_capture", phase=phase, iteration=iteration):
                # Driver owns the bounded network deadline. A shorter wrapper
                # cancellation would invalidate its otherwise healthy session.
                content = await driver.screenshot()
            with self.span("screenshot_write", phase=phase, iteration=iteration):
                def write_image():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._write_document(content, path=destination)

                await asyncio.to_thread(write_image)
            self.append("screenshot_saved", phase=phase, iteration=iteration, path=str(destination))
            return str(destination)
        except Exception as error:
            self.append("screenshot_error", phase=phase, iteration=iteration, error_type=type(error).__name__)
            logging.getLogger(__name__).warning("Could not save %s screenshot; see the run report.", phase)
            return None

    def _write_document(self, content: str | bytes, *, path: Path | None = None) -> None:
        destination = self.path if path is None else path
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        # Unlike tempfile.mkstemp on Windows, permission failures fail promptly.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                output = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with output:
                output.write(content.encode("utf-8") if isinstance(content, str) else content)
                output.flush()
                os.fsync(output.fileno())
            # Close before replacing so Windows can swap the complete document.
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Cleanup must not hide the original write failure.

    def _elapsed_ms(self, at_ns: int) -> float:
        return (at_ns - self._origin_ns) / 1_000_000

    def _timestamp(self, at_ns: int) -> str:
        # A single UTC anchor keeps timestamps aligned with monotonic intervals
        # even if the system wall clock is adjusted during a run.
        seconds = (self._origin_wall_ns + at_ns - self._origin_ns) / 1_000_000_000
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _context(self) -> dict[str, Any]:
        contexts = self._contexts.get()
        if not contexts:
            return {}
        return {key: value for key, value in contexts[-1].items() if key != "_started_ns"}

    def _summary(self, at_ns: int) -> dict[str, Any]:
        return {
            "origin": self.origin_label,
            "duration_ms": self._elapsed_ms(at_ns),
            "jev_api_call_count": self._request_count,
            "time_to_first_jev_request_ms": (
                self._elapsed_ms(self._first_request_ns) if self._first_request_ns is not None else None
            ),
            "end_boundary": "operational_end_before_report_generation",
            "durable_journal_write_duration_ms": self._journal_write_ns / 1_000_000,
        }

    def _serializable(self, saved: dict[str, Any]) -> dict[str, Any]:
        record = {"timestamp": self._timestamp(saved["_at_ns"]),
                  **{key: value for key, value in saved.items() if key != "_at_ns"}}
        if isinstance(record.get("body"), bytes):
            record.update(_body_fields(record.pop("body")))
        return record

    def snapshot_events(self, *, step_id: str) -> list[dict[str, Any]]:
        """Copy existing event records for boundary inspection before report close.

        Desktop screenshot payloads remain in the final archive. Their dispatch
        and return facts are still included here, without inline image bytes.
        """
        with self._lock:
            records = [self._serializable(deepcopy(item)) for item in self._events if item.get("step_id") == step_id]
        for record in records:
            if record.get("event") == "native_call_returned" and record.get("tool") == "get_desktop_state":
                record.pop("body", None)
                record.pop("body_text", None)
                record["capture_note"] = "Desktop image payload is available in the final evidence archive."
        return records

    def append(self, event: str, *, _at_ns: int | None = None, **data: Any) -> None:
        if self._closed:
            raise ValueError("Cannot append to a closed run report")
        at_ns = perf_counter_ns() if _at_ns is None else _at_ns
        record = {
            "_at_ns": at_ns,
            "elapsed_ms": self._elapsed_ms(at_ns),
            "event": event,
            **self._context(),
            **data,
        }
        if event in {"run_finished", "run_error", "run_interrupted"}:
            self._finished_ns = at_ns
            record["timing"] = self._summary(at_ns)
        # Copy only the new observation. Immutable HTTP bytes need no copy or parse.
        record = deepcopy(record)
        with self._lock:
            self._events.append(record)
            started_ns = perf_counter_ns()
            encoded = json.dumps(self._serializable(record), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._journal.write(encoded + b"\n")
            self._journal.flush()
            os.fsync(self._journal.fileno())
            self._journal_write_ns += perf_counter_ns() - started_ns

    def native_sent(self, call_id: int, method: str, params: dict[str, Any] | None) -> int:
        """Persist intent before transport; this is not proof the input landed."""
        tool = (params or {}).get("name") if method == "tools/call" else None
        details = {"call_id": call_id, "method": method, "tool": tool,
                   "arguments": (params or {}).get("arguments") if tool else params}
        self.append("native_call_intent", **details, boundary="journaled_before_transport",
                    delivery="not_yet_dispatched")
        iteration = self._context().get("iteration")
        # All synchronous journal work precedes this timestamp. No further file
        # writes occur before the caller invokes native transport. A crash can
        # retain intent without a dispatch marker, which recovery labels unknown.
        at_ns = perf_counter_ns()
        self._native_dispatches[call_id] = {**details, "_at_ns": at_ns}
        if iteration in self._loops and tool in {"click", "type_text", "press_key", "hotkey"}:
            self._loops[iteration].update(last_command_ns=at_ns, last_command_id=call_id, last_command_tool=tool)
        return at_ns

    def native_dispatch_record(self, call_id: int) -> None:
        dispatch = self._native_dispatches.pop(call_id, None)
        if dispatch:
            self.append("native_call_sent", **dispatch, boundary="transport_call_start",
                        recorded_after_transport=True, delivery="not_proof_of_input_delivery")

    def finish_loop(self, iteration: int) -> None:
        loop = self._loops.pop(iteration, None)
        if loop is None:
            return
        ended = loop.get("last_command_ns", perf_counter_ns())
        self.append("loop_finished", _at_ns=ended, iteration=iteration,
                    started_elapsed_ms=self._elapsed_ms(loop["started_ns"]),
                    finished_elapsed_ms=self._elapsed_ms(ended),
                    duration_ms=(ended-loop["started_ns"])/1_000_000,
                    last_command_id=loop.get("last_command_id"),
                    last_command_tool=loop.get("last_command_tool"),
                    end_boundary="transport_call_start" if "last_command_ns" in loop else "stopped_without_input_command",
                    completed_action_loop="last_command_ns" in loop)

    def _new_span(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._span_count += 1
            span_id = self._span_count
        context = self._context()
        return {
            **context, **data, "operation": operation,
            "parent_span_id": context.get("span_id"), "span_id": span_id,
        }

    def _finish_span(
        self, span: dict[str, Any], started_ns: int, finished_ns: int, status: str,
        error: BaseException | None = None,
    ) -> None:
        span.update(
            started_elapsed_ms=self._elapsed_ms(started_ns),
            finished_elapsed_ms=self._elapsed_ms(finished_ns),
            duration_ms=(finished_ns - started_ns) / 1_000_000, status=status,
        )
        if error is not None:
            span["error_type"] = type(error).__name__
        self.append("timing_finished", _at_ns=finished_ns, **span)

    @contextmanager
    def span(self, operation: str, **data: Any):
        """Record durable operation boundaries using a monotonic clock."""
        span = self._new_span(operation, data)
        self.append("timing_started", **span)
        started_ns = perf_counter_ns()
        if operation == "loop.cycle":
            self._loops[data["iteration"]] = {"started_ns": started_ns}
            self.append("loop_started", _at_ns=started_ns, iteration=data["iteration"],
                        boundary="before_current_state_acquisition")
        token = self._contexts.set((*self._contexts.get(), {**span, "_started_ns": started_ns}))
        failure = None
        try:
            yield span
        except BaseException as error:
            failure = error
            raise
        finally:
            finished_ns = perf_counter_ns()
            status = "ok" if failure is None else (
                "interrupted" if isinstance(failure, (KeyboardInterrupt, SystemExit)) else "error"
            )
            try:
                if operation == "loop.cycle":
                    self.finish_loop(data["iteration"])
                if operation == "selection.sdk_call":
                    self._finish_pending_requests(span["span_id"], finished_ns, failure)
                self._finish_span(span, started_ns, finished_ns, status, failure)
            finally:
                self._contexts.reset(token)

    def record_interval(
        self, operation: str, started_ns: int, finished_ns: int, **data: Any,
    ) -> None:
        """Retain boundaries for work measured before the reporter was ready."""
        span = self._new_span(operation, {**data, "recorded_after_completion": True})
        self.append("timing_started", _at_ns=started_ns, **span)
        self._finish_span(span, started_ns, finished_ns, "ok")

    def on_request(self, request: httpx2.Request) -> None:
        """Capture the prepared request, then start timing before transport."""
        # Keep capture and timing initialization atomic with other appends.
        # This lock is released before transport or any response-body read.
        with self._lock:
            self._request_count += 1
            request_id = self._request_count
            request.extensions[_REQUEST_ID_EXTENSION] = request_id
            content = request.content
            self.append(
                "jev_request", request_id=request_id, method=request.method,
                url=str(request.url.copy_with(userinfo=b"", query=None, fragment=None)),
                body_bytes=len(content), body=content,
                start_boundary="prepared_request_before_transport",
            )
            record = self._events[-1]
            pending = {"context": self._context()}
            self._pending_requests[request_id] = pending
            # All payload capture precedes this boundary. Only lightweight timing
            # assignments remain before httpx hands the encoded request to transport.
            started_ns = perf_counter_ns()
            pending["started_ns"] = started_ns
            record["_at_ns"] = started_ns
            record["elapsed_ms"] = self._elapsed_ms(started_ns)
            if pending["context"].get("operation") == "selection.sdk_call":
                sdk_start = self._contexts.get()[-1]["_started_ns"]
                record["request_preparation_duration_ms"] = (started_ns - sdk_start) / 1_000_000
            record["started_elapsed_ms"] = record["elapsed_ms"]
            if self._first_request_ns is None:
                self._first_request_ns = started_ns

    def on_response(self, response: httpx2.Response) -> None:
        """Retain the complete body before the SDK validates or rejects it."""
        request_id = response.request.extensions.get(_REQUEST_ID_EXTENSION)
        headers_ns = perf_counter_ns()
        try:
            content = response.read()
        except BaseException as error:
            finished_ns = perf_counter_ns()
            timing = self._request_timing(request_id, finished_ns, headers_ns)
            try:
                self.append(
                    "jev_response_error", _at_ns=finished_ns, request_id=request_id, **timing,
                    status_code=response.status_code, error_type=type(error).__name__,
                )
            except BaseException:
                # A secondary report failure must not replace a read failure or cancellation.
                pass
            raise
        finished_ns = perf_counter_ns()
        timing = self._request_timing(request_id, finished_ns, headers_ns)
        parsed = _body_fields(content)
        usage = parsed.get("body", {}).get("usage", {}) if isinstance(parsed.get("body"), dict) else {}
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        self.append(
            "jev_response", _at_ns=finished_ns, request_id=request_id, **timing,
            status_code=response.status_code,
            body=content,
            input_tokens=input_tokens,
            input_cost_usd=input_tokens * 0.042 / 1_000_000 if type(input_tokens) is int else None,
            input_price_per_million_usd=0.042, output_cost_usd=0,
        )

    def _request_timing(self, request_id: int | None, finished_ns: int, headers_ns: int) -> dict[str, Any]:
        with self._lock:
            pending = self._pending_requests.pop(request_id, None)
        timing = {
            "response_headers_elapsed_ms": self._elapsed_ms(headers_ns),
            "response_body_read_duration_ms": (finished_ns - headers_ns) / 1_000_000,
            "finished_elapsed_ms": self._elapsed_ms(finished_ns),
            "end_boundary": "response_body_read",
        }
        if pending is not None:
            timing.update({
                **pending["context"],
                "started_elapsed_ms": self._elapsed_ms(pending["started_ns"]),
                "duration_ms": (finished_ns - pending["started_ns"]) / 1_000_000,
            })
        return timing

    def _finish_pending_requests(self, span_id: int, finished_ns: int, error: BaseException | None) -> None:
        # Public HTTP hooks have no transport-error callback. The enclosing SDK
        # exit is the first available failure boundary, not a claimed wire end.
        with self._lock:
            unfinished = [
                (request_id, pending) for request_id, pending in self._pending_requests.items()
                if pending["context"].get("span_id") == span_id
            ]
            for request_id, _ in unfinished:
                del self._pending_requests[request_id]
        for request_id, pending in unfinished:
            self.append(
                "jev_request_unfinished", _at_ns=finished_ns, **{
                    **pending["context"], "request_id": request_id,
                    "started_elapsed_ms": self._elapsed_ms(pending["started_ns"]),
                    "finished_elapsed_ms": self._elapsed_ms(finished_ns),
                    "duration_ms": (finished_ns - pending["started_ns"]) / 1_000_000,
                    "end_boundary": "sdk_call_exit_without_complete_response",
                    "error_type": type(error).__name__ if error is not None else None,
                },
            )

    def close(self) -> dict[str, Any]:
        """Finalize on one worker and wait once, after the action loop has ended."""
        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            return self._completion_timing.copy()
        self._closed = True
        started_ns = perf_counter_ns()
        if self._finished_ns is None:
            self._finished_ns = started_ns
        try:
            # The journal already preserves events if final rendering is interrupted.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="run-report") as worker:
                worker.submit(self._flush).result()
        except BaseException as error:
            self._close_error = error
            raise
        finally:
            self._journal.close()
            finished_ns = perf_counter_ns()
            self._completion_timing = {
                **self._summary(self._finished_ns),
                "report_generation_duration_ms": (finished_ns - started_ns) / 1_000_000,
                "report_write_duration_ms": self._write_ns / 1_000_000,
                "report_write_count": self._write_count,
                "jev_requests_directory": str(self.requests_path),
                "jev_request_file_count": self._request_file_count,
                "jev_request_write_duration_ms": self._request_write_ns / 1_000_000,
                "jev_response_file_count": self._response_file_count,
                "jev_response_write_duration_ms": self._response_write_ns / 1_000_000,
                "through_report_duration_ms": self._elapsed_ms(finished_ns),
            }
        return self._completion_timing.copy()

    def _flush(self) -> None:
        """Save chronological, readable Jev inputs and outputs beside the report."""
        responses = {
            event["parent_span_id"]: event for event in self._events
            if event["event"] == "jev_response" and event.get("parent_span_id") is not None
        }
        advancements = {
            event["parent_span_id"]: event for event in self._events
            if event["event"] == "timing_finished" and event["operation"] == "selection.advancement"
            and event.get("parent_span_id") is not None
        }
        batches: dict[int, list[dict[str, Any]]] = {}
        for event in self._events:
            if event["event"] == "timing_finished" and event["operation"] == "selection.batch":
                batches.setdefault(event["parent_span_id"], []).append(event)
        events = []
        payloads = []
        directions = {"jev_request": "input", "jev_response": "output"}
        payload_count = sum(
            event["event"] in directions and isinstance(event.get("body"), bytes)
            for event in self._events
        )
        file_digits = max(4, len(str(payload_count)))
        for saved in sorted(self._events, key=lambda event: event["_at_ns"]):
            record = {"timestamp": self._timestamp(saved["_at_ns"])}
            record.update({key: value for key, value in saved.items() if key != "_at_ns"})
            if record["event"] == "timing_finished":
                for field in ("started", "finished"):
                    at_ns = self._origin_ns + round(record[f"{field}_elapsed_ms"] * 1_000_000)
                    record[f"{field}_at"] = self._timestamp(at_ns)
                span_id = record["span_id"]
                if record["operation"] == "selection.batch" and span_id in responses:
                    start = responses[span_id]["finished_elapsed_ms"]
                    finish = record["finished_elapsed_ms"]
                    record.update(
                        response_processing_started_elapsed_ms=start,
                        response_processing_finished_elapsed_ms=finish,
                        response_processing_duration_ms=finish - start,
                    )
                elif record["operation"] == "selection.round":
                    if span_id in batches:
                        # Sum local processing across batches even when they
                        # overlap. A continuous interval would count waiting for
                        # another HTTP response as local processing.
                        durations = [
                            batch["finished_elapsed_ms"]
                            - responses[batch["span_id"]]["finished_elapsed_ms"]
                            for batch in batches[span_id] if batch["span_id"] in responses
                        ]
                        if durations:
                            record["response_processing_duration_ms"] = math.fsum(durations) + (
                                advancements[span_id]["duration_ms"] if span_id in advancements else 0
                            )
                    elif span_id in responses and span_id in advancements:
                        # Preserve the contiguous interval for unbatched events
                        # retained by callers using the earlier span structure.
                        start = responses[span_id]["finished_elapsed_ms"]
                        finish = advancements[span_id]["finished_elapsed_ms"]
                        record.update(
                            response_processing_started_elapsed_ms=start,
                            response_processing_finished_elapsed_ms=finish,
                            response_processing_duration_ms=finish - start,
                        )
            if record["event"] in directions and isinstance(record.get("body"), bytes):
                # Inputs and outputs share one sequence; request_id still pairs
                # the two events even when other calls occur between them.
                direction = directions[record["event"]]
                parts = [f"{len(payloads) + 1:0{file_digits}d}", f"jev_{direction}"]
                iteration = record.get("iteration")
                if type(iteration) is int and iteration > 0:
                    parts.append(f"loop_{iteration}")
                stage = {"windows": "window", "controls": "control", "text": "text"}.get(record.get("stage"))
                round_number = record.get("round")
                if stage is not None and type(round_number) is int and round_number > 0:
                    parts.append(f"{stage}_round_{round_number}")
                batch, batch_count = record.get("batch"), record.get("batch_count")
                if type(batch) is int and batch > 0 and type(batch_count) is int and batch_count > 1:
                    parts.append(f"batch_{batch}")
                path = self.requests_path / ("_".join(parts) + ".json")
                file_field = "request_file" if direction == "input" else "response_file"
                record[file_field] = path.relative_to(self.path.parent).as_posix()
                payloads.append((path, record, record["body"]))
            if isinstance(record.get("body"), bytes):
                record.update(_body_fields(record.pop("body")))
            events.append(record)
        text = json.dumps(events, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the full report available even if a separate payload file fails.
        self._write(text)
        for path, record, raw_body in payloads:
            is_request = record["event"] == "jev_request"
            # Use the same readable layout for both directions, retaining all
            # captured fields. Non-JSON bodies keep their original bytes.
            content = (
                _format_payload_json(record["body"]) if "body" in record
                else raw_body
            )
            started_ns = perf_counter_ns()
            try:
                self._write_document(content, path=path)
                if is_request:
                    self._request_file_count += 1
                else:
                    self._response_file_count += 1
            finally:
                duration_ns = perf_counter_ns() - started_ns
                if is_request:
                    self._request_write_ns += duration_ns
                else:
                    self._response_write_ns += duration_ns

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def package_report(directory: Path, *, run_id: str, interrupted: bool = False) -> tuple[Path, dict[str, Any]]:
    """Package only this run's evidence; recover a partial journal after a crash.

    Recovery never opens a Driver connection and cannot replay desktop commands.
    Server-side model/delivery evidence is joined by the authenticated download.
    """
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    events: list[dict[str, Any]] = []
    total = directory / "total_run.json"
    if total.exists():
        try:
            events = json.loads(total.read_text(encoding="utf-8"))
            if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
                raise ValueError("Invalid report event array")
        except (ValueError, OSError):
            events = []
            missing.append("Final total_run.json could not be read; recovered durable journal records instead.")
    if not events:
        journal = directory / "events.jsonl"
        if journal.exists():
            for number, line in enumerate(journal.read_bytes().splitlines(), 1):
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid event record")
                    events.append(event)
                except (ValueError, UnicodeError):
                    missing.append(f"Journal record {number} is incomplete or invalid, usually from interrupted persistence.")
        else:
            missing.append("No worker event journal was created before interruption.")
        events.sort(key=lambda event: event.get("elapsed_ms", 0))
        total.write_text(json.dumps(events, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    if interrupted:
        missing.append("Worker restarted before its terminal result was persisted; remaining observations, screenshots and command outcomes are unknown.")
    if not any(event.get("event") == "run_finished" for event in events):
        missing.append("No completed worker run_finished record is available.")
    observed_execution = any(event.get("event") in {"windows_discovered", "window_observed", "controls_observed", "loop_started"}
                             or (event.get("event") == "timing_started" and event.get("operation") in {"loop.iteration", "loop.cycle"})
                             for event in events)
    if observed_execution:
        if not (directory / "events.jsonl").is_file():
            missing.append("No durable event journal is available for this earlier desktop execution; only its finalized report was retained.")
        if not any(event.get("event") in {"native_call_sent", "native_call_intent", "native_call_returned"} for event in events):
            missing.append("Raw native MCP observations, complete command arguments/results and per-call dispatch timestamps were not captured by this earlier worker.")
        if not any(event.get("event") == "loop_started" for event in events) or not any(event.get("event") == "loop_finished" for event in events):
            missing.append("Exact current-state-to-command-dispatch loop boundaries are unavailable; historical cycle timings include later return/verification/reporting work.")
        if not any(event.get("event") == "screenshot_saved" for event in events) and not any((directory / "screenshots").glob("*.png")):
            missing.append("No screenshot is available for the observed desktop execution.")
    native_intents = {event.get("call_id") for event in events if event.get("event") == "native_call_intent"}
    native_sent = {event.get("call_id") for event in events if event.get("event") == "native_call_sent"}
    native_returned = {event.get("call_id") for event in events if event.get("event") == "native_call_returned"}
    for call_id in sorted(native_intents - native_sent, key=str):
        missing.append(f"Native call {call_id} has durable intent but no captured transport-start marker; dispatch is unknown.")
    for call_id in sorted((native_intents | native_sent) - native_returned, key=str):
        missing.append(f"Native call {call_id} has no captured response; delivery may be uncertain.")
    jev_sent = {event.get("request_id") for event in events if event.get("event") == "jev_request"}
    jev_returned = {event.get("request_id") for event in events if event.get("event") == "jev_response"}
    for request_id in sorted(jev_sent - jev_returned, key=str):
        missing.append(f"Jev API call {request_id} has no complete captured response; token usage and cost are unavailable.")
    for sequence, event in enumerate(events):
        if event.get("event") == "screenshot_error":
            missing.append(f"Screenshot unavailable: loop {event.get('iteration')}, {event.get('phase')} ({event.get('error_type')}).")
        if event.get("event") == "screenshot_saved":
            # Historical reports can be copied from another machine. Resolve
            # only the recorded image basename inside this evidence directory.
            image_name = Path(str(event.get("path", "")).replace("\\", "/")).name
            if not image_name or not (directory / "screenshots" / image_name).is_file():
                missing.append(f"Recorded screenshot file is missing: loop {event.get('iteration')}, {event.get('phase')} ({image_name or 'path unavailable'}).")
        response_body = event.get("body")
        response_usage = response_body.get("usage", {}) if isinstance(response_body, dict) else {}
        input_tokens = event.get("input_tokens")
        if input_tokens is None and isinstance(response_usage, dict):
            input_tokens = response_usage.get("input_tokens")
        if event.get("event") == "jev_response" and input_tokens is None:
            missing.append(f"Jev API call {event.get('request_id')} returned no captured input-token usage; cost is unknown.")
        direction = {"jev_request": "input", "jev_response": "output"}.get(event.get("event"))
        recorded_file = event.get("request_file" if direction == "input" else "response_file")
        if direction and (not isinstance(recorded_file, str) or not (directory / recorded_file).is_file()):
            # A crash can happen before close creates the readable per-call files.
            identity = event.get("request_id")
            identity = identity if type(identity) is int and identity >= 0 else sequence
            path = directory / f"recovered_jev_{direction}_{identity:04d}.json"
            path.write_text(_format_payload_json(event.get("body", {"body_text": event.get("body_text")})), encoding="utf-8")
    manifest = {
        "run_id": run_id, "complete": not missing, "missing_capture": missing,
        "scope": "Available worker evidence; server.json in the combined download includes OpenAI and delivery records.",
        "loop_boundary": "current_state_start_to_last_input_command_dispatch",
        "clock_scope": "worker_monotonic; do not subtract timestamps from different machines",
        "credential_capture": "HTTP authentication headers, worker tokens and environment secrets are excluded",
        "files": [],
    }
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "manifest.json" and not path.name.endswith(".tmp"):
            content = path.read_bytes()
            manifest["files"].append({"path": path.relative_to(directory).as_posix(), "size_bytes": len(content),
                                      "sha256": hashlib.sha256(content).hexdigest()})
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    archive = directory.with_suffix(".zip")
    temporary = archive.with_suffix(".zip.tmp")
    with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as bundle:
        os.chmod(temporary, 0o600)
        for entry in manifest["files"]:
            bundle.write(directory / entry["path"], entry["path"])
        bundle.write(directory / "manifest.json", "manifest.json")
    temporary.replace(archive)
    return archive, manifest
