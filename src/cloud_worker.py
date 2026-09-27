"""Outbound-only assigned worker. GUI mutations are never automatically replayed."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx2
from .worker import execute_instruction


async def main():
    endpoint=os.environ['CTRLLOOP_API_URL'].rstrip('/')
    if not endpoint.startswith('https://'): raise RuntimeError('Worker delivery requires HTTPS')
    state=Path(os.environ.get('CTRLLOOP_WORKER_STATE',str(Path.home()/'.local/state/ctrlloop-worker')))
    state.mkdir(parents=True,exist_ok=True)
    # A system-wide per-user lock also prevents two revision services racing.
    import fcntl
    lock=(state/'worker.lock').open('a+')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    journal=state/'active-run.json'
    def write_journal(value):
        temporary=state/'active-run.tmp'
        with temporary.open('w') as stream:
            json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
        temporary.replace(journal)
        directory=os.open(state,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    revision=os.environ.get('CTRLLOOP_REVISION','unknown')
    async with httpx2.AsyncClient(base_url=endpoint,headers={'Authorization':'Bearer '+os.environ['WORKER_TOKEN']},timeout=25) as client:
        async def send(path,body):
            response=await client.post(path,json=body);response.raise_for_status();return response.json()

        # Crash journal carries result or interruption, never restarts GUI actions.
        async def settle_journal():
            if not journal.exists():return
            pending=json.loads(journal.read_text())
            body=pending.get('finish') or {'status':'interrupted','error':'Worker restarted during execution. Actions were not replayed; inspect the desktop before a new run.','result':{}}
            try: await send(f"/api/worker/runs/{pending['id']}/finish",body)
            except httpx2.HTTPStatusError as error:
                if error.response.status_code != 409: raise
            journal.unlink()

        await settle_journal()
        await send('/api/worker/recover',{})
        async def heartbeat():
            while True:
                try: await send('/api/worker/heartbeat',{})
                except Exception: pass
                await asyncio.sleep(5)
        heartbeat_task=asyncio.create_task(heartbeat())
        try:
            while True:
                try:
                    await settle_journal()
                    try:
                        payload=await send('/api/worker/poll',{'revision':revision})
                    except Exception:
                        # An interrupted poll may have committed delivery in the DB.
                        # No GUI execution has started; mark that delivery uncertain.
                        await send('/api/worker/recover',{})
                        raise
                    run=payload.get('run')
                    if not run:
                        await asyncio.sleep(2);continue
                    identity=run['id']
                    write_journal({'id':identity})
                    async def emit(details):
                        details=dict(details)
                        stage=details.pop('stage','worker_progress')
                        message=details.pop('message',stage.replace('_',' ').capitalize())
                        await send(f'/api/worker/runs/{identity}/events',{'id':str(uuid4()),'stage':stage,'message':message,'data':details})
                    try:
                        await emit({'stage':'delivery_acknowledged','message':'VM received and journaled the exact persisted instruction','instruction':run['instruction'],'revision':revision})
                        result=await execute_instruction(run['instruction'],emit)
                        status='succeeded' if result.get('verified_url') and result.get('status') in ('verified','succeeded') else ('blocked' if result.get('status')=='blocked' else 'error')
                        finish={'status':status,'result':result,'error':None if status=='succeeded' else result.get('error','The browser destination was not verified.')}
                    except Exception as error:
                        finish={'status':'error','result':{},'error':f'Worker execution stopped ({type(error).__name__}); GUI actions were not retried.'}
                    write_journal({'id':identity,'finish':finish})
                    await settle_journal()
                except Exception as error:
                    print(json.dumps({'stage':'worker_connection_error','type':type(error).__name__}),flush=True)
                    await asyncio.sleep(5)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError): await heartbeat_task


if __name__=='__main__': asyncio.run(main())
