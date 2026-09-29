"""Workspace-scoped CTRL Loop API. PostgreSQL is the durable contract."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from .identity import (token_hash, password_hash, verify_password, normalize_username,
                       migrate_legacy_identity, create_member)
from .run_artifacts import (MAX_ARCHIVE_BYTES, inspect_archive, store_archive,
                            load_archive, worker_record, evidence_bundle)

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'gpt-6-luna'
OPENAI_PRICING = {'model': MODEL, 'input_per_million_usd': 0.10,
                  'cached_input_per_million_usd': 0.01, 'output_per_million_usd': 0.50,
                  'source': 'https://developers.openai.com/api/docs/pricing',
                  'checked_at': '2026-09-28', 'kind': 'usage_estimate_not_invoice'}
generation_tasks = set()
login_attempts = {}
dummy_password_hash = None
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
    global database_pool, dummy_password_hash
    if os.environ.get('OPENAI_MODEL', MODEL) != MODEL:
        raise RuntimeError('This MVP pins gpt-6-luna; model substitution is disabled')
    for name in ('DATABASE_URL', 'APP_ENV', 'APP_ORIGIN', 'OPENAI_API_KEY', 'CUA_CLIENT_ID', 'CUA_CLIENT_SECRET'):
        if not os.environ.get(name): raise RuntimeError(f'Missing required setting: {name}')
    if os.environ['APP_ENV'] not in ('dev', 'prod'): raise RuntimeError('APP_ENV must be dev or prod')
    dummy_password_hash = await asyncio.to_thread(password_hash, secrets.token_urlsafe(32))
    database_pool = AsyncConnectionPool(
        os.environ['DATABASE_URL'], open=False, min_size=2, max_size=8,
        timeout=5, kwargs={'row_factory': dict_row, 'connect_timeout': 5},
        name='ctrlloop',
    )
    try:
        await database_pool.open(wait=True, timeout=15)
        async with database() as db:
            await db.execute((ROOT / 'src/schema.sql').read_text())
            await migrate_legacy_identity(db)
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


async def session_identity(db, digest, *, lock=False, session_update=False):
    session = await (await db.execute('''SELECT user_id,workspace_id FROM sessions
        WHERE token_hash=%s AND expires_at>now()''', (digest,))).fetchone()
    if not session: return None
    # Membership -> session -> worker is the shared lock order. Recheck the
    # session after locking membership so a workspace switch cannot race lookup.
    identity = await (await db.execute('''SELECT m.user_id,m.workspace_id,u.username,u.display_name,
        m.role,w.slug AS workspace_slug,w.name AS workspace_name
        FROM memberships m JOIN users u ON u.id=m.user_id JOIN workspaces w ON w.id=m.workspace_id
        WHERE m.user_id=%s AND m.workspace_id=%s AND m.revoked_at IS NULL
        AND u.activated_at IS NOT NULL AND u.disabled_at IS NULL'''
        + (' FOR SHARE OF m,u' if lock else ''), (session['user_id'],session['workspace_id']))).fetchone()
    if not identity: return None
    current = await (await db.execute('''SELECT token_hash FROM sessions WHERE token_hash=%s
        AND user_id=%s AND workspace_id=%s AND expires_at>now()'''
        + (' FOR UPDATE' if session_update else ' FOR SHARE' if lock else ''),
        (digest,session['user_id'],session['workspace_id']))).fetchone()
    return {**identity, 'session_hash':digest} if current else None


async def authorize(request, db, *, owner=False, session_update=False):
    identity = await session_identity(db, token_hash(request.cookies.get('ctrl_session', '')), lock=True, session_update=session_update)
    if not identity: raise HTTPException(401, 'Sign in to access this workspace')
    if owner and identity['role'] != 'owner': raise HTTPException(403, 'Workspace owner access required')
    return identity


async def assigned_worker(db, identity, *, lock=True, required=True):
    worker = await (await db.execute('''SELECT * FROM workers WHERE workspace_id=%s
        AND assigned_user_id=%s AND disabled_at IS NULL''' + (' FOR UPDATE' if lock else ''),
        (identity['workspace_id'], identity['user_id']))).fetchone()
    if not worker and required: raise HTTPException(409, 'No personal desktop is assigned to your membership')
    return worker


async def worker_authorize(request, db):
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer ') or len(header) > 600: raise HTTPException(401, 'Invalid worker credential')
    digest = token_hash(header[7:])
    # Lock membership before worker, matching user and admin mutation order.
    # Pending users may provision/heartbeat their VM, but cannot sign in or run.
    candidate = await (await db.execute('''SELECT w.id,w.workspace_id,w.assigned_user_id FROM workers w
        JOIN memberships m ON m.workspace_id=w.workspace_id AND m.user_id=w.assigned_user_id
        JOIN users u ON u.id=m.user_id WHERE w.credential_hash=%s AND w.disabled_at IS NULL
        AND m.revoked_at IS NULL AND u.disabled_at IS NULL FOR SHARE OF m,u''', (digest,))).fetchone()
    if not candidate: raise HTTPException(401, 'Invalid worker credential or inactive assignment')
    worker = await (await db.execute('''SELECT * FROM workers WHERE id=%s AND credential_hash=%s
        AND workspace_id=%s AND assigned_user_id=%s AND disabled_at IS NULL FOR UPDATE''',
        (candidate['id'], digest, candidate['workspace_id'], candidate['assigned_user_id']))).fetchone()
    if not worker: raise HTTPException(401, 'Worker assignment changed')
    return worker


def public_worker(worker, digest=None):
    if not worker: return None
    age = (datetime.now(timezone.utc)-worker['last_seen']).total_seconds() if worker['last_seen'] else 1e9
    return {key: worker[key] for key in ('id', 'mode', 'last_seen', 'revision', 'active_run', 'fleet_namespace', 'fleet_claim')} | {
        'status': 'online' if age < 30 else 'offline',
        'can_control': worker['mode'] == 'human' and worker['human_session'] == digest}


async def release_expired_control(db, worker_id):
    await db.execute('''UPDATE workers SET mode='agent',human_session=NULL WHERE id=%s AND mode='human'
        AND NOT EXISTS (SELECT 1 FROM sessions s JOIN memberships m ON m.user_id=s.user_id
        AND m.workspace_id=s.workspace_id JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=workers.human_session AND s.expires_at>now() AND m.revoked_at IS NULL
        AND u.disabled_at IS NULL AND s.workspace_id=workers.workspace_id AND s.user_id=workers.assigned_user_id)''', (worker_id,))


def throttle(request, identity=''):
    now = time.monotonic()
    peer = request.client.host if request.client else 'unknown'
    keys = ('peer:' + peer, 'identity:' + token_hash(identity)) if identity else ('peer:' + peer,)
    for key in keys:
        recent = [stamp for stamp in login_attempts.get(key, []) if now-stamp < 300]
        if len(recent) >= 10: raise HTTPException(429, 'Too many attempts; wait five minutes')
        login_attempts[key] = recent + [now]
    if len(login_attempts) > 10000:
        for key in list(login_attempts):
            if not login_attempts[key] or now-login_attempts[key][-1] > 300: login_attempts.pop(key, None)


async def issue_session(db, response, user_id, workspace_id):
    raw = secrets.token_urlsafe(32)
    await db.execute("INSERT INTO sessions(token_hash,expires_at,user_id,workspace_id) VALUES(%s,now()+interval '12 hours',%s,%s)",
                     (token_hash(raw), user_id, workspace_id))
    response.set_cookie('ctrl_session', raw, secure=True, httponly=True, samesite='strict', max_age=43200)


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
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(max_length=500)


class ActivationToken(BaseModel):
    token: str = Field(min_length=32, max_length=500)


class Activation(ActivationToken):
    password: str = Field(min_length=12, max_length=500)


class WorkspaceSelection(BaseModel):
    workspace_id: UUID


class NewMember(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)


class MembershipUpdate(BaseModel):
    active: bool


class WorkerDeployment(BaseModel):
    phase: str
    expected_worker_id: str = Field(min_length=1, max_length=200)
    environment: str
    revision: str | None = Field(default=None, max_length=200)


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
    username = body.username.strip().lower()
    throttle(request, username)
    async with database() as db:
        user = await (await db.execute('SELECT * FROM users WHERE username=%s FOR SHARE', (username,))).fetchone()
        valid = await asyncio.to_thread(verify_password, body.password, (user or {}).get('password_hash') or dummy_password_hash)
        if not valid or not user or not user['activated_at'] or user['disabled_at']:
            raise HTTPException(401, 'Incorrect username or password')
        membership = await (await db.execute('''SELECT workspace_id FROM memberships WHERE user_id=%s
            AND revoked_at IS NULL ORDER BY created_at,workspace_id LIMIT 1 FOR SHARE''', (user['id'],))).fetchone()
        if not membership: raise HTTPException(403, 'No active workspace membership')
        await issue_session(db, response, user['id'], membership['workspace_id'])
    return {'authenticated': True}


async def activation_user(db, token, *, lock=False):
    user = await (await db.execute('''SELECT * FROM users WHERE activation_token_hash=%s
        AND activation_expires_at>now() AND activated_at IS NULL AND disabled_at IS NULL'''
        + (' FOR UPDATE' if lock else ''), (token_hash(token),))).fetchone()
    if not user: raise HTTPException(400, 'Activation link is invalid, expired or already used')
    memberships = await (await db.execute('''SELECT w.id,w.name FROM workspaces w JOIN memberships m
        ON m.workspace_id=w.id WHERE m.user_id=%s AND m.revoked_at IS NULL
        ORDER BY m.created_at,w.id FOR SHARE OF m''', (user['id'],))).fetchall()
    if not memberships: raise HTTPException(400, 'Activation link has no active workspace membership')
    return user, memberships


@app.post('/api/activate/inspect')
async def inspect_activation(body: ActivationToken, request: Request):
    throttle(request)
    async with database() as db:
        user, memberships = await activation_user(db, body.token)
        return {'user': {'username': user['username'], 'display_name': user['display_name']},
                'workspaces': memberships, 'expires_at': user['activation_expires_at']}


@app.post('/api/activate')
async def activate(body: Activation, request: Request, response: Response):
    throttle(request)
    async with database() as db:
        user, memberships = await activation_user(db, body.token, lock=True)
        encoded = await asyncio.to_thread(password_hash, body.password)
        await db.execute('''UPDATE users SET password_hash=%s,activated_at=now(),activation_token_hash=NULL,
            activation_expires_at=NULL WHERE id=%s''', (encoded, user['id']))
        await issue_session(db, response, user['id'], memberships[0]['id'])
    return {'authenticated': True}


@app.post('/api/logout')
async def logout(request: Request, response: Response):
    digest = token_hash(request.cookies.get('ctrl_session', ''))
    async with database() as db:
        # Logging out remains possible after a membership was revoked.
        await db.execute('DELETE FROM sessions WHERE token_hash=%s', (digest,))
        await db.execute("UPDATE workers SET mode='agent',human_session=NULL WHERE human_session=%s", (digest,))
    response.delete_cookie('ctrl_session')
    return {'authenticated': False}


@app.get('/api/session')
async def session(request: Request):
    async with database() as db:
        try: identity = await authorize(request, db)
        except HTTPException: return {'authenticated': False}
        worker = await assigned_worker(db, identity, required=False)
        if worker:
            await release_expired_control(db, worker['id'])
            worker = await assigned_worker(db, identity)
        memberships = await (await db.execute('''SELECT m.workspace_id,w.slug,w.name,m.role FROM memberships m
            JOIN workspaces w ON w.id=m.workspace_id WHERE m.user_id=%s AND m.revoked_at IS NULL
            ORDER BY w.name,w.id''', (identity['user_id'],))).fetchall()
        return {'authenticated': True,
                'user': {'id': identity['user_id'], 'username': identity['username'], 'display_name': identity['display_name']},
                'workspace': {'id': identity['workspace_id'], 'slug': identity['workspace_slug'],
                              'name': identity['workspace_name'], 'role': identity['role']},
                'memberships': memberships, 'worker': public_worker(worker, identity['session_hash']),
                'model': MODEL, 'reasoning_effort': 'high', 'jev_model': 'jev-1.13.0'}


@app.post('/api/session/workspace')
async def select_workspace(body: WorkspaceSelection, request: Request):
    async with database() as db:
        identity = await authorize(request, db, session_update=True)
        membership = await (await db.execute('''SELECT 1 FROM memberships WHERE user_id=%s AND workspace_id=%s
            AND revoked_at IS NULL FOR SHARE''', (identity['user_id'], body.workspace_id))).fetchone()
        if not membership: raise HTTPException(403, 'Active workspace membership required')
        await db.execute("UPDATE workers SET mode='agent',human_session=NULL WHERE human_session=%s", (identity['session_hash'],))
        await db.execute('UPDATE sessions SET workspace_id=%s WHERE token_hash=%s', (body.workspace_id, identity['session_hash']))
    return {'workspace_id': body.workspace_id}


@app.get('/api/memberships')
async def list_memberships(request: Request):
    async with database() as db:
        identity = await authorize(request, db, owner=True)
        rows = await (await db.execute('''SELECT u.id AS user_id,u.username,u.display_name,m.role,
            CASE WHEN m.revoked_at IS NOT NULL OR u.disabled_at IS NOT NULL THEN 'revoked'
                 WHEN u.activated_at IS NULL THEN 'pending' ELSE 'active' END AS status,
            w.id AS worker_id,w.last_seen,w.mode FROM memberships m JOIN users u ON u.id=m.user_id
            LEFT JOIN workers w ON w.workspace_id=m.workspace_id AND w.assigned_user_id=m.user_id AND w.disabled_at IS NULL
            WHERE m.workspace_id=%s ORDER BY u.display_name,u.username''', (identity['workspace_id'],))).fetchall()
        for row in rows:
            worker_id, seen, mode = row.pop('worker_id'), row.pop('last_seen'), row.pop('mode')
            row['worker'] = {'id': worker_id, 'mode': mode, 'status': 'online' if seen and (datetime.now(timezone.utc)-seen).total_seconds()<30 else 'offline'} if worker_id else None
        return {'members': rows}


@app.post('/api/memberships')
async def add_membership(body: NewMember, request: Request):
    async with database() as db:
        identity = await authorize(request, db, owner=True)
        try: return await create_member(db, identity['workspace_id'], body.username, body.display_name)
        except ValueError as error: raise HTTPException(409, str(error)) from error


@app.patch('/api/memberships/{user_id}')
async def update_membership(user_id: UUID, body: MembershipUpdate, request: Request):
    async with database() as db:
        identity = await authorize(request, db, owner=True)
        if user_id == identity['user_id']: raise HTTPException(409, 'You cannot change your own membership')
        membership = await (await db.execute('SELECT * FROM memberships WHERE workspace_id=%s AND user_id=%s FOR UPDATE',
            (identity['workspace_id'], user_id))).fetchone()
        if not membership: raise HTTPException(404, 'Membership not found')
        # Owner role changes are an explicit admin operation, not member controls.
        if membership['role'] == 'owner': raise HTTPException(409, 'Owner memberships require an administrator')
        await db.execute('UPDATE memberships SET revoked_at=' + ('NULL' if body.active else 'now()') + ' WHERE workspace_id=%s AND user_id=%s',
                         (identity['workspace_id'], user_id))
        if not body.active:
            await db.execute('DELETE FROM sessions WHERE workspace_id=%s AND user_id=%s', (identity['workspace_id'], user_id))
            await db.execute("UPDATE workers SET mode=CASE WHEN mode='human' THEN 'agent' ELSE mode END,human_session=NULL WHERE workspace_id=%s AND assigned_user_id=%s",
                             (identity['workspace_id'], user_id))
    return {'user_id': user_id, 'active': body.active}


@app.post('/api/worker/mode')
async def change_mode(body: Mode, request: Request):
    if request.headers.get('origin') != os.environ['APP_ORIGIN']: raise HTTPException(403, 'Same-origin request required')
    if body.mode not in ('agent', 'human'): raise HTTPException(400, 'Invalid mode')
    async with database() as db:
        identity = await authorize(request, db)
        digest = identity['session_hash']
        worker = await assigned_worker(db, identity)
        await release_expired_control(db, worker['id'])
        worker = await assigned_worker(db, identity)
        if worker['mode'] == 'maintenance': raise HTTPException(409, 'Desktop deployment is in progress')
        active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running')", (worker['id'],))).fetchone()
        if active: raise HTTPException(409, 'Wait for the active run to finish before changing control')
        if worker['mode'] == 'human' and worker['human_session'] != digest: raise HTTPException(409, 'Another signed-in session owns the desktop')
        await db.execute('UPDATE workers SET mode=%s,human_session=%s WHERE id=%s', (body.mode, digest if body.mode=='human' else None, worker['id']))
    return {'mode': body.mode}


@app.get('/api/loops')
async def list_loops(request: Request):
    async with database() as db:
        identity = await authorize(request, db)
        return await (await db.execute('''SELECT l.*,jsonb_build_object('id',u.id,'username',u.username,'display_name',u.display_name) AS creator
            FROM loops l LEFT JOIN users u ON u.id=l.created_by WHERE l.workspace_id=%s
            ORDER BY l.created_at DESC LIMIT 100''', (identity['workspace_id'],))).fetchall()


@app.post('/api/loops')
async def create_loop(body: Prompt, request: Request):
    prompt = body.prompt.strip()
    if not prompt: raise HTTPException(400, 'Enter a request')
    identity = uuid4()
    async with database() as db:
        principal = await authorize(request, db)
        busy = await (await db.execute("SELECT count(*) AS count FROM loops WHERE workspace_id=%s AND status='generating'", (principal['workspace_id'],))).fetchone()
        if busy['count'] >= 3: raise HTTPException(429, 'Generation is busy; try again shortly')
        row = await (await db.execute('INSERT INTO loops(id,prompt,model,workspace_id,created_by) VALUES(%s,%s,%s,%s,%s) RETURNING *',
            (identity,prompt,MODEL,principal['workspace_id'],principal['user_id']))).fetchone()
        row['creator'] = {key: principal[key] for key in ('username', 'display_name')} | {'id': principal['user_id']}
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
    async with database() as db:
        principal = await authorize(request, db)
        row = await (await db.execute('''SELECT l.*,jsonb_build_object('id',u.id,'username',u.username,'display_name',u.display_name) AS creator
            FROM loops l LEFT JOIN users u ON u.id=l.created_by WHERE l.id=%s AND l.workspace_id=%s''', (identity,principal['workspace_id']))).fetchone()
        if not row: raise HTTPException(404,'Loop not found')
        row['events'] = await (await db.execute('SELECT * FROM events WHERE loop_id=%s AND run_id IS NULL ORDER BY created_at',(identity,))).fetchall()
        row['runs'] = await (await db.execute('''SELECT r.*,jsonb_build_object('id',u.id,'username',u.username,'display_name',u.display_name) AS initiator
            FROM runs r LEFT JOIN users u ON u.id=r.initiated_by JOIN workers w ON w.id=r.worker_id
            WHERE r.loop_id=%s AND w.workspace_id=%s ORDER BY r.created_at DESC''', (identity,principal['workspace_id']))).fetchall()
        for run in row['runs']:
            run['events'] = await (await db.execute('SELECT * FROM events WHERE run_id=%s ORDER BY created_at',(run['id'],))).fetchall()
        return row


@app.post('/api/loops/{identity}/runs')
async def create_run(identity: UUID, request: Request):
    async with database() as db:
        principal = await authorize(request, db)
        worker = await assigned_worker(db, principal)
        if worker['mode'] != 'agent': raise HTTPException(409,'Return desktop control to the worker first')
        if not worker['last_seen'] or (datetime.now(timezone.utc)-worker['last_seen']).total_seconds()>30: raise HTTPException(409,'Worker is offline')
        active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running')",(worker['id'],))).fetchone()
        if active: raise HTTPException(409,'The desktop already has an active run')
        loop = await (await db.execute('SELECT * FROM loops WHERE id=%s AND workspace_id=%s',(identity,principal['workspace_id']))).fetchone()
        if not loop or loop['status'] != 'ready': raise HTTPException(409,'A saved instruction is required')
        run = await (await db.execute('INSERT INTO runs(id,loop_id,worker_id,initiated_by) VALUES(%s,%s,%s,%s) RETURNING *',
            (uuid4(),identity,worker['id'],principal['user_id']))).fetchone()
        run['initiator'] = {key: principal[key] for key in ('username', 'display_name')} | {'id': principal['user_id']}
        await event(db,identity,'delivery_queued','Saved instruction queued for its assigned Omarchy worker',run_id=run['id'])
        return run


@app.post('/api/worker/poll')
async def worker_poll(body: WorkerPoll, request: Request):
    async with database() as db:
        worker = await worker_authorize(request, db)
        await release_expired_control(db, worker['id'])
        worker = await (await db.execute('UPDATE workers SET last_seen=now(),revision=%s WHERE id=%s RETURNING *',(body.revision,worker['id']))).fetchone()
        if worker['active_run'] or worker['mode'] != 'agent': return {'run':None}
        run = await (await db.execute("SELECT r.*,l.instruction FROM runs r JOIN loops l ON l.id=r.loop_id WHERE r.worker_id=%s AND r.initiated_by=%s AND l.workspace_id=%s AND r.status='queued' ORDER BY r.created_at LIMIT 1 FOR UPDATE OF r",(worker['id'],worker['assigned_user_id'],worker['workspace_id']))).fetchone()
        if not run: return {'run':None}
        await db.execute("UPDATE runs SET status='running',started_at=now(),revision=%s WHERE id=%s",(body.revision,run['id']))
        await db.execute('UPDATE workers SET active_run=%s WHERE id=%s',(run['id'],worker['id']))
        await event(db,run['loop_id'],'delivery_sent','Persisted instruction dispatched to the assigned worker',run_id=run['id'],data={'revision':body.revision,'worker':worker['id']})
        return {'run':run}


@app.post('/api/worker/heartbeat')
async def worker_heartbeat(request: Request):
    async with database() as db:
        worker = await worker_authorize(request, db)
        await db.execute('UPDATE workers SET last_seen=now() WHERE id=%s',(worker['id'],))
    return {'ok':True, 'worker_id':worker['id'], 'environment':os.environ['APP_ENV']}


@app.post('/api/worker/deployment')
async def worker_deployment(body: WorkerDeployment, request: Request):
    if body.phase not in ('begin', 'finish'): raise HTTPException(400, 'Invalid deployment phase')
    async with database() as db:
        worker = await worker_authorize(request, db)
        if body.expected_worker_id != worker['id'] or body.environment != os.environ['APP_ENV']:
            raise HTTPException(409, 'Deployment worker or environment does not match the credential')
        active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running')", (worker['id'],))).fetchone()
        if active or worker['active_run'] or worker['mode'] == 'human':
            raise HTTPException(409, 'Desktop must be idle under agent control before deployment')
        if body.phase == 'finish' and worker['mode'] != 'maintenance': raise HTTPException(409, 'Desktop is not in maintenance')
        mode = 'maintenance' if body.phase == 'begin' else 'agent'
        await db.execute('UPDATE workers SET mode=%s,human_session=NULL,revision=COALESCE(%s,revision) WHERE id=%s',
                         (mode, body.revision if body.phase == 'finish' else None, worker['id']))
    return {'worker_id': worker['id'], 'environment': os.environ['APP_ENV'], 'mode': mode}


@app.post('/api/worker/recover')
async def worker_recover(request: Request):
    """Called once after the worker acquires its guest-wide process lock."""
    async with database() as db:
        worker = await worker_authorize(request, db)
        rows=await (await db.execute("""UPDATE runs r SET status='interrupted',finished_at=now(),
            error='Worker restarted or delivery was interrupted. Actions were not replayed.' FROM loops l
            WHERE r.loop_id=l.id AND r.worker_id=%s AND r.initiated_by=%s AND l.workspace_id=%s
            AND r.status='running' RETURNING r.id,r.loop_id""",
            (worker['id'],worker['assigned_user_id'],worker['workspace_id']))).fetchall()
        for row in rows:
            await event(db,row['loop_id'],'interrupted','Worker restarted; inspect the desktop before a new run. No actions replayed.',run_id=row['id'])
        await db.execute('UPDATE workers SET active_run=NULL,last_seen=now() WHERE id=%s',(worker['id'],))
    return {'ok':True}


async def owned_run(db, identity, worker, *, active=True):
    run = await (await db.execute('''SELECT r.* FROM runs r JOIN loops l ON l.id=r.loop_id
        WHERE r.id=%s AND r.worker_id=%s AND r.initiated_by=%s AND l.workspace_id=%s'''
        + (" AND r.status='running'" if active else '') + ' FOR UPDATE OF r',
        (identity,worker['id'],worker['assigned_user_id'],worker['workspace_id']))).fetchone()
    if not run: raise HTTPException(409,'Run is not owned by this active worker')
    return run


@app.post('/api/worker/runs/{identity}/events')
async def worker_event(identity: UUID, body: WorkerEvent, request: Request):
    async with database() as db:
        worker = await worker_authorize(request, db)
        run = await owned_run(db,identity,worker)
        await event(db,run['loop_id'],body.stage,body.message,run_id=identity,data=body.data,event_id=body.id)
    return {'ok':True}


@app.post('/api/worker/runs/{identity}/finish')
async def worker_finish(identity: UUID, body: WorkerResult, request: Request):
    if body.status not in ('succeeded','error','blocked','interrupted','unverified'): raise HTTPException(400,'Invalid run status')
    if body.status=='succeeded' and body.result.get('verified') is not True: raise HTTPException(400,'Explicit task verification evidence is required')
    async with database() as db:
        worker = await worker_authorize(request, db)
        run = await owned_run(db,identity,worker)
        result = {**(run.get('result') or {}), **body.result}
        # Only the authenticated archive endpoint owns durable storage metadata.
        if (run.get('result') or {}).get('artifacts'): result['artifacts'] = run['result']['artifacts']
        else: result.pop('artifacts', None)
        await db.execute('UPDATE runs SET status=%s,finished_at=now(),error=%s,result=%s WHERE id=%s',(body.status,body.error,Jsonb(result),identity))
        await db.execute('UPDATE workers SET active_run=NULL,last_seen=now() WHERE id=%s',(worker['id'],))
        await event(db,run['loop_id'],'observed_result' if body.status=='succeeded' else body.status,body.error or body.result.get('reason') or 'Worker finished; inspect the evidence and reported verification state.',run_id=identity,data=body.result)
    return {'ok':True}


@app.websocket('/api/desktop')
async def desktop(websocket: WebSocket):
    digest=token_hash(websocket.cookies.get('ctrl_session',''))
    if websocket.headers.get('origin') != os.environ['APP_ORIGIN']:
        await websocket.close(code=1008);return
    async with database() as db:
        identity = await session_identity(db, digest, lock=True)
        worker = await assigned_worker(db, identity, required=False) if identity else None
        if not worker or not worker['fleet_namespace'] or not worker['fleet_claim']:
            await websocket.close(code=1008);return
    # A socket is bound to this identity, workspace and target for its lifetime.
    # A workspace switch, reassignment or revocation cannot redirect the socket.
    async def connection_worker(db):
        current = await session_identity(db, digest, lock=True)
        if not current or current['user_id'] != identity['user_id'] or current['workspace_id'] != identity['workspace_id']:
            return None
        return await (await db.execute('''SELECT * FROM workers WHERE id=%s AND workspace_id=%s
            AND assigned_user_id=%s AND fleet_namespace=%s AND fleet_claim=%s AND disabled_at IS NULL FOR SHARE''',
            (worker['id'],identity['workspace_id'],identity['user_id'],worker['fleet_namespace'],worker['fleet_claim']))).fetchone()

    async def session_valid():
        async with database() as db: return await connection_worker(db) is not None

    @asynccontextmanager
    async def can_input():
        async with database() as db:
            # Keep the shared ownership lock until input is forwarded. A mode
            # change or worker claim cannot commit between the check and send.
            # One round trip on a pooled connection, only for actual input.
            # PostgreSQL remains authoritative even during overlapping deployments.
            row = await connection_worker(db)
            if row is None: raise PermissionError('Desktop assignment is no longer authorized')
            yield bool(row['mode']=='human'
                       and row['human_session']==digest and row['active_run'] is None)
    try: await proxy_desktop(websocket,can_input,session_valid,namespace=worker['fleet_namespace'],sandbox=worker['id'])
    except Exception as error:
        # Record type only: upstream URLs/headers and session tokens are private.
        logger.warning('desktop connection setup failed error=%s', type(error).__name__)
        try: await websocket.close(code=1011,reason='Desktop connection unavailable; reconnect to retry')
        except RuntimeError: pass


@app.post('/api/worker/runs/{identity}/artifacts')
async def worker_artifacts(identity: UUID, request: Request):
    async with database() as db:
        worker = await worker_authorize(request, db)
        await owned_run(db, identity, worker, active=False)
    async with artifact_slots:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_ARCHIVE_BYTES: raise HTTPException(413, 'Evidence archive exceeds 100 MiB')
        try: metadata = await asyncio.to_thread(inspect_archive, bytes(content), identity)
        except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
            raise HTTPException(400, 'Invalid run evidence archive') from error
        async with database() as db:
            # Recovery uploads are permitted after interruption; they never replay actions.
            # Recheck after the upload body arrives and retain membership/assignment
            # locks through storage, so revocation cannot commit during this write.
            worker = await worker_authorize(request, db)
            run = await owned_run(db, identity, worker, active=False)
            try: await asyncio.to_thread(store_archive, identity, bytes(content))
            except Exception as error:
                logger.warning('artifact upload failed error=%s', type(error).__name__)
                raise HTTPException(503, 'Evidence storage unavailable; retrying the upload is safe') from error
            metadata['uploaded_at'] = datetime.now(timezone.utc).isoformat()
            result = {**(run.get('result') or {}), 'artifacts': metadata}
            result.pop('artifact_upload_error', None)
            result['capture'] = {key: metadata['manifest'].get(key) for key in ('complete', 'missing_capture')}
            await db.execute('UPDATE runs SET result=%s WHERE id=%s', (Jsonb(result), identity))
            await event(db, run['loop_id'], 'artifacts_saved', 'Worker evidence stored privately', run_id=identity,
                        data={'sha256': metadata['sha256'], 'size_bytes': metadata['size_bytes'], 'file_count': len(metadata['files'])})
    return {'ok': True, 'sha256': metadata['sha256']}


async def report_record(identity, request, *, loop_only=False):
    async with database() as db:
        principal = await authorize(request, db)
        if loop_only:
            run = None
            loop_id = identity
        else:
            run = await (await db.execute('''SELECT r.*,jsonb_build_object('id',u.id,'username',u.username,'display_name',u.display_name) AS initiator
                FROM runs r JOIN loops l ON l.id=r.loop_id JOIN workers w ON w.id=r.worker_id
                LEFT JOIN users u ON u.id=r.initiated_by WHERE r.id=%s AND l.workspace_id=%s AND w.workspace_id=%s''',
                (identity,principal['workspace_id'],principal['workspace_id']))).fetchone()
            if not run: raise HTTPException(404, 'Run not found')
            loop_id = run['loop_id']
        loop = await (await db.execute('''SELECT l.*,jsonb_build_object('id',u.id,'username',u.username,'display_name',u.display_name) AS creator
            FROM loops l LEFT JOIN users u ON u.id=l.created_by WHERE l.id=%s AND l.workspace_id=%s''',
            (loop_id,principal['workspace_id']))).fetchone()
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
    async with artifact_slots:
        record = await report_record(identity, request, loop_only=loop_only)
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
    async with artifact_slots:
        record = await report_record(identity, request)
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
