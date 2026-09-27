"""Exact-desktop WebSocket gateway with server-enforced RFB input ownership."""
import asyncio
from contextlib import suppress
import os
import time

import httpx2
from websockets.asyncio.client import connect


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
    """Parse client RFB 3.8 messages, retaining negotiation and dropping input.

    The existing WayVNC loopback service uses security type None. Unknown
    messages/security are refused, never blindly forwarded past the handshake.
    This survives message fragmentation/coalescing and does not trust viewOnly JS.
    """
    def __init__(self):
        self.phase = 'version'
        self.pending = bytearray()

    def feed(self, packet, allow_input):
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
                    length, input_message = 6, True
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
            if not input_message or allow_input:
                output.append(message)
        return output


fleet_token = FleetToken()


async def proxy_desktop(websocket, can_input, session_valid):
    namespace = os.environ['FLEET_NAMESPACE']
    sandbox = os.environ['FLEET_SANDBOX']
    upstream = f'wss://run.cua.ai/api/svc/{namespace}/{sandbox}-vnc/websockify'
    token = await fleet_token.get()
    async with connect(upstream, additional_headers={'Authorization': f'Bearer {token}'},
                       subprotocols=['binary'], max_size=20_000_000, open_timeout=20) as remote:
        await websocket.accept(subprotocol='binary' if 'binary' in websocket.scope.get('subprotocols', []) else None)
        parser = RFBInputFilter()

        async def inbound():
            while True:
                message = await websocket.receive_bytes()
                async with can_input() as allowed:
                    for packet in parser.feed(message, allowed):
                        await remote.send(packet)

        async def outbound():
            async for message in remote:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)

        async def expiry():
            while await session_valid():
                await asyncio.sleep(15)

        tasks = [asyncio.create_task(fn()) for fn in (inbound, outbound, expiry)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(RuntimeError, OSError):
                await websocket.close()
