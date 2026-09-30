"""Direct Fleet RFB transport, enforced beside the assigned desktop.

Only small authorization messages reach the application. Framebuffer bytes go
between this guest's loopback WayVNC and the browser through Fleet.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from itertools import groupby
import json
import logging
import math
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.protocol import State

from .desktop_gateway import RFBInputFilter


PROTOCOL = 'ctrlloop-direct-v2'
INPUT_SECONDS = 2
CHECK_SECONDS = 24 * 60 * 60
POLICY_CHECK_SECONDS = 7
IDENTITY_KEYS = ('worker_id', 'company_id', 'assigned_user_id', 'environment')
logger = logging.getLogger('ctrlloop.desktop')
# Library tracebacks / DEBUG frame logs can contain the credential-bearing
# request URL or policy messages. Application logs below contain types only.
transport_logger = logging.Logger('ctrlloop.desktop.private', level=logging.CRITICAL + 1)


class InputRecoveryPersistenceError(RuntimeError):
    """Operator recovery must prevent an automatic worker restart."""


def _json(value):
    return json.dumps(value, separators=(',', ':'))


def _http(status, body):
    data = _json(body).encode()
    return Response(status, HTTPStatus(status).phrase,
                    Headers({'Content-Type': 'application/json',
                             'Content-Length': str(len(data)), 'Cache-Control': 'no-store'}), data)


class DesktopStream:
    def __init__(self, endpoint, worker_token, assignment, action_lock):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.path not in ('', '/')
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Desktop policy requires a credential-free HTTPS application origin')
        self.origin = 'https://' + parsed.netloc
        self.policy_url = 'wss://' + parsed.netloc + '/api/worker/desktop-policy'
        self.worker_token = worker_token
        self.assignment = {key: str(assignment[key]) for key in IDENTITY_KEYS}
        self.action_lock = action_lock
        # Serialize authorization snapshots across viewers, never input events.
        # A late reply for a former owner cannot overtake a newer admission.
        self.policy_lock = asyncio.Lock()
        self.control_session = None
        self.control_version = -1
        self.clients = set()
        self.executing = False
        self.execution_epoch = 0
        state = Path(os.environ.get('CTRLLOOP_WORKER_STATE', str(Path.home() / '.local/state/ctrlloop-worker')))
        self.recovery_marker = state / 'desktop-input-recovery-required'
        self.input_healthy = not self.recovery_marker.exists()
        self.persistence_failed = asyncio.Event()

    def latch_input_failure(self):
        self.input_healthy = False
        try:
            descriptor = os.open(self.recovery_marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write('Desktop input requires explicit operator recovery.\n')
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != 'nt':
                directory = os.open(self.recovery_marker.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception:
            # The supervisor exits with status 78. The service must declare
            # RestartPreventExitStatus=78 so lost persistence cannot bypass it.
            self.persistence_failed.set()

    def ensure_input_ready(self):
        if not self.input_healthy:
            raise RuntimeError('Desktop input cleanup failed; operator recovery is required')

    def begin_execution(self):
        # Called while the worker owns action_lock, before its first native
        # action. Revoke input without interrupting the framebuffer stream.
        self.execution_epoch += 1
        self.executing = True
        for client in self.clients:
            client.pause_input()

    async def process_request(self, connection, request):
        if request.path == '/health':
            return _http(200 if self.input_healthy else 503,
                         {'protocol': PROTOCOL, **self.assignment, 'ready': self.input_healthy,
                          'live_permissions': True,
                          'revision': os.environ.get('CTRLLOOP_REVISION', 'unknown')})
        if not self.input_healthy:
            return _http(503, {'error': 'Desktop input requires operator recovery'})
        try:
            parsed = urlsplit(request.path)
            query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=2)
            credentials = query.get('credential', [])
            valid = (parsed.path == '/websockify' and not parsed.fragment
                     and set(query) in ({'credential'}, {'credential', 'permission_events'})
                     and query.get('permission_events', ['1']) == ['1'] and len(credentials) == 1
                     and re.fullmatch(r'[A-Za-z0-9._~-]{20,2048}', credentials[0])
                     and request.headers.get('Origin') == self.origin)
        except (ValueError, KeyError):
            valid = False
        if not valid:
            return _http(403, {'error': 'Desktop connection refused'})
        return None

    async def release_inputs(self):
        """Called with the worker action lock before its first native action.

        Release tracked keys and buttons explicitly: destroying a WayVNC client
        alone does not reliably clear compositor state. Failure latches the
        gate unhealthy and prevents this or subsequent agent actions.
        """
        self.ensure_input_ready()
        affected = [client for client in self.clients if client.has_pressed_input]
        failures = []
        for client in affected:
            try:
                await client.release_pressed_input(action_lock_held=True)
            except Exception as error:
                failures.append(error)
        if failures:
            self.ensure_input_ready()
        # One small acknowledgement per transition, on the existing viewer
        # transport. A departed viewer must not abort an agent's dispatch.
        await asyncio.gather(*(client.notify_permissions('pause', tolerate_disconnect=True)
                               for client in tuple(self.clients)))

    async def handler(self, websocket):
        client = _Viewer(self, websocket)
        self.clients.add(client)
        try:
            await client.run()
        finally:
            self.clients.discard(client)
            client.closed.set()


class _Viewer:
    def __init__(self, gate, websocket):
        self.gate = gate
        self.websocket = websocket
        self.writer = None
        self.closed = asyncio.Event()
        self.stopped = asyncio.Event()
        self.keys = {}
        self.buttons = 0
        self.pointer_packet = None
        self.control_allowed = False
        self.control_session = None
        self.input_epoch = 0
        self.live_permissions = False
        self.permission_events = parse_qs(urlsplit(websocket.request.path).query).get('permission_events') == ['1']
        self.authorization_deadline = 0
        self.authorization_changed = asyncio.Event()

    @property
    def has_pressed_input(self):
        return bool(self.keys or self.buttons)

    def stop(self):
        self.pause_input()
        self.stopped.set()

    def pause_input(self):
        self.control_allowed = False
        self.control_session = None
        self.input_epoch += 1

    def input_generation(self):
        return self.gate.execution_epoch, self.gate.control_version, self.input_epoch

    async def notify_permissions(self, operation, *, tolerate_disconnect=False):
        if not self.permission_events:
            return
        try:
            async with asyncio.timeout(INPUT_SECONDS):
                await self.websocket.send(_json({'op': operation, 'live_permissions': True,
                    'allowed': operation != 'pause' and self.input_allowed(),
                    'control_version': max(0, self.gate.control_version)}))
        except (ConnectionClosed, TimeoutError):
            self.stop()
            if not tolerate_disconnect:
                raise

    def input_allowed(self):
        return (self.control_allowed and not self.stopped.is_set()
                and not self.gate.executing
                and self.control_session == self.gate.control_session
                and asyncio.get_running_loop().time() < self.authorization_deadline)

    async def authorize(self, permission, started, epoch):
        """Apply a connection-level grant; input never contacts the backend."""
        allowed = permission.get('allowed')
        remaining = permission.get('expires_in_seconds')
        session = permission.get('control_session')
        if (not isinstance(allowed, bool) or type(remaining) not in (int, float)
                or not math.isfinite(remaining) or remaining <= 0
                or (allowed and (not isinstance(session, str)
                                 or not re.fullmatch(r'[0-9a-f]{64}', session)))
                or (not allowed and session is not None)):
            raise PermissionError('Invalid desktop authorization')
        # Anchor to request start, not response arrival. A late renewal cannot
        # extend either the session expiry or the daily validation deadline.
        self.authorization_deadline = started + min(remaining, CHECK_SECONDS + POLICY_CHECK_SECONDS)
        self.authorization_changed.set()
        if asyncio.get_running_loop().time() >= self.authorization_deadline:
            raise PermissionError('Desktop authorization expired')
        version, owner = None, session
        if self.live_permissions:
            version = permission.get('control_version')
            mode = permission.get('worker_mode')
            owner = permission.get('current_control_session')
            if (type(version) is not int or version < 0
                    or mode not in ('agent', 'human', 'maintenance')
                    or (owner is not None and (not isinstance(owner, str)
                        or not re.fullmatch(r'[0-9a-f]{64}', owner)))
                    or (mode != 'human' and owner is not None)
                    or (allowed and (mode != 'human' or session != owner))):
                raise PermissionError('Invalid desktop ownership update')
        self.pause_input()
        if self.live_permissions:
            if version < self.gate.control_version:
                await self.release_pressed_input()
                return
            if version > self.gate.control_version:
                # Revocation happens before awaiting the action lock. Every
                # viewer uses the version of the same worker authority row.
                self.gate.control_version = version
                self.gate.control_session = owner
                for client in tuple(self.gate.clients):
                    client.pause_input()
            elif owner != self.gate.control_session:
                raise PermissionError('Desktop ownership update is inconsistent')
        elif self.gate.control_version >= 0:
            # A rolling release can temporarily route a policy socket to an
            # older backend. An unversioned grant cannot supersede ownership
            # already established by a live-permission backend.
            if allowed:
                raise PermissionError('Desktop backend needs versioned ownership')
        if not allowed or epoch != self.gate.execution_epoch:
            # During a run, the worker releases these under its existing lock.
            # Do not wait for the entire run just to apply a view-only grant.
            for client in tuple(self.gate.clients):
                if not client.control_allowed:
                    await client.release_pressed_input()
            return
        async with self.gate.action_lock:
            if (self.stopped.is_set() or self.gate.executing
                    or epoch != self.gate.execution_epoch
                    or (self.live_permissions and version != self.gate.control_version)
                    or asyncio.get_running_loop().time() >= self.authorization_deadline):
                return
            # Different viewers can learn about an ownership change at different
            # times. Revoke and release the former owner before enabling this one.
            for client in tuple(self.gate.clients):
                if not client.control_allowed or (client is not self and client.control_session not in (None, session)):
                    client.pause_input()
                    await client.release_pressed_input(action_lock_held=True)
            self.gate.control_session = self.control_session = session
            self.control_allowed = True

    def record_input(self, packet, *, confirmed=True):
        if packet[0] == 4:
            key, down = ('key', packet[4:8]), packet[1] != 0
        elif packet[0] == 255:
            key, down = ('extended', packet[8:12]), int.from_bytes(packet[2:4], 'big') != 0
        elif packet[0] == 5:
            self.pointer_packet = packet
            buttons = packet[1] & 0x7f
            if packet[1] & 0x80:
                buttons |= packet[6] << 7
            self.buttons = buttons if confirmed else self.buttons | buttons
            return
        else:
            return
        if down:
            self.keys[key] = packet
        elif confirmed:
            self.keys.pop(key, None)

    async def release_pressed_input(self, *, action_lock_held=False):
        """Send only releases derived from input this connection forwarded."""
        if not self.has_pressed_input:
            return
        acquired = False
        if not action_lock_held:
            # Do not queue behind a worker that may itself be waiting for this
            # client's teardown. It clears our tracked state under its lock;
            # observing that state lets this cleanup complete without deadlock.
            while self.has_pressed_input:
                try:
                    async with asyncio.timeout(0):
                        await self.gate.action_lock.acquire()
                    acquired = True
                    break
                except TimeoutError:
                    await asyncio.sleep(0.01)
            if not self.has_pressed_input and not acquired:
                return
        try:
            if not self.has_pressed_input:
                return
            try:
                if self.writer is None or self.writer.is_closing():
                    raise ConnectionError('Desktop input release transport is unavailable')
                deadline = asyncio.get_running_loop().time() + INPUT_SECONDS
                async with asyncio.timeout_at(deadline):
                    for index, packet in enumerate(tuple(self.keys.values())):
                        if index and index % 16 == 0:
                            await asyncio.sleep(0)
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError('Desktop input cleanup expired')
                        release = (packet[:1] + b'\x00' + packet[2:] if packet[0] == 4
                                   else packet[:2] + b'\x00\x00' + packet[4:])
                        self.writer.write(release)
                    if self.buttons:
                        packet = self.pointer_packet
                        release = (b'\x05\x80' + packet[2:6] + b'\x00' if len(packet) == 7
                                   else b'\x05\x00' + packet[2:6])
                        self.writer.write(release)
                    await self.writer.drain()
                self.keys.clear()
                self.buttons = 0
            except BaseException:
                # Preserve uncertainty even if this viewer is removed. Merely
                # reconnecting or clearing a DB run cannot repair stuck input.
                self.gate.latch_input_failure()
                raise
        finally:
            if acquired:
                self.gate.action_lock.release()

    async def run(self):
        policy = None
        admission_locked = False
        tasks = []
        close_code = 1000
        failure_stage = 'admission'
        try:
            credential = parse_qs(urlsplit(self.websocket.request.path).query)['credential'][0]
            # Queue locally before opening the policy socket: the backend's
            # admission timeout must not include another viewer's round trip.
            await self.gate.policy_lock.acquire()
            admission_locked = True
            async with connect(self.gate.policy_url,
                               additional_headers={'Authorization': 'Bearer ' + self.gate.worker_token},
                               max_size=8192, open_timeout=5, close_timeout=1,
                               logger=transport_logger) as policy:
                async def receive_policy():
                    raw = await policy.recv()
                    if not isinstance(raw, str):
                        raise PermissionError('Invalid desktop policy response')
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise PermissionError('Invalid desktop policy response')
                    if value.get('op') == 'pause' and self.live_permissions:
                        return value
                    if value.get('valid') is not True:
                        self.stop()
                        raise PermissionError('Desktop access is no longer authorized')
                    return value

                try:
                    async with asyncio.timeout(POLICY_CHECK_SECONDS):
                        checked_at = asyncio.get_running_loop().time()
                        epoch = self.gate.execution_epoch
                        await policy.send(_json({'credential': credential, 'protocol': PROTOCOL,
                                                 'live_permissions': True}))
                        initial = await receive_policy()
                        if any(str(initial.get(key, '')) != value for key, value in self.gate.assignment.items()):
                            raise PermissionError('Desktop policy assignment changed')
                        reader, self.writer = await asyncio.open_connection('127.0.0.1', 5902)
                        if policy.state is not State.OPEN:
                            raise PermissionError('Desktop policy connection closed')
                        self.live_permissions = initial.get('live_permissions') is True
                        await self.authorize(initial, checked_at, epoch)
                        await self.notify_permissions('update')
                finally:
                    self.gate.policy_lock.release()
                    admission_locked = False
                # Send the capability only at admission; never include it in
                # traffic logs, error messages, or subsequent policy exchanges.
                del credential
                parser = RFBInputFilter()
                pending_check = None

                async def input_packets(packets, epoch, received_with_control):
                    # Only local state and the local action lock are consulted.
                    # No policy request or database query is made per input.
                    if (not received_with_control or not self.input_allowed()
                            or policy.state is not State.OPEN
                            or self.input_generation() != epoch):
                        return
                    self.gate.ensure_input_ready()
                    deadline = asyncio.get_running_loop().time() + INPUT_SECONDS
                    try:
                        async with asyncio.timeout_at(deadline):
                            await self.gate.action_lock.acquire()
                    except TimeoutError:
                        if self.has_pressed_input:
                            self.stop()
                        return
                    try:
                        if (not self.input_allowed() or policy.state is not State.OPEN
                                or self.input_generation() != epoch):
                            return
                        async with asyncio.timeout_at(deadline):
                            for index, packet in enumerate(packets):
                                if index and index % 16 == 0:
                                    await asyncio.sleep(0)
                                if (not self.input_allowed() or policy.state is not State.OPEN
                                        or self.input_generation() != epoch
                                        or asyncio.get_running_loop().time() >= deadline):
                                    return
                                self.writer.write(packet)
                                # Keep uncertainty until drain succeeds so
                                # cancellation still releases pressed input.
                                self.record_input(packet, confirmed=False)
                            await self.writer.drain()
                            for packet in packets:
                                self.record_input(packet)
                    finally:
                        self.gate.action_lock.release()

                async def inbound():
                    pending_epoch = None
                    pending_with_control = False
                    async for packet in self.websocket:
                        epoch = self.input_generation()
                        received_with_control = self.input_allowed()
                        if self.stopped.is_set():
                            return
                        if not isinstance(packet, bytes):
                            raise ValueError('Binary desktop protocol required')
                        # Bound synchronous parsing even when an untrusted
                        # frame packs hundreds of thousands of pointer events.
                        # Large single clipboard messages remain supported by
                        # the existing parser's bounded pending buffer.
                        for offset in range(0, len(packet), 16_384):
                            # A fragmented input message retains the permission
                            # it had when its first bytes arrived. It cannot
                            # become authorized by completing after takeover.
                            chunk_with_control = received_with_control and (
                                not parser.pending or (pending_with_control and pending_epoch == epoch))
                            parsed = parser.feed(packet[offset:offset + 16_384])
                            pending_epoch = epoch if not parser.pending or chunk_with_control else pending_epoch
                            pending_with_control = chunk_with_control
                            for requires_input, grouped in groupby(parsed, key=lambda item: item[1]):
                                packets = [item[0] for item in grouped]
                                if requires_input:
                                    for start in range(0, len(packets), 256):
                                        await input_packets(packets[start:start + 256], epoch,
                                                            chunk_with_control)
                                else:
                                    self.writer.writelines(packets)
                                    async with asyncio.timeout(5):
                                        await self.writer.drain()
                            await asyncio.sleep(0)

                async def outbound():
                    while not self.stopped.is_set():
                        packet = await reader.read(256 * 1024)
                        if not packet or self.stopped.is_set():
                            return
                        async with asyncio.timeout(5):
                            await self.websocket.send(packet)

                async def watchdog():
                    nonlocal pending_check
                    last_check = checked_at
                    while True:
                        await asyncio.sleep(max(0, last_check + CHECK_SECONDS
                                                - asyncio.get_running_loop().time()))
                        async with self.gate.policy_lock:
                            async with asyncio.timeout(POLICY_CHECK_SECONDS):
                                started = asyncio.get_running_loop().time()
                                epoch = self.gate.execution_epoch
                                reply = asyncio.get_running_loop().create_future()
                                pending_check = (started, epoch, reply)
                                await policy.send(_json({'op': 'check'}))
                                await reply
                                pending_check = None
                                last_check = started

                async def policy_updates():
                    # This is the only receiver after admission. Pushed updates
                    # never consume the reply to a requested daily refresh.
                    pause = None
                    while True:
                        if pause is None:
                            permission = await receive_policy()
                        else:
                            async with asyncio.timeout_at(pause[0] + POLICY_CHECK_SECONDS):
                                permission = await receive_policy()
                        operation = permission.get('op')
                        if operation == 'pause' and self.live_permissions:
                            pause = (asyncio.get_running_loop().time(), self.gate.execution_epoch)
                            self.pause_input()
                            async with asyncio.timeout_at(pause[0] + POLICY_CHECK_SECONDS):
                                await self.notify_permissions('pause')
                                await self.release_pressed_input()
                            continue
                        if self.live_permissions and operation == 'update':
                            if pause is None:
                                raise PermissionError('Desktop update has no handoff boundary')
                            started, epoch = pause
                            async with asyncio.timeout_at(started + POLICY_CHECK_SECONDS):
                                await self.authorize(permission, started, epoch)
                                await self.notify_permissions('update')
                            pause = None
                        elif operation == 'check' or (not self.live_permissions and operation is None):
                            if pending_check is None:
                                raise PermissionError('Unexpected desktop policy response')
                            started, epoch, reply = pending_check
                            async with asyncio.timeout_at(started + POLICY_CHECK_SECONDS):
                                await self.authorize(permission, started, epoch)
                                await self.notify_permissions('update')
                            if not reply.done():
                                reply.set_result(None)
                        else:
                            raise PermissionError('Invalid desktop policy operation')

                async def expiry():
                    # Session expiry and a failed daily renewal have a hard
                    # local deadline, even if the renewal task is waiting.
                    while True:
                        self.authorization_changed.clear()
                        remaining = self.authorization_deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise PermissionError('Desktop authorization expired')
                        try:
                            await asyncio.wait_for(self.authorization_changed.wait(), remaining)
                        except TimeoutError:
                            raise PermissionError('Desktop authorization expired') from None

                tasks = [asyncio.create_task(fn(), name=name) for name, fn in
                         (('browser_input', inbound), ('desktop_output', outbound),
                          ('policy_check', watchdog), ('local_stop', self.stopped.wait),
                          ('policy_update', policy_updates),
                          ('authorization_expiry', expiry),
                          ('policy_closed', policy.wait_closed))]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        failure_stage = task.get_name()
                        task.result()
                finally:
                    # Revoke guest transport before policy WebSocket teardown,
                    # which itself can require a close-handshake timeout.
                    self.stop()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            close_code = 1001
            raise
        except ConnectionClosed:
            close_code = 1001
        except Exception as error:
            close_code = 1008 if isinstance(error, (PermissionError, ValueError)) else 1011
            logger.warning('desktop stream closed stage=%s error_type=%s',
                           failure_stage, type(error).__name__)
        finally:
            if admission_locked:
                self.gate.policy_lock.release()
            self.stop()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Close the browser promptly even if releasing native input must
            # wait for an idle worker poll to finish holding action_lock.
            with suppress(Exception):
                async with asyncio.timeout(1):
                    await self.websocket.close(code=close_code, reason='Desktop connection ended; reconnect')
            try:
                await self.release_pressed_input()
            except BaseException as error:
                if self.has_pressed_input:
                    self.gate.latch_input_failure()
                if not isinstance(error, Exception):
                    raise
                logger.error('desktop input cleanup failed error_type=%s', type(error).__name__)
            finally:
                if self.writer is not None:
                    self.writer.close()
                    with suppress(Exception):
                        async with asyncio.timeout(1):
                            await self.writer.wait_closed()


@asynccontextmanager
async def desktop_stream(endpoint, worker_token, assignment, action_lock):
    """Keep listener lifetime coupled to the single assigned worker process."""
    gate = DesktopStream(endpoint, worker_token, assignment, action_lock)
    async with serve(gate.handler, '0.0.0.0', 5900, process_request=gate.process_request,
                     select_subprotocol=lambda connection, offered: 'binary' if 'binary' in offered else None,
                     max_size=2_000_000, max_queue=4, compression=None,
                     open_timeout=5, close_timeout=1, logger=transport_logger) as server:
        # A listener failure must cancel worker execution as well as all
        # viewers. TaskGroup propagates that failure out of the worker loop.
        stopping = False

        async def supervise():
            try:
                await server.serve_forever()
            except asyncio.CancelledError:
                if stopping:
                    raise
                raise RuntimeError('Desktop listener stopped unexpectedly') from None
            raise RuntimeError('Desktop listener stopped unexpectedly')

        async def supervise_persistence():
            await gate.persistence_failed.wait()
            raise InputRecoveryPersistenceError('Cannot persist desktop input recovery requirement')

        async with asyncio.TaskGroup() as tasks:
            listener = tasks.create_task(supervise())
            persistence = tasks.create_task(supervise_persistence())
            try:
                yield gate
            finally:
                stopping = True
                listener.cancel()
                persistence.cancel()
