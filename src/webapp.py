"""Single-tenant CTRL Loop MVP API. PostgreSQL is the durable contract."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx2
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .desktop_gateway import proxy_desktop

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'gpt-6-luna'
WORKER = os.environ.get('WORKER_ID', 'ctrlloop-omarchy-20260927-1708-e33048ae')
generation_tasks = set()
login_attempts = {}


@asynccontextmanager
async def database():
    async with await psycopg.AsyncConnection.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as connection:
        yield connection


async def event(db, loop_id, stage, message, *, run_id=None, data=None, event_id=None):
    await db.execute('INSERT INTO events(id,loop_id,run_id,stage,message,data) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING',
                     (event_id or uuid4(), loop_id, run_id, stage, message, Jsonb(data or {})))


@asynccontextmanager
async def lifespan(app):
    if os.environ.get('OPENAI_MODEL', MODEL) != MODEL:
        raise RuntimeError('This MVP pins gpt-6-luna; model substitution is disabled')
    for name in ('DATABASE_URL', 'APP_PASSWORD', 'WORKER_TOKEN', 'APP_ORIGIN', 'OPENAI_API_KEY', 'CUA_CLIENT_ID', 'CUA_CLIENT_SECRET', 'FLEET_NAMESPACE', 'FLEET_SANDBOX'):
        if not os.environ.get(name): raise RuntimeError(f'Missing required setting: {name}')
    async with database() as db:
        await db.execute((ROOT / 'src/schema.sql').read_text())
        await db.execute('INSERT INTO workers(id) VALUES(%s) ON CONFLICT DO NOTHING', (WORKER,))
        # Interrupted generation is surfaced; never fabricate or silently regenerate.
        await db.execute("UPDATE loops SET status='error',error='Generation interrupted by server restart; create a new loop.' WHERE status='generating'")
    yield
    for task in generation_tasks: task.cancel()


app = FastAPI(title='CTRL Loop', lifespan=lifespan, docs_url=None, redoc_url=None)


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


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
    try:
        async with httpx2.AsyncClient(timeout=120) as client:
            response = await client.post('https://api.openai.com/v1/responses',
                headers={'Authorization': 'Bearer '+os.environ['OPENAI_API_KEY']},
                json={'model': MODEL, 'reasoning': {'effort': 'high'}, 'store': False,
                      'instructions': 'You produce one brief computer instruction for a tiny browser demo. Translate the user request into the first useful navigation step: opening its relevant website in Google Chrome. Output only that single instruction, including the HTTPS URL. Do not claim to have executed anything or read authenticated content. Do not produce shell commands, login actions, posting, purchases, or multiple steps. The current executor supports only opening https://x.com/; if the request requires a different website or action, explain that it is unsupported instead of redirecting it to X.',
                      'input': prompt, 'max_output_tokens': 500})
        response.raise_for_status(); result = response.json()
        instruction = ''.join(part.get('text','') for item in result.get('output',[]) if item.get('type')=='message' for part in item.get('content',[]) if part.get('type')=='output_text').strip()
        if result.get('status') != 'completed' or not instruction: raise ValueError('Model did not return a complete instruction')
        async with database() as db:
            await db.execute("UPDATE loops SET instruction=%s,status='ready',generated_at=now(),llm_response_id=%s,llm_usage=%s WHERE id=%s", (instruction,result.get('id'),Jsonb(result.get('usage',{})),identity))
            await event(db,identity,'llm_generated',instruction,data={'model':result.get('model'), 'reasoning_effort':'high','duration_ms':round((time.monotonic()-started)*1000), 'response_id':result.get('id'),'usage':result.get('usage')})
            await event(db,identity,'db_saved','Generated instruction committed to PostgreSQL')
    except asyncio.CancelledError: raise
    except Exception as error:
        message = f'LLM generation failed ({type(error).__name__}); no instruction was fabricated.'
        async with database() as db:
            await db.execute("UPDATE loops SET status='error',error=%s WHERE id=%s", (message,identity))
            await event(db,identity,'error',message)


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
    if body.status not in ('succeeded','error','blocked','interrupted'): raise HTTPException(400,'Invalid run status')
    if body.status=='succeeded' and not body.result.get('verified_url'): raise HTTPException(400,'Observed URL evidence is required')
    async with database() as db:
        run = await owned_run(db,identity)
        await db.execute('UPDATE runs SET status=%s,finished_at=now(),error=%s,result=%s WHERE id=%s',(body.status,body.error,Jsonb(body.result),identity))
        await db.execute('UPDATE workers SET active_run=NULL,last_seen=now() WHERE id=%s',(WORKER,))
        await event(db,run['loop_id'],'observed_result' if body.status=='succeeded' else body.status,body.error or 'Browser destination verified; no authenticated feed read is claimed.',run_id=identity,data=body.result)
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
            row=await (await db.execute('SELECT mode,human_session,active_run FROM workers WHERE id=%s FOR SHARE',(WORKER,))).fetchone()
            valid=await (await db.execute('SELECT 1 FROM sessions WHERE token_hash=%s AND expires_at>now()', (digest,))).fetchone()
            yield bool(valid) and row['mode']=='human' and row['human_session']==digest and row['active_run'] is None
    try: await proxy_desktop(websocket,can_input,lambda:session_exists(digest))
    except Exception:
        try: await websocket.close(code=1011,reason='Desktop connection unavailable; reconnect to retry')
        except RuntimeError: pass


@app.get('/')
async def index(): return FileResponse(ROOT/'web/index.html')

app.mount('/vendor',StaticFiles(directory=ROOT/'vendor',check_dir=False),name='vendor')
app.mount('/',StaticFiles(directory=ROOT/'web',check_dir=False),name='web')
