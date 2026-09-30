"""Fleet viewer credentials and the shared RFB input framing contract.

Pixels are relayed only inside the assigned guest (desktop_stream.py).
"""
from contextlib import asynccontextmanager
import asyncio
import logging
import re
from urllib.parse import urlsplit, urlencode


logger = logging.getLogger('uvicorn.error.desktop')
STREAM_PROTOCOL = 'ctrlloop-direct-v2'
SIGNED_URL_SECONDS = 120


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


@asynccontextmanager
async def desktop_connection(worker, environment):
    """Use Fleet's existing signed-service API; never create/release a claim.

    An issuance failure revokes its URL. Successful URLs expire at Fleet after
    two minutes; the guest separately enforces ongoing CtrlLoop authorization.
    """
    from cua_sandbox import Sandbox

    namespace, claim = worker['fleet_namespace'], worker['fleet_claim']
    if not all(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,199}', value or '')
               for value in (namespace, claim, worker['id'])):
        raise ValueError('Invalid assigned Fleet desktop')
    reference = {'version': 1, 'provider': 'fleet', 'namespace': namespace,
                 'pool': namespace, 'claim': claim, 'service': 'vnc'}
    async with Sandbox.from_dict(reference) as sandbox:
        if sandbox.name != worker['id']:
            raise PermissionError('Fleet claim no longer matches the assigned desktop')
        # Check BEFORE issuing any bearer URL. An old unguarded websockify
        # bridge must never receive credentials intended for the policy gate.
        response = await sandbox.services.request('vnc', method='GET', path='/health')
        response.raise_for_status()
        health = response.json()
        expected = {'protocol': STREAM_PROTOCOL, 'worker_id': worker['id'],
                    'environment': environment, 'company_id': str(worker['company_id']),
                    'assigned_user_id': str(worker['assigned_user_id'])}
        if not isinstance(health, dict) or any(health.get(key) != value for key, value in expected.items()):
            raise PermissionError('The assigned desktop needs the direct streaming worker')
        signed = await sandbox.services.create_signed_url(
            'vnc', expires_in_seconds=SIGNED_URL_SECONDS, label='CtrlLoop viewer')
        try:
            if (signed.namespace != namespace or signed.claim != claim or signed.sandbox != worker['id']
                    or signed.service != 'vnc' or signed.revoked_at):
                raise PermissionError('Fleet returned a different signed service assignment')
            parsed = urlsplit(signed.url)
            # Treat Fleet's token as opaque; signed formats may contain dots.
            match = re.fullmatch(r'/api/signed-svc/([A-Za-z0-9._~-]{20,2048})/', parsed.path)
            if (parsed.scheme != 'https' or parsed.netloc != 'run.cua.ai' or not match
                    or parsed.query or parsed.fragment):
                raise ValueError('Unexpected Fleet signed service origin or path')
            credential = match[1]
            # Fleet strips its routing prefix. Reuse the SAME capability so the
            # guest can resolve its session binding, instead of minting another.
            url = 'wss://run.cua.ai' + parsed.path + 'websockify?' + urlencode({'credential': credential})
            yield signed, credential, url
        except BaseException:
            try:
                await asyncio.wait_for(sandbox.services.revoke_signed_url(signed), 5)
            except Exception as error:
                logger.warning('desktop credential cleanup failed error=%s', type(error).__name__)
            raise
