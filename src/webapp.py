"""Single-tenant CTRL Loop MVP API. PostgreSQL is the durable contract."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import secrets
import time
from uuid import UUID, uuid4
import zipfile

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx2
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from .desktop_gateway import proxy_desktop
from .run_artifacts import (MAX_ARCHIVE_BYTES, inspect_archive, store_archive,
                            load_archive, worker_record, evidence_bundle)

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'gpt-6-luna'
OPENAI_PRICING = {'model': MODEL, 'input_per_million_usd': 0.10,
                  'cached_input_per_million_usd': 0.01, 'output_per_million_usd': 0.50,
                  'source': 'https://developers.openai.com/api/docs/pricing',
                  'checked_at': '2026-09-28', 'kind': 'usage_estimate_not_invoice'}
WORKER = os.environ.get('WORKER_ID', 'ctrlloop-omarchy-20260927-1708-e33048ae')
generation_tasks = set()
login_attempts = {}
database_pool = None
logger = logging.getLogger('uvicorn.error.desktop')
artifact_slots = asyncio.Semaphore(1)


@asynccontextmanager
async def database():
    if database_pool is None:
        raise RuntimeError('Database pool is not running')
    async with database_pool.connection() as connection:
        yield connection


async def event(db, loop_id, stage, message, *, run_id=None, data=None, event_id=None):
    await db.execute('INSERT INTO events(id,loop_id,run_id,stage,message,data) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING',
                     (event_id or uuid4(), loop_id, run_id, stage, message, Jsonb(data or {})))


@asynccontextmanager
async def lifespan(app):
    global database_pool
    if os.environ.get('OPENAI_MODEL', MODEL) != MODEL:
        raise RuntimeError('This MVP pins gpt-6-luna; model substitution is disabled')
    for name in ('DATABASE_URL', 'APP_PASSWORD', 'WORKER_TOKEN', 'APP_ORIGIN', 'OPENAI_API_KEY', 'CUA_CLIENT_ID', 'CUA_CLIENT_SECRET', 'FLEET_NAMESPACE', 'FLEET_SANDBOX'):
        if not os.environ.get(name): raise RuntimeError(f'Missing required setting: {name}')
    database_pool = AsyncConnectionPool(
        os.environ['DATABASE_URL'], open=False, min_size=2, max_size=8,
        timeout=5, kwargs={'row_factory': dict_row, 'connect_timeout': 5},
        name='ctrlloop',
    )
    try:
        await database_pool.open(wait=True, timeout=15)
        async with database() as db:
            await db.execute((ROOT / 'src/schema.sql').read_text())
            await db.execute('INSERT INTO workers(id) VALUES(%s) ON CONFLICT DO NOTHING', (WORKER,))
            # Deploy while generation is idle; this single-process task cannot resume.
            await db.execute("UPDATE loops SET status='error',error='Generation interrupted by server restart; create a new loop.' WHERE status='generating'")
        yield
    finally:
        pending = list(generation_tasks)
        for task in pending: task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await database_pool.close()
        database_pool = None


app = FastAPI(title='CTRL Loop', lifespan=lifespan, docs_url=None, redoc_url=None)


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def openai_cost_estimate(usage):
    if not isinstance(usage, dict): return None
    input_tokens, output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
    cached = (usage.get('input_tokens_details') or {}).get('cached_tokens')
    cache_write = (usage.get('input_tokens_details') or {}).get('cache_write_tokens')
    # This app uses small, ordinary Responses requests. Do not apply the
    # standard estimate to an unfamiliar cache-write billing configuration.
    if cache_write != 0: return None
    if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens, cached)) or cached > input_tokens:
        return None
    return {**OPENAI_PRICING, 'input_tokens': input_tokens, 'cached_input_tokens': cached,
            'cache_write_tokens': cache_write, 'pricing_scope': 'standard short-context request, no cache writes',
            'output_tokens': output_tokens,
            'input_cost_usd': ((input_tokens-cached)*0.10+cached*0.01)/1_000_000,
            'output_cost_usd': output_tokens*0.50/1_000_000,
            'cost_usd': ((input_tokens-cached)*0.10+cached*0.01+output_tokens*0.50)/1_000_000}


async def session_exists(digest):
    async with database() as db:
        return await (await db.execute('SELECT 1 FROM sessions WHERE token_hash=%s AND expires_at>now()', (digest,))).fetchone() is not None


async def authorize(request):
    digest = token_hash(request.cookies.get('ctrl_session', ''))
    if not await session_exists(digest): raise HTTPException(401, 'Sign in to access this desktop')
    return digest


def worker_authorize(request):
    if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + os.environ['WORKER_TOKEN']):
        raise HTTPException(401, 'Invalid worker credential')


@app.middleware('http')
async def protect(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.url.path.startswith('/api/worker/'):
        if request.headers.get('origin') != os.environ['APP_ORIGIN']:
            return JSONResponse({'detail':'Same-origin request required'},status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'"
    return response


class Login(BaseModel):
    password: str = Field(max_length=500)


class Prompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)


class Mode(BaseModel):
    mode: str


class WorkerPoll(BaseModel):
    revision: str = Field(max_length=200)


class WorkerEvent(BaseModel):
    id: UUID
    stage: str = Field(max_length=80)
    message: str = Field(max_length=2000)
    data: dict = Field(default_factory=dict)


class WorkerResult(BaseModel):
    status: str
    result: dict = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2000)


@app.get('/healthz')
async def health():
    async with database() as db: await db.execute('SELECT 1')
    return {'status': 'ok'}


@app.post('/api/login')
async def login(body: Login, request: Request, response: Response):
    peer = request.client.host
    now = time.monotonic()
    recent = [stamp for stamp in login_attempts.get(peer, []) if now-stamp < 300]
    if len(recent) >= 10: raise HTTPException(429, 'Too many attempts; wait five minutes')
    login_attempts[peer] = recent + [now]
    if not hmac.compare_digest(body.password.encode('utf-8'), os.environ['APP_PASSWORD'].encode('utf-8')): raise HTTPException(401, 'Incorrect access key')
    raw = secrets.token_urlsafe(32)
    async with database() as db:
        await db.execute("INSERT INTO sessions VALUES(%s, now()+interval '12 hours')", (token_hash(raw),))
    response.set_cookie('ctrl_session', raw, secure=True, httponly=True, samesite='strict', max_age=43200)
    return {'authenticated': True}


@app.post('/api/logout')
async def logout(request: Request, response: Response):
    digest = await authorize(request)
    async with database() as db:
        await db.execute('DELETE FROM sessions WHERE token_hash=%s', (digest,))
        await db.execute("UPDATE workers SET mode='agent',human_session=NULL WHERE human_session=%s", (digest,))
    response.delete_cookie('ctrl_session')
    return {'authenticated': False}


@app.get('/api/session')
async def session(request: Request):
    try: digest = await authorize(request)
    except HTTPException: return {'authenticated': False}
    async with database() as db:
        await db.execute("UPDATE workers SET mode='agent',human_session=NULL WHERE id=%s AND mode='human' AND NOT EXISTS (SELECT 1 FROM sessions WHERE token_hash=workers.human_session AND expires_at>now())", (WORKER,))
        worker = await (await db.execute('SELECT * FROM workers WHERE id=%s', (WORKER,))).fetchone()
    worker['can_control'] = worker.pop('human_session') == digest and worker['mode'] == 'human'
    age = (datetime.now(timezone.utc)-worker['last_seen']).total_seconds() if worker['last_seen'] else 1e9
    worker['status'] = 'online' if age < 30 else 'offline'
    return {'authenticated': True, 'worker': worker, 'model': MODEL, 'reasoning_effort': 'high', 'jev_model':'jev-1.13.0'}


@app.post('/api/worker/mode')
async def change_mode(body: Mode, request: Request):
    digest = await authorize(request)
    if request.headers.get('origin') != os.environ['APP_ORIGIN']: raise HTTPException(403, 'Same-origin request required')
    if body.mode not in ('agent', 'human'): raise HTTPException(400, 'Invalid mode')
    async with database() as db:
        await db.execute("UPDATE workers SET mode='agent',human_session=NULL WHERE id=%s AND mode='human' AND NOT EXISTS (SELECT 1 FROM sessions WHERE token_hash=workers.human_session AND expires_at>now())", (WORKER,))
        worker = await (await db.execute('SELECT * FROM workers WHERE id=%s FOR UPDATE', (WORKER,))).fetchone()
        active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running')", (WORKER,))).fetchone()
        if active: raise HTTPException(409, 'Wait for the active run to finish before changing control')
        if worker['mode'] == 'human' and worker['human_session'] != digest: raise HTTPException(409, 'Another signed-in session owns the desktop')
        await db.execute('UPDATE workers SET mode=%s,human_session=%s WHERE id=%s', (body.mode, digest if body.mode=='human' else None, WORKER))
    return {'mode': body.mode}


@app.get('/api/loops')
async def list_loops(request: Request):
    await authorize(request)
    async with database() as db:
        return await (await db.execute('SELECT * FROM loops ORDER BY created_at DESC LIMIT 100')).fetchall()


@app.post('/api/loops')
async def create_loop(body: Prompt, request: Request):
    await authorize(request)
    prompt = body.prompt.strip()
    if not prompt: raise HTTPException(400, 'Enter a request')
    identity = uuid4()
    async with database() as db:
        busy = await (await db.execute("SELECT count(*) AS count FROM loops WHERE status='generating'")).fetchone()
        if busy['count'] >= 3: raise HTTPException(429, 'Generation is busy; try again shortly')
        row = await (await db.execute('INSERT INTO loops(id,prompt,model) VALUES(%s,%s,%s) RETURNING *', (identity,prompt,MODEL))).fetchone()
        await event(db, identity, 'user_request', prompt)
        await event(db, identity, 'llm_generation', 'Generating an instruction with gpt-6-luna · high reasoning')
    task = asyncio.create_task(generate_instruction(identity, prompt)); generation_tasks.add(task); task.add_done_callback(generation_tasks.discard)
    return row


async def generate_instruction(identity, prompt):
    started = time.monotonic()
    payload = {'model': MODEL, 'reasoning': {'effort': 'high'}, 'store': False,
               'instructions': 'Restate the user request as one concise computer instruction, preserving their intended application and task. Output only the instruction. Do not invent task data, credentials, destinations, additional steps, or claims of execution. The executor can select visible windows and controls and attempt click or type actions, but does not yet plan arbitrary tasks, generate typing content, or assess general task completion. Preserve the request faithfully even when those capabilities may be needed; do not redirect it to a particular website.',
               'input': prompt, 'max_output_tokens': 500}
    raw_request = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    try:
        async with database() as db:
            await event(db, identity, 'llm_request', 'Exact instruction-model request captured before dispatch',
                        data={'provider': 'openai', 'method': 'POST', 'url': 'https://api.openai.com/v1/responses',
                              'body': payload, 'body_text': raw_request, 'clock': 'server',
                              'headers_excluded': True})
        async with httpx2.AsyncClient(timeout=120) as client:
            dispatched = time.monotonic()
            response = await client.post('https://api.openai.com/v1/responses',
                headers={'Authorization': 'Bearer '+os.environ['OPENAI_API_KEY'], 'Content-Type': 'application/json'},
                content=raw_request.encode('utf-8'))
            returned = time.monotonic()
        try: response_body = response.json()
        except ValueError: response_body = None
        usage = response_body.get('usage') if isinstance(response_body, dict) else None
        estimated_cost = openai_cost_estimate(usage)
        async with database() as db:
            await event(db, identity, 'llm_response', 'Instruction-model response captured',
                        data={'provider': 'openai', 'status_code': response.status_code,
                              'body': response_body, 'body_text': response.text, 'clock': 'server',
                              'duration_ms': round((returned-dispatched)*1000, 3),
                              'generation_elapsed_ms': round((returned-started)*1000, 3),
                              'usage': usage, 'cost_usd': estimated_cost['cost_usd'] if estimated_cost else None,
                              'cost_estimate': estimated_cost,
                              'cost_status': 'Usage estimate from published pricing; not an invoice' if estimated_cost else 'Usage or cached-token breakdown unavailable; cost is unknown'})
        response.raise_for_status(); result = response.json()
        instruction = ''.join(part.get('text','') for item in result.get('output',[]) if item.get('type')=='message' for part in item.get('content',[]) if part.get('type')=='output_text').strip()
        if result.get('status') != 'completed' or not instruction: raise ValueError('Model did not return a complete instruction')
        async with database() as db:
            await db.execute("UPDATE loops SET instruction=%s,status='ready',generated_at=now(),llm_response_id=%s,llm_usage=%s WHERE id=%s", (instruction,result.get('id'),Jsonb(result.get('usage',{})),identity))
            await event(db,identity,'llm_generated',instruction,data={'model':result.get('model'), 'reasoning_effort':'high','duration_ms':round((time.monotonic()-started)*1000), 'response_id':result.get('id'),'usage':result.get('usage')})
            await event(db,identity,'db_saved','Generated instruction committed to PostgreSQL')
    except asyncio.CancelledError:
        async with database() as db:
            await db.execute("UPDATE loops SET status='error',error='Generation interrupted; inspect available request evidence.' WHERE id=%s", (identity,))
            await event(db, identity, 'error', 'Generation interrupted; response capture may be unavailable',
                        data={'error_type': 'CancelledError', 'duration_ms': round((time.monotonic()-started)*1000, 3)})
        raise
    except Exception as error:
        message = f'LLM generation failed ({type(error).__name__}); no instruction was fabricated.'
        async with database() as db:
            await db.execute("UPDATE loops SET status='error',error=%s WHERE id=%s", (message,identity))
            await event(db,identity,'error',message,data={'error_type':type(error).__name__, 'duration_ms':round((time.monotonic()-started)*1000, 3)})


@app.get('/api/loops/{identity}')
async def loop_detail(identity: UUID, request: Request):
    await authorize(request)
    async with database() as db:
        row = await (await db.execute('SELECT * FROM loops WHERE id=%s',(identity,))).fetchone()
        if not row: raise HTTPException(404,'Loop not found')
        row['events'] = await (await db.execute('SELECT * FROM events WHERE loop_id=%s AND run_id IS NULL ORDER BY created_at',(identity,))).fetchall()
        row['runs'] = await (await db.execute('SELECT * FROM runs WHERE loop_id=%s ORDER BY created_at DESC',(identity,))).fetchall()
        for run in row['runs']:
            run['events'] = await (await db.execute('SELECT * FROM events WHERE run_id=%s ORDER BY created_at',(run['id'],))).fetchall()
        return row


@app.post('/api/loops/{identity}/runs')
async def create_run(identity: UUID, request: Request):
    await authorize(request)
    async with database() as db:
        worker = await (await db.execute('SELECT * FROM workers WHERE id=%s FOR UPDATE',(WORKER,))).fetchone()
        if worker['mode'] != 'agent': raise HTTPException(409,'Return desktop control to the worker first')
        if not worker['last_seen'] or (datetime.now(timezone.utc)-worker['last_seen']).total_seconds()>30: raise HTTPException(409,'Worker is offline')
        active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running')",(WORKER,))).fetchone()
        if active: raise HTTPException(409,'The desktop already has an active run')
        loop = await (await db.execute('SELECT * FROM loops WHERE id=%s',(identity,))).fetchone()
        if not loop or loop['status'] != 'ready': raise HTTPException(409,'A saved instruction is required')
        run = await (await db.execute('INSERT INTO runs(id,loop_id,worker_id) VALUES(%s,%s,%s) RETURNING *',(uuid4(),identity,WORKER))).fetchone()
        await event(db,identity,'delivery_queued','Saved instruction queued for its assigned Omarchy worker',run_id=run['id'])
        return run


@app.post('/api/worker/poll')
async def worker_poll(body: WorkerPoll, request: Request):
    worker_authorize(request)
    async with database() as db:
        worker = await (await db.execute('SELECT * FROM workers WHERE id=%s FOR UPDATE',(WORKER,))).fetchone()
        await db.execute('UPDATE workers SET last_seen=now(),revision=%s WHERE id=%s',(body.revision,WORKER))
        if worker['active_run'] or worker['mode'] != 'agent': return {'run':None}
        run = await (await db.execute("SELECT r.*,l.instruction FROM runs r JOIN loops l ON l.id=r.loop_id WHERE r.worker_id=%s AND r.status='queued' ORDER BY r.created_at LIMIT 1 FOR UPDATE OF r",(WORKER,))).fetchone()
        if not run: return {'run':None}
        await db.execute("UPDATE runs SET status='running',started_at=now(),revision=%s WHERE id=%s",(body.revision,run['id']))
        await db.execute('UPDATE workers SET active_run=%s WHERE id=%s',(run['id'],WORKER))
        await event(db,run['loop_id'],'delivery_sent','Persisted instruction dispatched to the assigned worker',run_id=run['id'],data={'revision':body.revision,'worker':WORKER})
        return {'run':run}


@app.post('/api/worker/heartbeat')
async def worker_heartbeat(request: Request):
    worker_authorize(request)
    async with database() as db: await db.execute('UPDATE workers SET last_seen=now() WHERE id=%s',(WORKER,))
    return {'ok':True}


@app.post('/api/worker/recover')
async def worker_recover(request: Request):
    """Called once after the worker acquires its guest-wide process lock."""
    worker_authorize(request)
    async with database() as db:
        await db.execute('SELECT id FROM workers WHERE id=%s FOR UPDATE',(WORKER,))
        rows=await (await db.execute("UPDATE runs SET status='interrupted',finished_at=now(),error='Worker restarted or delivery was interrupted. Actions were not replayed.' WHERE worker_id=%s AND status='running' RETURNING id,loop_id",(WORKER,))).fetchall()
        for row in rows:
            await event(db,row['loop_id'],'interrupted','Worker restarted; inspect the desktop before a new run. No actions replayed.',run_id=row['id'])
        await db.execute('UPDATE workers SET active_run=NULL,last_seen=now() WHERE id=%s',(WORKER,))
    return {'ok':True}


async def owned_run(db, identity):
    run = await (await db.execute("SELECT * FROM runs WHERE id=%s AND worker_id=%s AND status='running' FOR UPDATE",(identity,WORKER))).fetchone()
    if not run: raise HTTPException(409,'Run is not owned by this active worker')
    return run


@app.post('/api/worker/runs/{identity}/events')
async def worker_event(identity: UUID, body: WorkerEvent, request: Request):
    worker_authorize(request)
    async with database() as db:
        run = await owned_run(db,identity)
        await event(db,run['loop_id'],body.stage,body.message,run_id=identity,data=body.data,event_id=body.id)
    return {'ok':True}


@app.post('/api/worker/runs/{identity}/finish')
async def worker_finish(identity: UUID, body: WorkerResult, request: Request):
    worker_authorize(request)
    if body.status not in ('succeeded','error','blocked','interrupted','unverified'): raise HTTPException(400,'Invalid run status')
    if body.status=='succeeded' and body.result.get('verified') is not True: raise HTTPException(400,'Explicit task verification evidence is required')
    async with database() as db:
        run = await owned_run(db,identity)
        result = {**(run.get('result') or {}), **body.result}
        # Only the authenticated archive endpoint owns durable storage metadata.
        if (run.get('result') or {}).get('artifacts'): result['artifacts'] = run['result']['artifacts']
        await db.execute('UPDATE runs SET status=%s,finished_at=now(),error=%s,result=%s WHERE id=%s',(body.status,body.error,Jsonb(result),identity))
        await db.execute('UPDATE workers SET active_run=NULL,last_seen=now() WHERE id=%s',(WORKER,))
        await event(db,run['loop_id'],'observed_result' if body.status=='succeeded' else body.status,body.error or body.result.get('reason') or 'Worker finished; inspect the evidence and reported verification state.',run_id=identity,data=body.result)
    return {'ok':True}


@app.websocket('/api/desktop')
async def desktop(websocket: WebSocket):
    digest=token_hash(websocket.cookies.get('ctrl_session',''))
    if websocket.headers.get('origin') != os.environ['APP_ORIGIN'] or not await session_exists(digest):
        await websocket.close(code=1008);return
    @asynccontextmanager
    async def can_input():
        async with database() as db:
            # Keep the shared ownership lock until input is forwarded. A mode
            # change or worker claim cannot commit between the check and send.
            # One round trip on a pooled connection, only for actual input.
            # PostgreSQL remains authoritative even during overlapping deployments.
            row=await (await db.execute('''SELECT w.mode,w.human_session,w.active_run,
                EXISTS (SELECT 1 FROM sessions WHERE token_hash=%s AND expires_at>now()) AS valid
                FROM workers w WHERE w.id=%s FOR SHARE OF w''', (digest, WORKER))).fetchone()
            yield bool(row and row['valid'] and row['mode']=='human'
                       and row['human_session']==digest and row['active_run'] is None)
    try: await proxy_desktop(websocket,can_input,lambda:session_exists(digest))
    except Exception as error:
        # Record type only: upstream URLs/headers and session tokens are private.
        logger.warning('desktop connection setup failed error=%s', type(error).__name__)
        try: await websocket.close(code=1011,reason='Desktop connection unavailable; reconnect to retry')
        except RuntimeError: pass


@app.post('/api/worker/runs/{identity}/artifacts')
async def worker_artifacts(identity: UUID, request: Request):
    worker_authorize(request)
    async with database() as db:
        run = await (await db.execute('SELECT id FROM runs WHERE id=%s AND worker_id=%s', (identity, WORKER))).fetchone()
        if not run: raise HTTPException(404, 'Assigned run not found')
    async with artifact_slots:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_ARCHIVE_BYTES: raise HTTPException(413, 'Evidence archive exceeds 100 MiB')
        try: metadata = await asyncio.to_thread(inspect_archive, bytes(content), identity)
        except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
            raise HTTPException(400, 'Invalid run evidence archive') from error
        try: await asyncio.to_thread(store_archive, identity, bytes(content))
        except Exception as error:
            logger.warning('artifact upload failed error=%s', type(error).__name__)
            raise HTTPException(503, 'Evidence storage unavailable; retrying the upload is safe') from error
        metadata['uploaded_at'] = datetime.now(timezone.utc).isoformat()
        async with database() as db:
            # Recovery uploads are permitted after interruption; they never replay actions.
            run = await (await db.execute('SELECT * FROM runs WHERE id=%s AND worker_id=%s FOR UPDATE', (identity, WORKER))).fetchone()
            result = {**(run.get('result') or {}), 'artifacts': metadata}
            result.pop('artifact_upload_error', None)
            result['capture'] = {key: metadata['manifest'].get(key) for key in ('complete', 'missing_capture')}
            await db.execute('UPDATE runs SET result=%s WHERE id=%s', (Jsonb(result), identity))
            await event(db, run['loop_id'], 'artifacts_saved', 'Worker evidence stored privately', run_id=identity,
                        data={'sha256': metadata['sha256'], 'size_bytes': metadata['size_bytes'], 'file_count': len(metadata['files'])})
    return {'ok': True, 'sha256': metadata['sha256']}


async def report_record(identity, *, loop_only=False):
    async with database() as db:
        if loop_only:
            run = None
            loop_id = identity
        else:
            run = await (await db.execute('SELECT * FROM runs WHERE id=%s', (identity,))).fetchone()
            if not run: raise HTTPException(404, 'Run not found')
            loop_id = run['loop_id']
        loop = await (await db.execute('SELECT * FROM loops WHERE id=%s', (loop_id,))).fetchone()
        if not loop: raise HTTPException(404, 'Request not found')
        events = await (await db.execute('SELECT * FROM events WHERE loop_id=%s AND (run_id IS NULL OR run_id=%s) ORDER BY created_at,id', (loop_id, None if loop_only else identity))).fetchall()
    missing = []
    if not any(item['stage'] == 'llm_request' for item in events): missing.append('Exact OpenAI request was not captured for this earlier request.')
    if not any(item['stage'] == 'llm_response' for item in events): missing.append('OpenAI response is unavailable; generation may predate capture or have been interrupted.')
    artifact = ((run or {}).get('result') or {}).get('artifacts')
    if run and not artifact: missing.append('Worker evidence archive has not been uploaded. Only the available server record is included; native traces, Jev payloads and screenshots may be unavailable.')
    if artifact: missing.extend(artifact.get('manifest', {}).get('missing_capture', []))
    if not run: missing.append('No desktop run belongs to this request-only report.')
    return {'loop': loop, 'run': run, 'events': events, 'artifact': artifact,
            'openai_cost_estimate': openai_cost_estimate(loop.get('llm_usage')),
            'missing_capture': missing, 'worker_events': [],
            'timing_note': 'Server and worker timestamps are separate clock domains. Only durations measured on one machine are precise.',
            'cost_note': 'Jev input cost is a usage estimate at $0.042 per million input tokens; output is free. No provider invoice is attached.'}


async def report_content(record):
    if not record['artifact']: return None
    try: return await asyncio.to_thread(load_archive, record['run']['id'])
    except Exception as error:
        logger.warning('artifact read failed error=%s', type(error).__name__)
        record['missing_capture'].append('The stored worker archive could not be read at download time. Retry later; server evidence remains available.')
        return None


async def render_report_data(identity, request, *, loop_only=False, download=False):
    await authorize(request)
    async with artifact_slots:
        record = await report_record(identity, loop_only=loop_only)
        content = await report_content(record)
        if download:
            body = await asyncio.to_thread(evidence_bundle, record, content)
            return Response(body, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="ctrlloop-{identity}.zip"'})
        if content:
            try: record['worker_events'] = await asyncio.to_thread(worker_record, content)
            except (ValueError, zipfile.BadZipFile): record['missing_capture'].append('Worker timeline could not be decoded; original files remain in the ZIP.')
        return record


@app.get('/api/runs/{identity}/report')
async def run_report(identity: UUID, request: Request):
    return await render_report_data(identity, request)


@app.get('/api/runs/{identity}/download')
async def run_download(identity: UUID, request: Request):
    return await render_report_data(identity, request, download=True)


@app.get('/api/loops/{identity}/report')
async def request_report(identity: UUID, request: Request):
    return await render_report_data(identity, request, loop_only=True)


@app.get('/api/loops/{identity}/download')
async def request_download(identity: UUID, request: Request):
    return await render_report_data(identity, request, loop_only=True, download=True)


@app.get('/api/runs/{identity}/files/{path:path}')
async def artifact_file(identity: UUID, path: str, request: Request):
    await authorize(request)
    async with artifact_slots:
        record = await report_record(identity)
        allowed = {item['path'] for item in (record['artifact'] or {}).get('files', [])}
        if path not in allowed: raise HTTPException(404, 'Evidence file not found')
        content = await report_content(record)
        if not content: raise HTTPException(503, 'Evidence storage temporarily unavailable')
        with zipfile.ZipFile(io.BytesIO(content)) as archive: body = archive.read(path)
    image = path.lower().endswith('.png') and body.startswith(b'\x89PNG\r\n\x1a\n')
    name = Path(path).name.replace('"', '')
    return Response(body, media_type='image/png' if image else 'application/octet-stream',
                    headers={'Content-Disposition': f'{"inline" if image else "attachment"}; filename="{name}"'})


@app.get('/reports/{kind}/{identity}')
async def report_page(kind: str, identity: UUID):
    if kind not in ('runs', 'loops'): raise HTTPException(404, 'Report not found')
    return FileResponse(ROOT/'web/report.html')


@app.get('/')
async def index(): return FileResponse(ROOT/'web/index.html')

app.mount('/vendor',StaticFiles(directory=ROOT/'vendor',check_dir=False),name='vendor')
app.mount('/',StaticFiles(directory=ROOT/'web',check_dir=False),name='web')
