"""Cross-process invalidation for the existing desktop policy WebSockets."""
import asyncio
from contextlib import suppress
import logging

from psycopg import AsyncConnection


CHANNEL = 'ctrlloop_desktop_policy'
logger = logging.getLogger('uvicorn.error.desktop')


class DesktopPolicyNotifications:
    """An interrupted LISTEN connection invalidates every grant it protected.

    PostgreSQL delivers committed changes to every application process. The
    dedicated autocommit connection never occupies an application transaction
    or performs periodic authorization queries.
    """

    def __init__(self, conninfo):
        self.conninfo = conninfo
        self.ready = asyncio.Event()
        self.subscriptions = {}
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.listen(), name='desktop_policy_notifications')
        # A temporary listener failure disables viewers, not the whole website.
        with suppress(TimeoutError):
            await asyncio.wait_for(self.ready.wait(), 10)

    def invalidate(self, scope=None):
        for changed, scopes in tuple(self.subscriptions.items()):
            if scope in (None, '*') or scopes is None or scope in scopes:
                changed.set()

    def subscribe(self):
        if not self.ready.is_set():
            raise PermissionError('Desktop authorization notifications are unavailable')
        changed = asyncio.Event()
        # Subscribe before querying identity. Any change during admission
        # invalidates it until its exact actor/target scopes have been resolved.
        self.subscriptions[changed] = None
        return changed

    def bind(self, changed, scopes):
        if changed.is_set() or not self.ready.is_set():
            raise PermissionError('Desktop authorization changed during validation')
        self.subscriptions[changed] = frozenset(scopes)

    def unsubscribe(self, changed):
        self.subscriptions.pop(changed, None)

    async def listen(self):
        try:
            while True:
                try:
                    async with await AsyncConnection.connect(
                            self.conninfo, autocommit=True, connect_timeout=5,
                            keepalives=1, keepalives_idle=10,
                            keepalives_interval=5, keepalives_count=2) as connection:
                        await connection.execute('LISTEN ' + CHANNEL)
                        self.ready.set()
                        async for notification in connection.notifies():
                            self.invalidate(notification.payload)
                        raise ConnectionError('Desktop notification listener ended')
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Never log a DSN, token hash, payload, or driver exception.
                    logger.warning('desktop notification listener unavailable error=%s', type(error).__name__)
                finally:
                    self.ready.clear()
                    self.invalidate()
                await asyncio.sleep(1)
        finally:
            self.ready.clear()
            self.invalidate()

    async def close(self):
        self.ready.clear()
        self.invalidate()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
