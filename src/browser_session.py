"""Read a running Chrome session into Browser Use's native storage-state format.

The source connection only reads cookies and storage. Browser Use runs actions
in its separate worker; it never owns or shuts down the source browser.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
import ipaddress
import logging
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

import psutil
import websockets
from cdp_use.client import CDPClient
from cdp_use.cdp.network import CookieParam


_CONSENT_TIMEOUT_SECONDS = 60
_COMMAND_TIMEOUT_SECONDS = 10
_TRANSPORT_LOGGER = logging.Logger("ctrlloop.session.transport")
_TRANSPORT_LOGGER.disabled = True


def _failure(message: str, code: str):
    # driver imports this module only when a worker starts; keep one error API.
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


def _source_endpoint(source_root: Path, executable: str) -> tuple[str, list[str]]:
    """Read only this profile's endpoint and verify its live process owner."""
    try:
        lines = (source_root / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise _failure(
            "Open Chrome and enable 'Allow remote debugging for this browser instance' at "
            "chrome://inspect/#remote-debugging, then retry. Chrome can stay open. "
            "This requires Chrome 144 or newer.",
            "browser_session_connection_required",
        ) from None
    if (
        len(lines) != 2
        or not lines[0].isdigit()
        or not 0 < int(lines[0]) < 65536
        or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", lines[1])
    ):
        raise _failure("Chrome's live-session endpoint is invalid. Re-enable remote debugging and retry.",
                       "browser_session_endpoint_invalid")
    port = int(lines[0])
    try:
        listeners = [
            connection for connection in psutil.net_connections(kind="tcp")
            if connection.status == psutil.CONN_LISTEN and connection.laddr.port == port
        ]
        if not listeners or any(
            not ipaddress.ip_address(connection.laddr.ip).is_loopback for connection in listeners
        ):
            raise _failure(
                "Chrome's saved live-session endpoint is not an active loopback listener. "
                "Open Chrome and enable remote debugging, then retry.",
                "browser_session_endpoint_unavailable",
            )
        owners = {connection.pid for connection in listeners}
        if len(owners) != 1 or None in owners:
            raise _failure("The live-session connection cannot be matched to Chrome.",
                           "browser_session_endpoint_unverified")
        process = psutil.Process(owners.pop())
        if not _same_path(process.exe(), executable):
            raise _failure("The live-session endpoint belongs to a different executable.",
                           "browser_session_endpoint_unverified")
        arguments = process.cmdline()
        declared_root = _flag(arguments, "--user-data-dir")
        if declared_root:
            actual_root = Path(declared_root)
        else:
            from browser_use.browser.chrome import get_chrome_profile_path

            detected_root = get_chrome_profile_path(None, executable_path=executable)
            if detected_root is None:
                raise _failure("The running browser's profile directory cannot be verified.",
                               "browser_session_endpoint_unverified")
            actual_root = Path(detected_root)
        if not _same_path(actual_root, source_root):
            raise _failure("The live-session endpoint belongs to a different browser profile directory.",
                           "browser_session_endpoint_unverified")
        host = "127.0.0.1" if any(c.laddr.ip == "127.0.0.1" for c in listeners) else "[::1]"
    except (psutil.Error, OSError, ValueError):
        raise _failure("The running Chrome process and its live-session connection could not be verified.",
                       "browser_session_endpoint_unverified") from None
    return f"ws://{host}:{port}{lines[1]}", arguments


class _SessionReader(CDPClient):
    async def start(self) -> None:
        """Allow time for Chrome's own connection permission dialog."""
        self.ws = await websockets.connect(
            self.url, max_size=self.max_ws_frame_size,
            open_timeout=_CONSENT_TIMEOUT_SECONDS, close_timeout=2, proxy=None,
            logger=_TRANSPORT_LOGGER,
        )
        self._message_handler_task = asyncio.create_task(self._handle_messages())


async def _read(client: CDPClient, method: str, params: dict | None = None,
                session_id: str | None = None) -> dict:
    return await asyncio.wait_for(client.send_raw(method, params, session_id), _COMMAND_TIMEOUT_SECONDS)


@contextmanager
def _private_transport():
    # CDP's diagnostics may include response bodies. Never log authentication
    # values, even when the calling application enables DEBUG logging.
    logger = logging.getLogger("cdp_use.client")
    deny = lambda record: False
    logger.addFilter(deny)
    try:
        yield
    finally:
        logger.removeFilter(deny)


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        default_port = 443 if parsed.scheme == "https" else 80
        suffix = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except ValueError:
        return None


def _cookie_matches(cookie: dict, host: str) -> bool:
    domain = str(cookie.get("domain", "")).lower()
    return host == domain.lstrip(".") or (domain.startswith(".") and host.endswith(domain))


async def _page_storage(client: CDPClient, target_id: str, origin: str) -> dict:
    """Read only an already-open page, preserving focus and navigation."""
    attached = await _read(client, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
    session_id = attached["sessionId"]
    try:
        frames = await _read(client, "Page.getFrameTree", session_id=session_id)
        if _origin(frames.get("frameTree", {}).get("frame", {}).get("url", "")) != origin:
            raise _failure("The source page changed while its login storage was being read. Retry the command.",
                           "browser_session_changed")
        result = {"origin": origin}
        for storage_type, is_local in (("localStorage", True), ("sessionStorage", False)):
            entries = await _read(client, "DOMStorage.getDOMStorageItems", {
                "storageId": {"securityOrigin": origin, "isLocalStorage": is_local},
            }, session_id)
            result[storage_type] = [{"name": name, "value": value} for name, value in entries.get("entries", [])]
        return result
    finally:
        with suppress(Exception):
            await _read(client, "Target.detachFromTarget", {"sessionId": session_id})


async def _profile_path(client: CDPClient, session_id: str) -> str | None:
    result = await _read(client, "Runtime.evaluate", {
        "expression": "document.getElementById('profile_path')?.textContent?.trim()",
        "returnByValue": True, "silent": True,
    }, session_id)
    value = result.get("result", {}).get("value")
    return value if isinstance(value, str) and value else None


async def import_live_session(
    source_root: Path, profile_directory: str, executable: str, url: str,
) -> tuple[dict, dict]:
    """Return native Browser Use storage state and safe import metadata."""
    if __package__:
        from .driver import DriverError
    else:
        from driver import DriverError

    endpoint, _ = await asyncio.to_thread(_source_endpoint, source_root, executable)
    client = _SessionReader(endpoint)
    owned_target: str | None = None
    diagnostic_session: str | None = None
    with _private_transport():
        try:
            try:
                await client.start()
            except TimeoutError:
                raise _failure(
                    "Chrome did not approve the live-session connection within 60 seconds. "
                    "Retry and choose Allow in Chrome's remote-debugging dialog.",
                    "browser_session_permission_required",
                ) from None
            except websockets.exceptions.InvalidStatus:
                raise _failure(
                    "Chrome declined the live-session connection. Retry and choose Allow in Chrome's "
                    "remote-debugging dialog.", "browser_session_permission_required",
                ) from None

            # Chrome's root endpoint can serve several profiles. The browser's
            # own version page identifies the exact live profile, and its target
            # context lets Storage.getCookies fail closed if that profile changes.
            created = await _read(client, "Target.createTarget", {
                "url": "chrome://version/", "background": True, "newWindow": False,
            })
            owned_target = created["targetId"]
            attached = await _read(client, "Target.attachToTarget", {"targetId": owned_target, "flatten": True})
            diagnostic_session = attached["sessionId"]
            actual_profile = None
            profile_deadline = time.monotonic() + 5
            while time.monotonic() < profile_deadline:
                # The internal page may still be replacing its first execution
                # context when createTarget returns.
                with suppress(Exception):
                    actual_profile = await _profile_path(client, diagnostic_session)
                if actual_profile:
                    break
                await asyncio.sleep(0.1)
            if not actual_profile or not _same_path(actual_profile, source_root / profile_directory):
                raise _failure(
                    "Chrome's active profile does not match the selected login profile. "
                    "Switch to that profile in Chrome and retry; Chrome can stay open.",
                    "browser_session_profile_mismatch",
                )
            target = (await _read(client, "Target.getTargetInfo", {"targetId": owned_target}))["targetInfo"]
            context_id = target.get("browserContextId")
            if not context_id:
                raise _failure("Chrome did not identify the selected profile's browser context.",
                               "browser_session_profile_unverified")

            response = await _read(client, "Storage.getCookies", {"browserContextId": context_id})
            cookie_fields = CookieParam.__annotations__.keys()
            now = time.time()
            profile_cookies = [cookie for cookie in response.get("cookies", [])
                               if cookie.get("session") or cookie.get("expires", -1) <= 0
                               or cookie["expires"] > now]
            host = (urlsplit(url).hostname or "").lower()
            # Replaying a whole long-lived profile can exceed Chromium's cookie
            # quota and silently evict even the requested site's login cookies.
            # Transfer only cookies whose native domain scope covers this host.
            source_cookies = [cookie for cookie in profile_cookies if _cookie_matches(cookie, host)]
            opaque_count = sum(bool(cookie.get("partitionKeyOpaque")) for cookie in source_cookies)
            if opaque_count:
                raise _failure(
                    "Chrome has cookies for the starting site in an opaque storage partition that cannot "
                    "be restored safely into an isolated worker.", "browser_session_unavailable",
                )
            cookies = []
            for cookie in source_cookies:
                copied = {key: value for key, value in cookie.items() if key in cookie_fields}
                if cookie.get("session") or copied.get("expires", -1) <= 0:
                    copied.pop("expires", None)
                cookies.append(copied)

            origin = _origin(url)
            origins = []
            origin_status = "origin_not_open"
            origin_tab_count = 0
            if origin:
                targets = (await _read(client, "Target.getTargets")).get("targetInfos", [])
                candidates = sorted((
                    target for target in targets
                    if target.get("type") == "page" and target.get("browserContextId") == context_id
                    and _origin(target.get("url", "")) == origin
                ), key=lambda target: target["targetId"])
                if candidates:
                    # localStorage is shared; sessionStorage is per-tab. Do not
                    # silently select a different login/app state when tabs vary.
                    stores = [await _page_storage(client, target["targetId"], origin) for target in candidates]
                    for item in stores:
                        for storage_type in ("localStorage", "sessionStorage"):
                            item[storage_type].sort(key=lambda value: value["name"])
                    if any(item != stores[0] for item in stores[1:]):
                        raise _failure(
                            "Several open tabs for the starting site have different session storage. "
                            "Keep the tab whose session should be reused, close the other tabs for that site, "
                            "and retry.", "browser_session_ambiguous",
                        )
                    origins.append(stores[0])
                    origin_tab_count = len(candidates)
                    origin_status = "imported_from_open_tab"

            final_profile = await _profile_path(client, diagnostic_session)
            if not final_profile or not _same_path(final_profile, source_root / profile_directory):
                raise _failure("The source browser profile changed during login-session import. Retry the command.",
                               "browser_session_changed")
            metadata: dict[str, Any] = {
                "cookie_import_status": "imported_live_session",
                "cookie_scope": "starting_host",
                "source_profile_cookie_count": len(profile_cookies),
                "excluded_cookie_count": len(profile_cookies) - len(source_cookies),
                "source_unexpired_cookie_count": len(source_cookies),
                "source_origin_cookie_count": len(source_cookies),
                "imported_cookie_count": len(cookies),
                "omitted_opaque_cookie_count": opaque_count,
                "origin_storage_status": origin_status,
                "origin_storage_tab_count": origin_tab_count,
                "local_storage_item_count": sum(len(item.get("localStorage", [])) for item in origins),
                "session_storage_item_count": sum(len(item.get("sessionStorage", [])) for item in origins),
                "source_profile_verified": True,
            }
            return {"cookies": cookies, "origins": origins}, metadata
        except DriverError:
            raise
        except Exception:
            # Underlying CDP errors can contain values or transport details.
            raise _failure(
                "The live Chrome login session could not be imported. Keep the selected profile open, "
                "allow Chrome's remote-debugging connection, and retry.",
                "browser_session_import_failed",
            ) from None
        finally:
            if diagnostic_session:
                with suppress(Exception):
                    await _read(client, "Target.detachFromTarget", {"sessionId": diagnostic_session})
            if owned_target:
                with suppress(Exception):
                    await _read(client, "Target.closeTarget", {"targetId": owned_target})
            with suppress(Exception):
                await client.stop()
