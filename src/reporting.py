"""Readable run reports and public HTTP hooks for the actual Jev payloads."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from time import perf_counter_ns, time_ns
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

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


class RunReport:
    """Keep each completed observation durable without reconstructing API data.

    Reports intentionally include request and observed UI content. HTTP headers,
    URL credentials/query strings, and exception messages are never recorded by
    these hooks. The caller supplies run-level progress and terminal events.
    """

    def __init__(
        self, directory: Path, *, origin_ns: int | None = None,
        origin_wall_ns: int | None = None, origin_label: str = "report_construction",
    ) -> None:
        self._origin_ns = perf_counter_ns() if origin_ns is None else origin_ns
        self._origin_wall_ns = time_ns() if origin_wall_ns is None else origin_wall_ns
        self.origin_label = origin_label
        self._span_count = 0
        self._contexts: list[dict[str, Any]] = []
        self._pending_requests: dict[int, dict[str, Any]] = {}
        self._first_request_ns: int | None = None
        self._write_ns = 0
        self._write_count = 0
        self._append_ns = 0
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = directory / f"run-{stamp}-{uuid4().hex}.json"
        self._events: list[dict[str, Any]] = []
        self._closed = False
        self._request_count = 0
        self._write("[]\n")

    def _write(self, text: str) -> None:
        started_ns = perf_counter_ns()
        try:
            self._write_document(text)
        finally:
            self._write_ns += perf_counter_ns() - started_ns
            self._write_count += 1

    def _write_document(self, text: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        # Unlike tempfile.mkstemp on Windows, permission failures fail promptly.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                output = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            except BaseException:
                os.close(descriptor)
                raise
            with output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            # Close before replacing so Windows can swap the complete document.
            os.replace(temporary, self.path)
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
        return self._contexts[-1].copy() if self._contexts else {}

    def _summary(self, at_ns: int) -> dict[str, Any]:
        return {
            "origin": self.origin_label,
            "duration_ms": self._elapsed_ms(at_ns),
            "time_to_first_jev_request_ms": (
                self._elapsed_ms(self._first_request_ns) if self._first_request_ns is not None else None
            ),
            "report_write_duration_ms": self._write_ns / 1_000_000,
            "report_write_count": self._write_count,
            "report_append_duration_ms": self._append_ns / 1_000_000,
            "end_boundary": "terminal_event_before_its_serialization_and_write",
        }

    def append(self, event: str, *, _at_ns: int | None = None, **data: Any) -> None:
        if self._closed:
            raise ValueError("Cannot append to a closed run report")
        entered_ns = perf_counter_ns()
        at_ns = entered_ns if _at_ns is None else _at_ns
        record = {
            "timestamp": self._timestamp(at_ns),
            "elapsed_ms": self._elapsed_ms(at_ns),
            "event": event,
            **self._context(),
            **data,
        }
        if event in {"run_finished", "run_error", "run_interrupted"}:
            record["timing"] = self._summary(at_ns)
        try:
            text = json.dumps([*self._events, record], ensure_ascii=False, allow_nan=False, indent=2) + "\n"
            # Snapshot nested data so later caller mutations cannot rewrite history.
            events = json.loads(text)
            self._write(text)
            self._events = events
        finally:
            self._append_ns += perf_counter_ns() - entered_ns

    def _new_span(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        self._span_count += 1
        context = self._context()
        return {
            **context, **data, "operation": operation,
            "parent_span_id": context.get("span_id"), "span_id": self._span_count,
        }

    def _finish_span(
        self, span: dict[str, Any], started_ns: int, finished_ns: int, status: str,
        error: BaseException | None = None,
    ) -> None:
        span.update(
            started_elapsed_ms=self._elapsed_ms(started_ns),
            finished_elapsed_ms=self._elapsed_ms(finished_ns),
            started_at=self._timestamp(started_ns), finished_at=self._timestamp(finished_ns),
            duration_ms=(finished_ns - started_ns) / 1_000_000, status=status,
        )
        if error is not None:
            span["error_type"] = type(error).__name__
        self.append("timing_finished", _at_ns=finished_ns, **span)

    @contextmanager
    def span(self, operation: str, **data: Any):
        """Time work, excluding its own durable boundary writes.

        The saved start is an intent boundary. The completed event also carries
        the actual work start (after that write). Child events/writes remain part
        of their parent's elapsed time. This records boundaries without adding
        recovery, retries, or suppression of reporting exceptions.
        """
        span = self._new_span(operation, data)
        self.append("timing_started", **span)
        started_ns = perf_counter_ns()
        self._contexts.append(span.copy())
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
                if operation == "selection.sdk_call":
                    self._finish_pending_requests(span["span_id"], finished_ns, failure)
                self._finish_span(span, started_ns, finished_ns, status, failure)
            finally:
                self._contexts.pop()

    def record_interval(
        self, operation: str, started_ns: int, finished_ns: int, **data: Any,
    ) -> None:
        """Backfill measured work that could not safely write a start boundary."""
        span = self._new_span(operation, {**data, "recorded_after_completion": True})
        self.append("timing_started", _at_ns=started_ns, **span)
        self._finish_span(span, started_ns, finished_ns, "ok")

    def on_request(self, request: httpx2.Request) -> None:
        """Record final SDK-encoded data before the network attempt begins."""
        started_ns = perf_counter_ns()
        self._request_count += 1
        request_id = self._request_count
        request.extensions[_REQUEST_ID_EXTENSION] = request_id
        content = request.content
        self.append(
            "jev_request", _at_ns=started_ns, request_id=request_id, method=request.method,
            url=str(request.url.copy_with(userinfo=b"", query=None, fragment=None)),
            body_bytes=len(content), **_body_fields(content),
        )
        self._pending_requests[request_id] = {"started_ns": started_ns, "context": self._context()}
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
        self.append(
            "jev_response", _at_ns=finished_ns, request_id=request_id, **timing,
            status_code=response.status_code,
            **_body_fields(content),
        )

    def _request_timing(self, request_id: int | None, finished_ns: int, headers_ns: int) -> dict[str, Any]:
        pending = self._pending_requests.pop(request_id, None)
        timing = {
            "response_headers_elapsed_ms": self._elapsed_ms(headers_ns),
            "response_body_read_duration_ms": (finished_ns - headers_ns) / 1_000_000,
            "finished_elapsed_ms": self._elapsed_ms(finished_ns),
            "end_boundary": "response_body_read",
        }
        if pending is not None:
            timing.update(
                **pending["context"], started_elapsed_ms=self._elapsed_ms(pending["started_ns"]),
                duration_ms=(finished_ns - pending["started_ns"]) / 1_000_000,
            )
        return timing

    def _finish_pending_requests(self, span_id: int, finished_ns: int, error: BaseException | None) -> None:
        # Public HTTP hooks have no transport-error callback. The enclosing SDK
        # exit is the first available failure boundary, not a claimed wire end.
        for request_id, pending in list(self._pending_requests.items()):
            if pending["context"].get("span_id") != span_id:
                continue
            del self._pending_requests[request_id]
            self.append(
                "jev_request_unfinished", _at_ns=finished_ns, **pending["context"],
                request_id=request_id, started_elapsed_ms=self._elapsed_ms(pending["started_ns"]),
                finished_elapsed_ms=self._elapsed_ms(finished_ns),
                duration_ms=(finished_ns - pending["started_ns"]) / 1_000_000,
                end_boundary="sdk_call_exit_without_complete_response",
                error_type=type(error).__name__ if error is not None else None,
            )

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
