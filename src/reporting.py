"""Readable run reports and public HTTP hooks for the actual Jev payloads."""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
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

    def __init__(self, directory: Path) -> None:
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = directory / f"run-{stamp}-{uuid4().hex}.json"
        self._events: list[dict[str, Any]] = []
        self._closed = False
        self._request_count = 0
        self._write("[]\n")

    def _write(self, text: str) -> None:
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

    def append(self, event: str, **data: Any) -> None:
        if self._closed:
            raise ValueError("Cannot append to a closed run report")
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event,
            **data,
        }
        text = json.dumps([*self._events, record], ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        # Snapshot nested data so later caller mutations cannot rewrite history.
        events = json.loads(text)
        self._write(text)
        self._events = events

    def on_request(self, request: httpx2.Request) -> None:
        """Record final SDK-encoded data before the network attempt begins."""
        self._request_count += 1
        request_id = self._request_count
        request.extensions[_REQUEST_ID_EXTENSION] = request_id
        content = request.content
        self.append(
            "jev_request", request_id=request_id, method=request.method,
            url=str(request.url.copy_with(userinfo=b"", query=None, fragment=None)),
            body_bytes=len(content), **_body_fields(content),
        )

    def on_response(self, response: httpx2.Response) -> None:
        """Retain the complete body before the SDK validates or rejects it."""
        request_id = response.request.extensions.get(_REQUEST_ID_EXTENSION)
        try:
            content = response.read()
        except BaseException as error:
            try:
                self.append(
                    "jev_response_error", request_id=request_id,
                    status_code=response.status_code, error_type=type(error).__name__,
                )
            except BaseException:
                # A secondary report failure must not replace a read failure or cancellation.
                pass
            raise
        self.append(
            "jev_response", request_id=request_id, status_code=response.status_code,
            **_body_fields(content),
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
