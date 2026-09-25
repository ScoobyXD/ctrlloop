"""Read-only local viewer for the agent tab in an existing Browser Use session."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from browser_use.browser.session import BrowserSession


_VIEWER = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ctrl Loop · Live preview</title>
<style>
* { box-sizing: border-box } body { margin: 0; background: #111827; color: #e5e7eb; font: 15px system-ui, sans-serif }
header { padding: 16px 22px; border-bottom: 1px solid #374151; display: flex; gap: 18px; flex-wrap: wrap; align-items: center }
h1 { margin: 0; font-size: 17px } #status { color: #9ca3af } main { padding: 18px; text-align: center }
img { max-width: 100%; max-height: calc(100vh - 125px); object-fit: contain; background: white; border-radius: 6px }
.note { font-size: 12px; color: #9ca3af; margin: 0 0 12px }
</style>
<header><h1>Ctrl Loop · Live preview</h1><span id="status" role="status">Waiting for browser…</span></header>
<main><p class="note">Read-only view of the agent's current tab</p><img id="frame" alt="Agent browser preview" hidden></main>
<script nonce="__NONCE__">
const frame = document.querySelector('#frame');
const status = document.querySelector('#status');
let imageUrl;
async function update() {
  try {
    const response = await fetch('frame.jpg', {cache: 'no-store', signal: AbortSignal.timeout(4000)});
    const message = decodeURIComponent(response.headers.get('X-Preview-Status') || 'Waiting for browser…');
    const captured = Number(response.headers.get('X-Preview-Captured-At'));
    status.textContent = message + (captured ? ' · ' + new Date(captured * 1000).toLocaleTimeString() : '');
    if (response.ok) {
      const next = URL.createObjectURL(await response.blob());
      const previous = imageUrl;
      imageUrl = next;
      frame.src = next;
      frame.hidden = false;
      if (previous) URL.revokeObjectURL(previous);
    }
  } catch (_) {
    status.textContent = 'Preview disconnected · showing the last frame';
  } finally {
    setTimeout(update, 500);
  }
}
update();
</script></html>
"""


class BrowserPreview:
    """Sample screenshots without refreshing the DOM map or controlling the page.

    Start before or after BrowserSession.start(). Closing the viewer must precede
    browser shutdown. Only the latest JPEG is retained; captures never overlap.
    """

    def __init__(self, browser: BrowserSession, *, interval_seconds: float = 0.5) -> None:
        if interval_seconds < 0.1:
            raise ValueError("Preview interval must be at least 0.1 seconds.")
        self.browser = browser
        self.interval_seconds = interval_seconds
        self.url: str | None = None
        self.status = "Waiting for browser"
        self.last_frame: bytes | None = None
        self.captured_at: float | None = None
        self._token = secrets.token_urlsafe(24)
        self._nonce = secrets.token_urlsafe(24)
        self._server: asyncio.Server | None = None
        self._capture_task: asyncio.Task | None = None
        self._clients: set[asyncio.Task] = set()
        self._authority = ""
        self._closed = False

    async def start(self) -> str:
        """Return a token-protected loopback URL immediately, before first capture."""
        if self._closed:
            raise RuntimeError("This preview has already been closed.")
        if self.url is not None:
            return self.url
        self._server = await asyncio.start_server(
            self._accept, host="127.0.0.1", port=0, limit=8192, backlog=8,
        )
        port = self._server.sockets[0].getsockname()[1]
        self._authority = f"127.0.0.1:{port}"
        self.url = f"http://{self._authority}/{self._token}/"
        self._capture_task = asyncio.create_task(self._capture(), name="browser-preview-capture")
        return self.url

    async def close(self) -> None:
        """Stop sampling and drain every owned task and HTTP connection."""
        self._closed = True
        self.status = "Preview stopped"
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._clients)
        if self._capture_task is not None:
            tasks.append(self._capture_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._capture_task = None
        self._clients.clear()

    async def _capture(self) -> None:
        while True:
            started = time.monotonic()
            try:
                # Do not ask get_or_create_cdp_session() to connect while the
                # normal browser startup is still establishing its session.
                if not self.browser.is_cdp_connected or not self.browser.agent_focus_target_id:
                    self.status = "Waiting for browser"
                else:
                    self.last_frame = await asyncio.wait_for(
                        self.browser.take_screenshot(full_page=False, format="jpeg", quality=65),
                        timeout=3.0,
                    )
                    self.captured_at = time.time()
                    self.status = "Live"
            except Exception as exc:
                # A navigation/closed tab may temporarily prevent capture. Keep
                # the last good frame and retry without affecting the action loop.
                detail = str(exc).strip() or type(exc).__name__
                self.status = f"Capture unavailable: {detail[:180]}"
            await asyncio.sleep(max(0.05, self.interval_seconds - (time.monotonic() - started)))

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed or len(self._clients) >= 8:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer), name="browser-preview-http")
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)
            lines = request.decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ")
            headers = {}
            for line in lines[1:]:
                if line:
                    key, value = line.split(":", 1)
                    key = key.lower()
                    if key in headers:
                        return
                    headers[key] = value.strip()
            origin = headers.get("origin")
            path = target.split("?", 1)[0]
            prefix = f"/{self._token}/"
            viewer_navigation = (
                path == prefix and headers.get("sec-fetch-mode") == "navigate"
                and headers.get("sec-fetch-dest") == "document"
            )
            if (
                method != "GET" or version != "HTTP/1.1"
                or headers.get("host") != self._authority
                or origin not in (None, f"http://{self._authority}")
                or (headers.get("sec-fetch-site") not in (None, "none", "same-origin") and not viewer_navigation)
            ):
                await self._respond(writer, "403 Forbidden", b"Forbidden", "text/plain")
                return
            if path == prefix:
                body = _VIEWER.replace("__NONCE__", self._nonce).encode("utf-8")
                await self._respond(writer, "200 OK", body, "text/html; charset=utf-8")
            elif path == prefix + "frame.jpg":
                body = self.last_frame or b"Waiting for the first frame."
                await self._respond(
                    writer, "200 OK" if self.last_frame else "503 Service Unavailable", body,
                    "image/jpeg" if self.last_frame else "text/plain; charset=utf-8",
                    {"X-Preview-Status": quote(self.status, safe=""),
                     "X-Preview-Captured-At": str(self.captured_at or "")},
                )
            else:
                await self._respond(writer, "404 Not Found", b"Not found", "text/plain")
        except (Exception, asyncio.CancelledError):
            # Malformed requests, disconnects and shutdown never escape into the
            # agent loop or leave unhandled HTTP task exceptions.
            pass
        finally:
            writer.close()
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    async def _respond(
        self, writer: asyncio.StreamWriter, status: str, body: bytes,
        content_type: str, extra_headers: dict[str, str] | None = None,
    ) -> None:
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "Connection": "close",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; img-src 'self' blob:; connect-src 'self'; "
                f"script-src 'nonce-{self._nonce}'; style-src 'unsafe-inline'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
            ),
            **(extra_headers or {}),
        }
        head = f"HTTP/1.1 {status}\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        writer.write(head.encode("ascii") + b"\r\n" + body)
        await asyncio.wait_for(writer.drain(), timeout=3.0)
