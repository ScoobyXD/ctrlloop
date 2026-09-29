"""Exact-desktop WebSocket gateway with server-enforced RFB input ownership."""
import asyncio
from contextlib import suppress
from itertools import groupby
import json
import logging
import os
import re
import time
from uuid import uuid4

import httpx2
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


logger = logging.getLogger('uvicorn.error.desktop')


class FleetToken:
    def __init__(self):
        self.value = None
        self.expiry = 0
        self.lock = asyncio.Lock()

    async def get(self):
        async with self.lock:
            if self.value and self.expiry > time.monotonic() + 60:
                return self.value
            async with httpx2.AsyncClient(timeout=20) as client:
                response = await client.post(
                    'https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token',
                    data={'grant_type': 'client_credentials', 'client_id': os.environ['CUA_CLIENT_ID'],
                          'client_secret': os.environ['CUA_CLIENT_SECRET']})
                response.raise_for_status()
                payload = response.json()
            self.value = payload['access_token']
            self.expiry = time.monotonic() + payload.get('expires_in', 300)
            return self.value


class RFBInputFilter:
    """Frame client RFB 3.8 messages and identify input requiring ownership.

    The existing WayVNC loopback service uses security type None. Unknown
    messages/security are refused, never blindly forwarded past the handshake.
    This survives message fragmentation/coalescing and does not trust viewOnly JS.
    """
    def __init__(self):
        self.phase = 'version'
        self.pending = bytearray()
        self.message_type = None

    def feed(self, packet):
        self.pending.extend(packet)
        if len(self.pending) > 2_000_000:
            raise ValueError('RFB client message too large')
        output = []
        while self.pending:
            input_message = False
            if self.phase == 'version':
                length = 12
            elif self.phase in ('security', 'init'):
                length = 1
            else:
                kind = self.pending[0]
                self.message_type = kind
                if kind == 0:
                    length = 20
                elif kind == 2:
                    if len(self.pending) < 4: break
                    length = 4 + 4 * int.from_bytes(self.pending[2:4], 'big')
                elif kind == 3:
                    length = 10
                elif kind == 4:
                    length, input_message = 8, True
                elif kind == 5:
                    if len(self.pending) < 2: break
                    # noVNC's extended PointerEvent appends one button byte.
                    # Treating it as a six-byte event corrupts the next frame.
                    length, input_message = (7 if self.pending[1] & 0x80 else 6), True
                elif kind == 6:
                    if len(self.pending) < 8: break
                    length = 8 + abs(int.from_bytes(self.pending[4:8], 'big', signed=True))
                    input_message = True
                elif kind == 150:
                    length = 10
                elif kind == 248:
                    if len(self.pending) < 9: break
                    length = 9 + self.pending[8]
                elif kind == 255:
                    length, input_message = 12, True
                else:
                    raise ValueError('Unsupported RFB client message')
            if length > 2_000_000:
                raise ValueError('RFB client message too large')
            if len(self.pending) < length: break
            message = bytes(self.pending[:length]); del self.pending[:length]
            if self.phase == 'version':
                if message != b'RFB 003.008\n':
                    raise ValueError('Only RFB 3.8 is supported')
                self.phase = 'security'
            elif self.phase == 'security':
                if message != b'\x01':
                    raise ValueError('Unexpected desktop authentication')
                self.phase = 'init'
            elif self.phase == 'init':
                # Every viewer is shared; a client cannot evict another viewer
                # by requesting an exclusive RFB connection.
                message = b'\x01'
                self.phase = 'messages'
            elif message[0] == 255 and message[1] != 0:
                raise ValueError('Unsupported extended RFB input message')
            output.append((message, input_message))
        return output


fleet_token = FleetToken()


async def proxy_desktop(websocket, can_input, session_valid, *, namespace, sandbox):
    # Targets come only from the authenticated member's database assignment.
    if not all(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,199}', value) for value in (namespace, sandbox)):
        raise ValueError('Invalid assigned Fleet desktop')
    upstream = f'wss://run.cua.ai/api/svc/{namespace}/{sandbox}-vnc/websockify'
    connection_id, started = uuid4().hex[:12], time.monotonic()
    parser, remote, first_task = RFBInputFilter(), None, 'connect'
    close_code, close_reason, cause = 1011, 'Desktop connection interrupted; reconnect', 'connection_error'
    details = {}
    metrics = dict(client_messages=0, server_messages=0, protocol_packets=0,
                   input_packets=0, input_forwarded=0, input_dropped=0,
                   permission_checks=0, permission_total_ms=0.0, permission_max_ms=0.0,
                   upstream_bytes=0, downstream_bytes=0)
    try:
        token = await asyncio.wait_for(fleet_token.get(), 20)
        async with connect(upstream, additional_headers={'Authorization': f'Bearer {token}'},
                           subprotocols=['binary'], max_size=20_000_000,
                           open_timeout=20, close_timeout=5) as remote:
            await asyncio.wait_for(websocket.accept(subprotocol='binary' if 'binary' in websocket.scope.get('subprotocols', []) else None), 5)
            logger.info(json.dumps({'event': 'desktop_connected', 'connection_id': connection_id,
                                    'connect_ms': round((time.monotonic()-started)*1000, 2)}))

            async def send(packet):
                await asyncio.wait_for(remote.send(packet), 5)
                metrics['upstream_bytes'] += len(packet)

            async def inbound():
                while True:
                    message = await websocket.receive()
                    if message['type'] == 'websocket.disconnect':
                        raise WebSocketDisconnect(message.get('code', 1000))
                    data = message.get('bytes')
                    if data is None:
                        raise ValueError('Binary RFB frames are required')
                    metrics['client_messages'] += 1
                    # Protocol/frame requests must never wait on the database.
                    # Adjacent input packets in one frame share one locked check.
                    for requires_input, packets in groupby(parser.feed(data), key=lambda packet: packet[1]):
                        if not requires_input:
                            for packet, _ in packets:
                                metrics['protocol_packets'] += 1
                                await send(packet)
                            continue
                        async with asyncio.timeout(5):
                            before = time.monotonic()
                            metrics['permission_checks'] += 1
                            async with can_input() as allowed:
                                elapsed = (time.monotonic()-before)*1000
                                metrics['permission_total_ms'] += elapsed
                                metrics['permission_max_ms'] = max(metrics['permission_max_ms'], elapsed)
                                for packet, _ in packets:
                                    metrics['input_packets'] += 1
                                    if allowed:
                                        await send(packet)
                                        metrics['input_forwarded'] += 1
                                    else:
                                        metrics['input_dropped'] += 1

            async def outbound():
                async for message in remote:
                    if not isinstance(message, bytes):
                        raise ValueError('Upstream returned a nonbinary RFB frame')
                    await asyncio.wait_for(websocket.send_bytes(message), 5)
                    metrics['server_messages'] += 1
                    metrics['downstream_bytes'] += len(message)

            async def expiry():
                while await asyncio.wait_for(session_valid(), 5):
                    await asyncio.sleep(15)

            first_finished = asyncio.get_running_loop().create_future()
            def finished(task):
                if not first_finished.done(): first_finished.set_result(task)
            tasks = [asyncio.create_task(fn(), name=name) for name, fn in
                     (('browser_input', inbound), ('upstream_frames', outbound), ('session_expiry', expiry))]
            for task in tasks: task.add_done_callback(finished)
            try:
                completed = await first_finished
                first_task = completed.get_name()
                details['upstream_close_code'] = remote.close_code
                completed.result()  # Surface the originating error before peer cancellation.
                if first_task == 'session_expiry':
                    cause, close_code, close_reason = 'session_expired', 1008, 'Desktop session expired; sign in again'
                else:
                    cause = 'upstream_closed'
            finally:
                for task in tasks: task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for task, result in zip(tasks, results):
                    if task.get_name() != first_task and isinstance(result, Exception):
                        logger.warning(json.dumps({'event': 'desktop_peer_error', 'connection_id': connection_id,
                                                   'task': task.get_name(), 'error_type': type(result).__name__}))
    except WebSocketDisconnect as error:
        cause, close_code, close_reason = 'browser_disconnected', 1000, 'Desktop viewer closed'
        details['browser_close_code'] = error.code
    except asyncio.CancelledError:
        cause, close_code, close_reason = 'gateway_shutdown', 1001, 'Desktop gateway restarting; reconnect'
        raise
    except Exception as error:
        details['error_type'] = type(error).__name__
        if isinstance(error, PermissionError):
            cause, close_code, close_reason = 'access_revoked', 1008, 'Desktop access changed; sign in again'
        elif isinstance(error, ValueError) and first_task == 'browser_input':
            cause, close_code, close_reason = 'rfb_protocol_error', 1008, 'Unsupported desktop protocol message'
            details.update(parser_phase=parser.phase, message_type=parser.message_type,
                           buffered_bytes=len(parser.pending))
        elif isinstance(error, ConnectionClosed):
            cause = 'upstream_connection_closed'
            details['upstream_close_code'] = error.rcvd.code if error.rcvd else None
            details['upstream_sent_close_code'] = error.sent.code if error.sent else None
        elif isinstance(error, InvalidStatus):
            cause = 'upstream_handshake_rejected'
            details['upstream_http_status'] = error.response.status_code
        elif isinstance(error, TimeoutError):
            cause = 'gateway_operation_timeout'
    finally:
        # Never log tokens, packet contents, keys, clipboard text, or raw errors.
        record = {'event': 'desktop_closed', 'connection_id': connection_id, 'first_task': first_task,
                  'cause': cause, 'close_code': close_code, 'duration_ms': round((time.monotonic()-started)*1000, 2),
                  **details, **{key: round(value, 2) if isinstance(value, float) else value for key, value in metrics.items()}}
        (logger.info if cause == 'browser_disconnected' else logger.warning)(json.dumps(record))
        with suppress(RuntimeError, OSError, TimeoutError, WebSocketDisconnect):
            await asyncio.wait_for(websocket.close(code=close_code, reason=close_reason), 5)
