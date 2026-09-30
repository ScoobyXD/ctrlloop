"""Company-scoped CTRL Loop API. PostgreSQL is the durable contract."""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import io
import json
import logging
import math
import os
from pathlib import Path
import secrets
import time
from uuid import UUID, uuid4
import zipfile

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx2
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from fastapi.exceptions import RequestValidationError
from starlette.websockets import WebSocketDisconnect

from .desktop_gateway import desktop_connection
from .desktop_policy import DesktopPolicyNotifications
from .identity import token_hash, hash_password, check_password, normalize_email
from .company_migration import migrate_company_schema
from .account_mail import send_account_mail, delivery_mode
from .actions import normalize_action
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
desktop_policy_notifications = None
logger = logging.getLogger('uvicorn.error.desktop')
artifact_slots = asyncio.Semaphore(1)


@asynccontextmanager
async def database():
    if database_pool is None:
        raise RuntimeError('Database pool is not running')
    async with database_pool.connection() as connection:
        yield connection


async def event(db, loop_id, stage, message, *, run_id=None, data=None, event_id=None):
    await db.execute('''INSERT INTO events(id,company_id,loop_id,run_id,stage,message,data)
        SELECT %s,company_id,id,%s,%s,%s,%s FROM loops WHERE id=%s ON CONFLICT(id) DO NOTHING''',
        (event_id or uuid4(), run_id, stage, message, Jsonb(data or {}), loop_id))


@asynccontextmanager
async def lifespan(app):
    global database_pool, dummy_password_hash, desktop_policy_notifications
    if os.environ.get('OPENAI_MODEL', MODEL) != MODEL:
        raise RuntimeError('This MVP pins gpt-6-luna; model substitution is disabled')
    for name in ('DATABASE_URL', 'APP_ENV', 'APP_ORIGIN', 'OPENAI_API_KEY', 'CUA_CLIENT_ID', 'CUA_CLIENT_SECRET'):
        if not os.environ.get(name): raise RuntimeError(f'Missing required setting: {name}')
    if os.environ['APP_ENV'] not in ('dev', 'prod'): raise RuntimeError('APP_ENV must be dev or prod')
    dummy_password_hash = await hash_password(secrets.token_urlsafe(32))
    database_pool = AsyncConnectionPool(
        os.environ['DATABASE_URL'], open=False, min_size=2, max_size=8,
        timeout=5, kwargs={'row_factory': dict_row, 'connect_timeout': 5},
        name='ctrlloop',
    )
    try:
        await database_pool.open(wait=True, timeout=15)
        async with database() as db:
            await migrate_company_schema(db)
            # Neither unfinished generation nor undispatched execution is replayed.
            await interrupt_queued_runs(db, 'Server restarted before dispatch. Start again when ready.')
            await db.execute("UPDATE loops SET status='error',error='Generation interrupted by server restart; create a new loop.' WHERE status='generating'")
        desktop_policy_notifications = DesktopPolicyNotifications(os.environ['DATABASE_URL'])
        await desktop_policy_notifications.start()
        yield
    finally:
        if desktop_policy_notifications is not None:
            await desktop_policy_notifications.close()
            desktop_policy_notifications = None
        pending = list(generation_tasks)
        for task in pending: task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await database_pool.close()
        database_pool = None


app = FastAPI(title='CTRL Loop', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    # Pydantic's default response echoes rejected input, including credentials.
    fields = sorted({'.'.join(str(part) for part in item['loc'][1:]) for item in error.errors()})
    return JSONResponse({'detail': 'Check these fields: ' + ', '.join(fields)}, status_code=422)


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
    session = await (await db.execute('''SELECT user_id,company_id FROM sessions
        WHERE token_hash=%s AND expires_at>now()''', (digest,))).fetchone()
    if not session: return None
    identity = await (await db.execute('''SELECT u.id AS user_id,u.company_id,u.email,u.display_name,
        u.role,u.is_devadmin,c.slug AS company_slug,c.name AS company_name
        FROM users u JOIN companies c ON c.id=u.company_id
        WHERE u.id=%s AND u.company_id=%s AND u.email_verified_at IS NOT NULL
        AND u.disabled_at IS NULL''' + (' FOR SHARE OF u' if lock else ''),
        (session['user_id'],session['company_id']))).fetchone()
    if not identity: return None
    current = await (await db.execute('''SELECT token_hash FROM sessions WHERE token_hash=%s
        AND user_id=%s AND company_id=%s AND expires_at>now()'''
        + (' FOR UPDATE' if session_update else ' FOR SHARE' if lock else ''),
        (digest,session['user_id'],session['company_id']))).fetchone()
    return {**identity, 'session_hash': digest, 'home_company_id': identity['company_id'],
            'target_user_id': identity['user_id'], 'target_email': identity['email'],
            'target_display_name': identity['display_name'], 'target_verified': True} if current else None


async def target_context(db, identity, company_id=None, user_id=None):
    """Only a verified platform devadmin can supply another company/user context."""
    try:
        company_id = UUID(str(company_id)) if company_id else identity['home_company_id']
        user_id = UUID(str(user_id)) if user_id else (identity['user_id'] if company_id == identity['home_company_id'] else None)
    except ValueError as error:
        raise HTTPException(400, 'Invalid company or user selection') from error
    if not identity['is_devadmin'] and (company_id != identity['home_company_id'] or user_id != identity['user_id']):
        raise HTTPException(403, 'Platform devadmin access required for another company or user')
    company = await (await db.execute('SELECT id,slug,name FROM companies WHERE id=%s', (company_id,))).fetchone()
    if not company: raise HTTPException(404, 'Company not found')
    target = None
    if user_id:
        target = await (await db.execute('''SELECT id,email,display_name,email_verified_at FROM users
            WHERE id=%s AND company_id=%s AND disabled_at IS NULL FOR SHARE''', (user_id,company_id))).fetchone()
        if not target: raise HTTPException(404, 'Active company user not found')
    return {**identity, 'company_id': company_id, 'company_slug': company['slug'], 'company_name': company['name'],
            'target_user_id': user_id, 'target_email': target['email'] if target else None,
            'target_display_name': target['display_name'] if target else None,
            'target_verified': bool(target and target['email_verified_at'])}


async def authorize(request, db, *, owner=False, session_update=False):
    identity = await session_identity(db, token_hash(request.cookies.get('ctrl_session', '')), lock=True, session_update=session_update)
    if not identity: raise HTTPException(401, 'Sign in to access your company')
    identity = await target_context(db, identity,
        request.headers.get('x-company-id') or request.query_params.get('company_id'),
        request.headers.get('x-user-id') or request.query_params.get('user_id'))
    if owner and not identity['is_devadmin'] and identity['role'] != 'company_admin':
        raise HTTPException(403, 'Company admin access required')
    return identity


async def authorize_company_management(request, db):
    # Serialize company administration before taking user locks. Otherwise two
    # admins changing each other's access can hold each other's shared user lock.
    initial = await session_identity(db, token_hash(request.cookies.get('ctrl_session', '')))
    if not initial: raise HTTPException(401, 'Sign in to access your company')
    raw_company = request.headers.get('x-company-id') or request.query_params.get('company_id')
    try: company_id = UUID(raw_company) if raw_company else initial['home_company_id']
    except ValueError as error: raise HTTPException(400, 'Invalid company selection') from error
    if not initial['is_devadmin'] and company_id != initial['home_company_id']:
        raise HTTPException(403, 'Platform devadmin access required')
    # A global admin-write lock also covers devadmins managing each other's
    # home companies; these rare operations must share one lock order.
    await db.execute("SELECT pg_advisory_xact_lock(hashtext('ctrlloop.account.management'))")
    return await authorize(request, db, owner=True)


async def lock_session_owner(db, digest):
    session = await (await db.execute('SELECT user_id FROM sessions WHERE token_hash=%s', (digest,))).fetchone()
    if session:
        # Includes disabled/unverified/expired accounts so logout follows the
        # same user -> session -> worker order as reset and ordinary access.
        await db.execute('SELECT id FROM users WHERE id=%s FOR SHARE', (session['user_id'],))
    await db.execute('SELECT token_hash FROM sessions WHERE token_hash=%s FOR UPDATE', (digest,))


def acting_user(identity):
    if not identity['target_user_id']:
        raise HTTPException(409, 'Select a company user before creating or running a CtrlLoop')
    return identity['target_user_id']


async def assigned_worker(db, identity, *, lock=True, required=True):
    worker = await (await db.execute('''SELECT * FROM workers WHERE company_id=%s
        AND assigned_user_id=%s AND disabled_at IS NULL''' + (' FOR UPDATE' if lock else ''),
        (identity['company_id'], identity['target_user_id']))).fetchone()
    if not worker and required: raise HTTPException(409, 'No personal desktop is assigned to the selected company user')
    return worker


async def worker_authorize(request, db, *, lock=True):
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer ') or len(header) > 600: raise HTTPException(401, 'Invalid worker credential')
    digest = token_hash(header[7:])
    candidate = await (await db.execute('''SELECT w.id,w.company_id,w.assigned_user_id FROM workers w
        JOIN users u ON u.company_id=w.company_id AND u.id=w.assigned_user_id
        WHERE w.credential_hash=%s AND w.disabled_at IS NULL AND u.disabled_at IS NULL
        FOR SHARE OF u''', (digest,))).fetchone()
    if not candidate: raise HTTPException(401, 'Invalid worker credential or inactive assignment')
    worker = await (await db.execute('''SELECT * FROM workers WHERE id=%s AND credential_hash=%s
        AND company_id=%s AND assigned_user_id=%s AND disabled_at IS NULL''' + (' FOR UPDATE' if lock else ''),
        (candidate['id'], digest, candidate['company_id'], candidate['assigned_user_id']))).fetchone()
    if not worker: raise HTTPException(401, 'Worker assignment changed')
    return worker


def public_worker(worker, digest=None, *, queued_run=None):
    if not worker: return None
    age = (datetime.now(timezone.utc)-worker['last_seen']).total_seconds() if worker['last_seen'] else 1e9
    return {key: worker[key] for key in ('id', 'mode', 'last_seen', 'revision', 'active_run', 'fleet_namespace', 'fleet_claim')} | {
        'queued_run': queued_run,
        'status': 'online' if age < 30 else 'offline',
        'can_control': worker['mode'] == 'human' and worker['human_session'] == digest}


async def release_expired_control(db, worker_id):
    await db.execute("""UPDATE workers SET mode='agent',human_session=NULL WHERE id=%s AND mode='human'
        AND NOT EXISTS (SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id AND u.company_id=s.company_id
        WHERE s.token_hash=workers.human_session AND s.expires_at>now()
        AND u.disabled_at IS NULL AND u.email_verified_at IS NOT NULL
        AND (u.is_devadmin OR (s.company_id=workers.company_id AND s.user_id=workers.assigned_user_id)))""", (worker_id,))


async def interrupt_queued_runs(db, message, *, clause='TRUE', params=()):
    rows = await (await db.execute("""UPDATE runs SET status='interrupted',finished_at=now(),
        error=%s,request_session_hash=NULL WHERE status='queued' AND """ + clause +
        ' RETURNING id,loop_id', (message, *params))).fetchall()
    for row in rows:
        await event(db, row['loop_id'], 'interrupted', message, run_id=row['id'])


async def release_expired_queue(db, worker_id):
    # The session belongs to the real actor. A devadmin may act for a verified
    # user in another company, but losing that privilege cancels pending work.
    await interrupt_queued_runs(db, 'Start expired or authorization is no longer valid. Start again when ready.',
        clause="""worker_id=%s AND (dispatch_before IS NULL OR dispatch_before<=now()
            OR NOT EXISTS (SELECT 1 FROM sessions s
                JOIN users actor ON actor.id=s.user_id AND actor.company_id=s.company_id
                JOIN workers w ON w.id=runs.worker_id AND w.company_id=runs.company_id
                JOIN users target ON target.id=runs.initiated_by AND target.company_id=runs.company_id
                WHERE s.token_hash=runs.request_session_hash AND s.expires_at>now()
                AND actor.disabled_at IS NULL AND actor.email_verified_at IS NOT NULL
                AND target.disabled_at IS NULL AND target.email_verified_at IS NOT NULL
                AND w.assigned_user_id=target.id AND w.disabled_at IS NULL
                AND (actor.is_devadmin OR (actor.id=target.id AND actor.company_id=runs.company_id))))""",
        params=(worker_id,))


def public_run(run):
    return {key: value for key, value in run.items()
            if key not in ('request_session_hash', 'dispatch_before', 'loop_status')}


def run_step_end(run):
    """Older manual runs execute through the end of their immutable snapshot."""
    return run['end_step'] if run.get('end_step') is not None else len(run['steps'])


async def queue_run(db, loop, principal, *, mode='sequence', start_step=0, request_id=None):
    # Both create-and-start and explicit reruns use the same reservation checks.
    if not principal['target_verified']:
        raise HTTPException(409, 'The selected user must verify their email before running a CtrlLoop')
    worker = await assigned_worker(db, principal)
    if request_id:
        previous = await (await db.execute('''SELECT * FROM runs WHERE company_id=%s
            AND initiated_by=%s AND request_id=%s''',
            (principal['company_id'], acting_user(principal), request_id))).fetchone()
        if previous:
            requested_end = (start_step + 1 if mode == 'single' else len(previous['steps'])) if previous['steps'] is not None else None
            previous_end = run_step_end(previous) if previous['steps'] is not None else None
            if (previous['loop_id'] != loop['id'] or previous['start_step'] != start_step
                    or previous_end != requested_end):
                raise HTTPException(409, 'This execution request was already used for a different run')
            return public_run(previous)
    await release_expired_control(db, worker['id'])
    await release_expired_queue(db, worker['id'])
    worker = await assigned_worker(db, principal)
    if worker['mode'] != 'agent': raise HTTPException(409, 'Return desktop control to the worker first')
    if not worker['last_seen'] or (datetime.now(timezone.utc)-worker['last_seen']).total_seconds()>30:
        raise HTTPException(409, 'Worker is offline')
    active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running','paused')", (worker['id'],))).fetchone()
    if active or worker['active_run']: raise HTTPException(409, 'The desktop already has an active run')
    loop = await (await db.execute('SELECT * FROM loops WHERE id=%s AND company_id=%s FOR UPDATE',
                                  (loop['id'], principal['company_id']))).fetchone()
    end_step = None
    if loop['steps'] is not None:
        if not loop['steps']: raise HTTPException(409, 'Add a step before running this workbench')
        if not 0 <= start_step < len(loop['steps']):
            raise HTTPException(422, 'Choose an existing step as the starting point')
        end_step = start_step + 1 if mode == 'single' else len(loop['steps'])
        # Drafts can contain unfinished steps. Only executable actions in the
        # requested range cross the worker boundary; other rows stay as evidence.
        for index in range(start_step, end_step):
            try: normalize_action(loop['steps'][index]['action'])
            except ValueError as error:
                raise HTTPException(422, f'Step {index + 1}: {error}') from error
    elif start_step != 0 or mode != 'sequence':
        raise HTTPException(422, 'Step execution requires a manual workbench')
    run = await (await db.execute("""INSERT INTO runs(id,company_id,loop_id,worker_id,initiated_by,
        request_session_hash,dispatch_before,steps,run_mode,request_id,start_step,end_step,next_step)
        VALUES(%s,%s,%s,%s,%s,%s,now()+interval '5 minutes',%s,%s,%s,%s,%s,%s) RETURNING *""",
        (uuid4(),principal['company_id'],loop['id'],worker['id'],acting_user(principal),principal['session_hash'],
         Jsonb(loop['steps']) if loop['steps'] is not None else None, mode, request_id,
         start_step,end_step,start_step))).fetchone()
    run['initiator'] = {'id': acting_user(principal), 'email': principal['target_email'], 'display_name': principal['target_display_name']}
    message = ('Manual sequence queued for its assigned Omarchy worker' if loop['steps'] is not None else
               'Desktop reserved; the instruction will run automatically when ready'
               if loop['status'] == 'generating' else 'Saved instruction queued for its assigned Omarchy worker')
    await event(db, loop['id'], 'delivery_queued', message, run_id=run['id'],
                data={'actor_user_id': str(principal['user_id']), 'start_step':start_step,
                      'end_step':end_step, 'mode':mode})
    return public_run(run)


def throttle(request, identity='', *, peer_limit=True, limit=10):
    now = time.monotonic()
    peer = request.client.host if request.client else 'unknown'
    keys = (('peer:' + peer,) if peer_limit else ()) + (('identity:' + token_hash(identity),) if identity else ())
    if len(login_attempts) >= 10000:
        for key in list(login_attempts):
            if now-login_attempts[key][-1] >= 300: login_attempts.pop(key, None)
        if any(key not in login_attempts for key in keys) and len(login_attempts) >= 10000:
            raise HTTPException(429, 'Sign-in service is busy; try again in five minutes')
    for key in keys:
        recent = [stamp for stamp in login_attempts.get(key, []) if now-stamp < 300]
        if len(recent) >= limit: raise HTTPException(429, 'Too many attempts; wait five minutes')
        login_attempts[key] = recent + [now]


async def issue_session(db, response, user_id, company_id):
    raw = secrets.token_urlsafe(32)
    await db.execute("INSERT INTO sessions(token_hash,expires_at,user_id,company_id) VALUES(%s,now()+interval '12 hours',%s,%s)",
                     (token_hash(raw), user_id, company_id))
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
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' wss://run.cua.ai; frame-ancestors 'none'"
    return response


class AccountBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class EmailBody(AccountBody):
    email: str = Field(min_length=3, max_length=254)


class Login(EmailBody):
    password: str = Field(max_length=500)


class Signup(Login):
    password: str = Field(min_length=12, max_length=500)
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal['user', 'company_admin']
    company_id: UUID | None = None
    company_name: str | None = Field(default=None, min_length=1, max_length=100)
    company_password: str = Field(min_length=12, max_length=500)


class AccountToken(AccountBody):
    token: str = Field(min_length=32, max_length=500)


class PasswordReset(AccountToken):
    password: str = Field(min_length=12, max_length=500)


class CompanyUpdate(AccountBody):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    company_password: str | None = Field(default=None, min_length=12, max_length=500)
    current_password: str = Field(max_length=500)


class UserUpdate(AccountBody):
    active: bool
    role: Literal['user', 'company_admin'] | None = None


class WorkerDeployment(BaseModel):
    phase: str
    expected_worker_id: str = Field(min_length=1, max_length=200)
    environment: str
    company_id: UUID
    assigned_user_id: UUID
    revision: str | None = Field(default=None, max_length=200)


class AuthoredStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    action: dict
    instruction: str | None = Field(default=None, max_length=2000)


def authored_steps(steps):
    """Persist drafts in the same action representation used for execution."""
    result = []
    seen = set()
    for step in steps:
        if step.id in seen: raise HTTPException(422, 'Every step must have a distinct identity')
        seen.add(step.id)
        try: action = normalize_action(step.action, allow_incomplete=True)
        except ValueError as error: raise HTTPException(422, str(error)) from error
        row = {'id': str(step.id), 'action': action}
        if step.instruction: row['instruction'] = step.instruction.strip()
        result.append(row)
    return result


class Prompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    start: bool = False
    steps: list[AuthoredStep] | None = Field(default=None, max_length=100)


class LoopUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=2000)
    steps: list[AuthoredStep] = Field(max_length=100)
    version: int = Field(ge=0)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['step', 'sequence', 'single'] = 'sequence'
    start_step: int = Field(default=0, ge=0)
    request_id: UUID | None = None


class RunControl(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command: Literal['step', 'run', 'pause', 'stop']
    version: int = Field(ge=0)


class Mode(BaseModel):
    mode: str


class WorkerPoll(BaseModel):
    revision: str = Field(max_length=200)
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    run_id: UUID | None = None
    expected_step: int | None = Field(default=None, ge=0)


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


def account_email(value):
    try: return normalize_email(value)
    except ValueError as error: raise HTTPException(400, str(error)) from error


async def new_account_token(db, user, purpose):
    # Persistent per-account limit supplements peer/email/company attempt limits.
    count = await (await db.execute('''SELECT count(*) AS n FROM auth_tokens
        WHERE company_id=%s AND user_id=%s AND created_at>now()-interval '1 hour' ''',
        (user['company_id'],user['id']))).fetchone()
    if count['n'] >= 5: return None
    raw = secrets.token_urlsafe(32)
    minutes = 30 if purpose == 'reset_password' else 1440
    await db.execute('''INSERT INTO auth_tokens(token_hash,company_id,user_id,purpose,expires_at)
        VALUES(%s,%s,%s,%s,now()+make_interval(mins => %s))''',
        (token_hash(raw),user['company_id'],user['id'],purpose,minutes))
    return raw


async def deliver_account_email(email, raw, purpose):
    if not raw: return delivery_mode()
    try: return await asyncio.to_thread(send_account_mail, email, raw, purpose)
    except Exception as error:
        logger.warning('account mail delivery failed error=%s', type(error).__name__)
        return 'unavailable'


def mail_response(mode, *, signup=False):
    if mode == 'local':
        message = 'Development email was captured locally; no email was sent. Ask the local operator for the link.'
    elif mode == 'unavailable':
        message = 'Email delivery is unavailable. Contact the operator to configure email, then request a new link.'
    else:
        message = ('Check your email to verify your account.' if signup else
                   'If an eligible account exists, a link has been requested. Check your email.')
    return {'message': message, 'email_delivery': mode}


@app.get('/api/companies')
async def public_companies(request: Request):
    # No private company attributes or password material leaves this endpoint.
    async with database() as db:
        rows = await (await db.execute('''SELECT c.id,c.name FROM companies c
            WHERE c.join_password_hash IS NOT NULL AND EXISTS(SELECT 1 FROM users u
                WHERE u.company_id=c.id AND u.role='company_admin' AND u.email_verified_at IS NOT NULL
                AND u.disabled_at IS NULL) ORDER BY lower(c.name),c.id''')).fetchall()
    return {'companies': rows}


@app.post('/api/signup')
async def signup(body: Signup, request: Request):
    email = account_email(body.email)
    throttle(request, email)
    name = body.display_name.strip()
    if not name: raise HTTPException(400, 'Enter your name')
    if body.role == 'company_admin':
        if body.company_id or not body.company_name or not body.company_name.strip():
            raise HTTPException(400, 'Company admins must create a new company')
    elif not body.company_id or body.company_name is not None:
        raise HTTPException(400, 'Users must select an existing company')
    if body.company_id: throttle(request, 'join:' + str(body.company_id), peer_limit=False, limit=30)
    async with database() as db:
        await db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('ctrlloop.email.' + email,))
        exists = await (await db.execute('SELECT 1 FROM users WHERE email=%s', (email,))).fetchone()
        if exists: raise HTTPException(409, 'Unable to sign up with this email. Try signing in or recovering your account.')
        if body.role == 'user':
            company = await (await db.execute('''SELECT c.* FROM companies c WHERE c.id=%s
                AND EXISTS(SELECT 1 FROM users u WHERE u.company_id=c.id AND u.role='company_admin'
                    AND u.email_verified_at IS NOT NULL AND u.disabled_at IS NULL) FOR SHARE OF c''',
                (body.company_id,))).fetchone()
            valid = await check_password(body.company_password, (company or {}).get('join_password_hash') or dummy_password_hash)
            if not company or not valid: raise HTTPException(400, 'Company or company password is incorrect')
            company_id = company['id']
        else:
            company_id = uuid4()
            encoded_join = await hash_password(body.company_password)
            await db.execute('INSERT INTO companies(id,slug,name,join_password_hash) VALUES(%s,%s,%s,%s)',
                (company_id,'company-' + company_id.hex,body.company_name.strip(),encoded_join))
        encoded = await hash_password(body.password)
        user = await (await db.execute('''INSERT INTO users(id,company_id,email,display_name,password_hash,role)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING id,company_id,email''',
            (uuid4(),company_id,email,name,encoded,body.role))).fetchone()
        raw = await new_account_token(db, user, 'verify_email')
    mode = await deliver_account_email(email, raw, 'verify_email')
    return {'verification_required': True, **mail_response(mode, signup=True)}


@app.post('/api/login')
async def login(body: Login, request: Request, response: Response):
    email = account_email(body.email)
    throttle(request, email)
    async with database() as db:
        user = await (await db.execute('SELECT * FROM users WHERE email=%s FOR SHARE', (email,))).fetchone()
        valid = await check_password(body.password, (user or {}).get('password_hash') or dummy_password_hash)
        if not valid or not user or user['disabled_at']:
            raise HTTPException(401, 'Incorrect email or password')
        if not user['email_verified_at']:
            raise HTTPException(403, 'Verify your email before signing in. You can request a new verification link.')
        old_digest = token_hash(request.cookies.get('ctrl_session', ''))
        await lock_session_owner(db, old_digest)
        await db.execute("UPDATE workers SET mode='agent',human_session=NULL WHERE human_session=%s", (old_digest,))
        await interrupt_queued_runs(db, 'Sign-in changed before dispatch. Start again when ready.',
                                    clause='request_session_hash=%s', params=(old_digest,))
        await db.execute('DELETE FROM sessions WHERE token_hash=%s', (old_digest,))
        await issue_session(db, response, user['id'], user['company_id'])
    return {'authenticated': True}


async def consume_account_token(db, raw, purpose):
    digest = token_hash(raw)
    candidate = await (await db.execute('SELECT user_id FROM auth_tokens WHERE token_hash=%s', (digest,))).fetchone()
    if not candidate: raise HTTPException(400, 'Link is invalid, expired or already used')
    # Shared lock order with login, worker authorization, and account management.
    user = await (await db.execute('SELECT * FROM users WHERE id=%s AND disabled_at IS NULL FOR UPDATE',
                                  (candidate['user_id'],))).fetchone()
    if not user: raise HTTPException(400, 'Link is invalid, expired or already used')
    token = await (await db.execute('''DELETE FROM auth_tokens WHERE token_hash=%s AND purpose=%s
        AND company_id=%s AND user_id=%s AND expires_at>now() RETURNING token_hash''',
        (digest,purpose,user['company_id'],user['id']))).fetchone()
    if not token: raise HTTPException(400, 'Link is invalid, expired or already used')
    return user


@app.post('/api/verify-email')
async def verify_email(body: AccountToken, request: Request):
    throttle(request)
    async with database() as db:
        user = await consume_account_token(db, body.token, 'verify_email')
        await db.execute('UPDATE users SET email_verified_at=COALESCE(email_verified_at,now()) WHERE id=%s', (user['id'],))
        await db.execute("DELETE FROM auth_tokens WHERE user_id=%s AND purpose='verify_email'", (user['id'],))
    return {'message': 'Email verified. Sign in with your personal password.'}


async def request_account_email(body, request, purpose):
    email = account_email(body.email)
    throttle(request, email)
    raw = None
    async with database() as db:
        user = await (await db.execute('SELECT * FROM users WHERE email=%s AND disabled_at IS NULL FOR UPDATE', (email,))).fetchone()
        if user and (purpose == 'reset_password' or not user['email_verified_at']):
            raw = await new_account_token(db, user, purpose)
    return mail_response(await deliver_account_email(email, raw, purpose))


@app.post('/api/resend-verification')
async def resend_verification(body: EmailBody, request: Request):
    return await request_account_email(body, request, 'verify_email')


@app.post('/api/forgot-password')
async def forgot_password(body: EmailBody, request: Request):
    return await request_account_email(body, request, 'reset_password')


@app.post('/api/reset-password')
async def reset_password(body: PasswordReset, request: Request, response: Response):
    throttle(request)
    async with database() as db:
        user = await consume_account_token(db, body.token, 'reset_password')
        encoded = await hash_password(body.password)
        await db.execute('UPDATE users SET password_hash=%s,email_verified_at=COALESCE(email_verified_at,now()) WHERE id=%s', (encoded,user['id']))
        # Reset proves possession of the email; migrated pending accounts can activate here.
        await db.execute('DELETE FROM auth_tokens WHERE user_id=%s', (user['id'],))
        await db.execute('''UPDATE workers SET mode=CASE WHEN mode='human' THEN 'agent' ELSE mode END,human_session=NULL
            WHERE human_session IN (SELECT token_hash FROM sessions WHERE user_id=%s)''', (user['id'],))
        await interrupt_queued_runs(db, 'Password reset before dispatch. Start again after signing in.',
            clause='request_session_hash IN (SELECT token_hash FROM sessions WHERE user_id=%s)', params=(user['id'],))
        await db.execute('DELETE FROM sessions WHERE user_id=%s', (user['id'],))
    response.delete_cookie('ctrl_session', secure=True, httponly=True, samesite='strict')
    return {'message': 'Password reset. Sign in with your new personal password.'}


@app.post('/api/logout')
async def logout(request: Request, response: Response):
    digest = token_hash(request.cookies.get('ctrl_session', ''))
    async with database() as db:
        await lock_session_owner(db, digest)
        await db.execute("UPDATE workers SET mode='agent',human_session=NULL WHERE human_session=%s", (digest,))
        await interrupt_queued_runs(db, 'Signed out before dispatch. Start again after signing in.',
                                    clause='request_session_hash=%s', params=(digest,))
        await db.execute('DELETE FROM sessions WHERE token_hash=%s', (digest,))
    response.delete_cookie('ctrl_session', secure=True, httponly=True, samesite='strict')
    return {'authenticated': False}


@app.get('/api/session')
async def session(request: Request):
    async with database() as db:
        try: identity = await authorize(request, db)
        except HTTPException as error:
            if error.status_code == 401: return {'authenticated': False}
            raise
        worker = await assigned_worker(db, identity, required=False)
        # Company/user selection is part of the viewing authorization. Revoke
        # old guest connections even when the new target has no desktop yet.
        await db.execute('''DELETE FROM desktop_connections WHERE session_hash=%s
            AND (company_id<>%s OR assigned_user_id IS DISTINCT FROM %s)''',
            (identity['session_hash'], identity['company_id'], identity['target_user_id']))
        queued = None
        if worker:
            await release_expired_control(db, worker['id'])
            await release_expired_queue(db, worker['id'])
            worker = await assigned_worker(db, identity)
            queued = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status='queued'", (worker['id'],))).fetchone()
        return {'authenticated': True,
                'user': {key: identity[key] for key in ('email','display_name','is_devadmin')} | {'id': identity['user_id']},
                'company': {'id': identity['company_id'], 'slug': identity['company_slug'],
                            'name': identity['company_name'], 'role': identity['role'] if identity['company_id']==identity['home_company_id'] else None},
                'home_company_id': identity['home_company_id'], 'target_user_id': identity['target_user_id'],
                'worker': public_worker(worker, identity['session_hash'], queued_run=queued['id'] if queued else None),
                'model': MODEL, 'reasoning_effort': 'high', 'jev_model': 'jev-1.13.0'}


@app.get('/api/admin/companies')
async def admin_companies(request: Request):
    async with database() as db:
        identity = await authorize(request, db)
        if not identity['is_devadmin']: raise HTTPException(403, 'Platform devadmin access required')
        return {'companies': await (await db.execute('SELECT id,slug,name FROM companies ORDER BY name,id')).fetchall()}


@app.get('/api/company/users')
async def company_users(request: Request):
    async with database() as db:
        identity = await authorize(request, db, owner=True)
        rows = await (await db.execute('''SELECT u.id AS user_id,u.email,u.display_name,u.role,
            CASE WHEN u.disabled_at IS NOT NULL THEN 'disabled'
                 WHEN u.email_verified_at IS NULL THEN 'pending' ELSE 'active' END AS status,
            w.id AS worker_id,w.last_seen,w.mode FROM users u
            LEFT JOIN workers w ON w.company_id=u.company_id AND w.assigned_user_id=u.id AND w.disabled_at IS NULL
            WHERE u.company_id=%s ORDER BY u.display_name,u.email''', (identity['company_id'],))).fetchall()
        for row in rows:
            worker_id,seen,mode = row.pop('worker_id'),row.pop('last_seen'),row.pop('mode')
            row['worker'] = {'id': worker_id, 'mode': mode, 'status': 'online' if seen and (datetime.now(timezone.utc)-seen).total_seconds()<30 else 'offline'} if worker_id else None
        return {'users': rows}


@app.patch('/api/company')
async def update_company(body: CompanyUpdate, request: Request):
    throttle(request)
    async with database() as db:
        identity = await authorize_company_management(request, db)
        credentials = await (await db.execute('SELECT password_hash FROM users WHERE id=%s', (identity['user_id'],))).fetchone()
        if not await check_password(body.current_password, credentials['password_hash']):
            raise HTTPException(403, 'Your current personal password is incorrect')
        if body.name is not None:
            if not body.name.strip(): raise HTTPException(400, 'Enter a company name')
            await db.execute('UPDATE companies SET name=%s WHERE id=%s', (body.name.strip(),identity['company_id']))
        if body.company_password is not None:
            encoded = await hash_password(body.company_password)
            await db.execute('UPDATE companies SET join_password_hash=%s WHERE id=%s', (encoded,identity['company_id']))
    return {'message': 'Company updated. Company password changes apply only to future signups.'}


@app.patch('/api/company/users/{user_id}')
async def update_company_user(user_id: UUID, body: UserUpdate, request: Request):
    async with database() as db:
        identity = await authorize_company_management(request, db)
        if user_id == identity['user_id']: raise HTTPException(409, 'You cannot change your own company role or access')
        await db.execute('SELECT id FROM companies WHERE id=%s FOR UPDATE', (identity['company_id'],))
        user = await (await db.execute('SELECT * FROM users WHERE company_id=%s AND id=%s FOR UPDATE',
            (identity['company_id'],user_id))).fetchone()
        if not user: raise HTTPException(404, 'Company user not found')
        if user['is_devadmin'] and not identity['is_devadmin']:
            raise HTTPException(403, 'Platform devadmin accounts require platform administration')
        role = body.role or user['role']
        if user['role'] == 'company_admin' and (not body.active or role != 'company_admin'):
            other = await (await db.execute("SELECT 1 FROM users WHERE company_id=%s AND id<>%s AND role='company_admin' AND disabled_at IS NULL AND email_verified_at IS NOT NULL LIMIT 1",
                (identity['company_id'],user_id))).fetchone()
            if not other: raise HTTPException(409, 'Keep at least one verified, active company admin')
        active = await (await db.execute("SELECT 1 FROM runs WHERE company_id=%s AND initiated_by=%s AND status IN ('queued','running','paused') LIMIT 1",
            (identity['company_id'],user_id))).fetchone()
        if not body.active and active: raise HTTPException(409, 'Stop this user’s queued, running, or paused work before disabling access')
        await db.execute('UPDATE users SET role=%s,disabled_at=' + ('NULL' if body.active else 'now()') + ' WHERE id=%s AND company_id=%s',
            (role,user_id,identity['company_id']))
        if not body.active:
            await db.execute('''UPDATE workers SET mode=CASE WHEN mode='human' THEN 'agent' ELSE mode END,human_session=NULL
                WHERE (company_id=%s AND assigned_user_id=%s) OR human_session IN(SELECT token_hash FROM sessions WHERE user_id=%s)''',
                (identity['company_id'],user_id,user_id))
            await interrupt_queued_runs(db, 'Account disabled before dispatch. No actions were sent.',
                clause='request_session_hash IN (SELECT token_hash FROM sessions WHERE user_id=%s)', params=(user_id,))
            await db.execute('DELETE FROM sessions WHERE user_id=%s', (user_id,))
            await db.execute('DELETE FROM auth_tokens WHERE user_id=%s', (user_id,))
    return {'user_id': user_id, 'active': body.active, 'role': role}



@app.post('/api/worker/mode')
async def change_mode(body: Mode, request: Request):
    if request.headers.get('origin') != os.environ['APP_ORIGIN']: raise HTTPException(403, 'Same-origin request required')
    if body.mode not in ('agent', 'human'): raise HTTPException(400, 'Invalid mode')
    async with database() as db:
        identity = await authorize(request, db)
        digest = identity['session_hash']
        worker = await assigned_worker(db, identity)
        await release_expired_control(db, worker['id'])
        await release_expired_queue(db, worker['id'])
        worker = await assigned_worker(db, identity)
        if worker['mode'] == 'maintenance': raise HTTPException(409, 'Desktop deployment is in progress')
        active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running','paused')", (worker['id'],))).fetchone()
        if active: raise HTTPException(409, 'Wait for the active run to finish before changing control')
        if worker['mode'] == 'human' and worker['human_session'] != digest: raise HTTPException(409, 'Another signed-in session owns the desktop')
        await db.execute('UPDATE workers SET mode=%s,human_session=%s WHERE id=%s', (body.mode, digest if body.mode=='human' else None, worker['id']))
    return {'mode': body.mode}


@app.get('/api/loops')
async def list_loops(request: Request, archived: bool = False):
    async with database() as db:
        identity = await authorize(request, db)
        clause = 'l.company_id=%s AND l.archived_at IS NOT NULL AND l.deleted_at IS NULL' if archived else 'l.company_id=%s AND l.archived_at IS NULL AND l.deleted_at IS NULL'
        return await (await db.execute('''SELECT l.*,jsonb_build_object('id',u.id,'email',u.email,'display_name',u.display_name) AS creator
            FROM loops l LEFT JOIN users u ON u.id=l.created_by WHERE ''' + clause + '''
            ORDER BY l.created_at DESC LIMIT 100''', (identity['company_id'],))).fetchall()


@app.post('/api/loops/{identity}/archive')
async def archive_loop(identity: UUID, request: Request):
    async with database() as db:
        principal = await authorize(request, db)
        row = await (await db.execute('SELECT * FROM loops WHERE id=%s AND company_id=%s FOR UPDATE', (identity, principal['company_id']))).fetchone()
        if not row: raise HTTPException(404, 'Loop not found')
        if row['created_by'] != acting_user(principal) and not principal['is_devadmin']:
            raise HTTPException(403, 'Only the author can archive this CtrlLoop')
        if row['deleted_at'] is not None: raise HTTPException(409, 'This CtrlLoop was permanently deleted')
        if row['archived_at'] is not None: return row
        active = await (await db.execute("SELECT id FROM runs WHERE loop_id=%s AND status IN ('queued','running','paused')", (identity,))).fetchone()
        if active: raise HTTPException(409, 'Stop the active run before archiving this CtrlLoop')
        row = await (await db.execute('UPDATE loops SET archived_at=now() WHERE id=%s AND company_id=%s RETURNING *', (identity, principal['company_id']))).fetchone()
        await event(db, identity, 'loop_archived', 'CtrlLoop archived', data={'actor_user_id': str(principal['user_id'])})
        return row


@app.post('/api/loops/{identity}/restore')
async def restore_loop(identity: UUID, request: Request):
    async with database() as db:
        principal = await authorize(request, db)
        row = await (await db.execute('SELECT * FROM loops WHERE id=%s AND company_id=%s FOR UPDATE', (identity, principal['company_id']))).fetchone()
        if not row: raise HTTPException(404, 'Loop not found')
        if row['created_by'] != acting_user(principal) and not principal['is_devadmin']:
            raise HTTPException(403, 'Only the author can restore this CtrlLoop')
        if row['deleted_at'] is not None: raise HTTPException(409, 'This CtrlLoop was permanently deleted')
        if row['archived_at'] is None: return row
        row = await (await db.execute('UPDATE loops SET archived_at=NULL WHERE id=%s AND company_id=%s RETURNING *', (identity, principal['company_id']))).fetchone()
        await event(db, identity, 'loop_restored', 'CtrlLoop restored', data={'actor_user_id': str(principal['user_id'])})
        return row


@app.delete('/api/loops/{identity}')
async def delete_loop(identity: UUID, request: Request):
    async with database() as db:
        principal = await authorize(request, db)
        row = await (await db.execute('SELECT * FROM loops WHERE id=%s AND company_id=%s FOR UPDATE', (identity, principal['company_id']))).fetchone()
        if not row: raise HTTPException(404, 'Loop not found')
        if row['created_by'] != acting_user(principal) and not principal['is_devadmin']:
            raise HTTPException(403, 'Only the author can permanently delete this CtrlLoop')
        if row['archived_at'] is None: raise HTTPException(409, 'Archive this CtrlLoop before deleting it permanently')
        active = await (await db.execute("SELECT id FROM runs WHERE loop_id=%s AND status IN ('queued','running','paused')", (identity,))).fetchone()
        if active: raise HTTPException(409, 'Stop the active run before permanently deleting this CtrlLoop')
        await db.execute('DELETE FROM events WHERE company_id=%s AND loop_id=%s', (principal['company_id'], identity))
        await db.execute('DELETE FROM runs WHERE company_id=%s AND loop_id=%s', (principal['company_id'], identity))
        await db.execute('DELETE FROM loops WHERE id=%s AND company_id=%s', (identity, principal['company_id']))
        await event(db, identity, 'loop_deleted', 'CtrlLoop permanently deleted', data={'actor_user_id': str(principal['user_id'])})
        return {'deleted': True, 'loop_id': str(identity)}


@app.post('/api/loops')
async def create_loop(body: Prompt, request: Request):
    prompt = body.prompt.strip()
    if not prompt: raise HTTPException(400, 'Enter a request')
    steps = authored_steps(body.steps) if body.steps is not None else None
    identity = uuid4()
    async with database() as db:
        principal = await authorize(request, db)
        if steps is not None:
            row = await (await db.execute('''INSERT INTO loops(id,prompt,instruction,model,company_id,
                created_by,status,steps,generated_at) VALUES(%s,%s,%s,'manual',%s,%s,'ready',%s,now()) RETURNING *''',
                (identity,prompt,prompt,principal['company_id'],acting_user(principal),Jsonb(steps)))).fetchone()
            row['creator'] = {'id': acting_user(principal), 'email': principal['target_email'], 'display_name': principal['target_display_name']}
            await event(db, identity, 'steps_saved', 'Authored steps saved',
                        data={'step_count':len(steps), 'version':0, 'actor_user_id':str(principal['user_id'])})
            row['runs'] = [await queue_run(db, row, principal, request_id=uuid4())] if body.start else []
            return row
        busy = await (await db.execute("SELECT count(*) AS count FROM loops WHERE company_id=%s AND status='generating'", (principal['company_id'],))).fetchone()
        if busy['count'] >= 3: raise HTTPException(429, 'Generation is busy; try again shortly')
        row = await (await db.execute('INSERT INTO loops(id,prompt,model,company_id,created_by) VALUES(%s,%s,%s,%s,%s) RETURNING *',
            (identity,prompt,MODEL,principal['company_id'],acting_user(principal)))).fetchone()
        row['creator'] = {'id': acting_user(principal), 'email': principal['target_email'], 'display_name': principal['target_display_name']}
        await event(db, identity, 'user_request', prompt, data={'actor_user_id': str(principal['user_id'])})
        await event(db, identity, 'llm_generation', 'Generating an instruction with gpt-6-luna · high reasoning')
        row['runs'] = [await queue_run(db, row, principal)] if body.start else []
    task = asyncio.create_task(generate_instruction(identity, prompt, principal['company_id'])); generation_tasks.add(task); task.add_done_callback(generation_tasks.discard)
    return row


@app.patch('/api/loops/{identity}')
async def update_loop(identity: UUID, body: LoopUpdate, request: Request):
    prompt = body.prompt.strip()
    if not prompt: raise HTTPException(400, 'Enter a workbench name')
    steps = authored_steps(body.steps)
    async with database() as db:
        principal = await authorize(request, db)
        row = await (await db.execute('SELECT * FROM loops WHERE id=%s AND company_id=%s FOR UPDATE',
                                    (identity, principal['company_id']))).fetchone()
        if not row: raise HTTPException(404, 'Loop not found')
        if row['created_by'] != acting_user(principal) and not principal['is_devadmin']:
            raise HTTPException(403, 'Only the author can edit this workbench')
        if row['archived_at'] is not None or row['deleted_at'] is not None:
            raise HTTPException(409, 'Archived or deleted CtrlLoops cannot be edited')
        if row['steps'] is None: raise HTTPException(409, 'Only manual workbenches have editable steps')
        if row['version'] != body.version: raise HTTPException(409, 'This workbench changed; reload before saving')
        active = await (await db.execute("SELECT id FROM runs WHERE loop_id=%s AND status IN ('queued','running','paused')",
                                       (identity,))).fetchone()
        if active: raise HTTPException(409, 'Stop the active run before editing its steps')
        row = await (await db.execute('''UPDATE loops SET prompt=%s,instruction=%s,steps=%s,version=version+1
            WHERE id=%s RETURNING *''', (prompt,prompt,Jsonb(steps),identity))).fetchone()
        await event(db, identity, 'steps_saved', 'Authored steps saved',
                    data={'step_count':len(steps), 'version':row['version'], 'actor_user_id':str(principal['user_id'])})
        return row


async def generate_instruction(identity, prompt, company_id):
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
            await db.execute("UPDATE loops SET instruction=%s,status='ready',generated_at=now(),llm_response_id=%s,llm_usage=%s WHERE id=%s AND company_id=%s", (instruction,result.get('id'),Jsonb(result.get('usage',{})),identity,company_id))
            await event(db,identity,'llm_generated',instruction,data={'model':result.get('model'), 'reasoning_effort':'high','duration_ms':round((time.monotonic()-started)*1000), 'response_id':result.get('id'),'usage':result.get('usage')})
            await event(db,identity,'db_saved','Generated instruction committed to PostgreSQL')
    except asyncio.CancelledError:
        async with database() as db:
            await interrupt_queued_runs(db, 'Generation interrupted before dispatch. No actions were sent.',
                                        clause='loop_id=%s AND company_id=%s', params=(identity,company_id))
            await db.execute("UPDATE loops SET status='error',error='Generation interrupted; inspect available request evidence.' WHERE id=%s AND company_id=%s", (identity,company_id))
            await event(db, identity, 'error', 'Generation interrupted; response capture may be unavailable',
                        data={'error_type': 'CancelledError', 'duration_ms': round((time.monotonic()-started)*1000, 3)})
        raise
    except Exception as error:
        message = f'LLM generation failed ({type(error).__name__}); no instruction was fabricated.'
        async with database() as db:
            await interrupt_queued_runs(db, 'Generation failed before dispatch. No actions were sent.',
                                        clause='loop_id=%s AND company_id=%s', params=(identity,company_id))
            await db.execute("UPDATE loops SET status='error',error=%s WHERE id=%s AND company_id=%s", (message,identity,company_id))
            await event(db,identity,'error',message,data={'error_type':type(error).__name__, 'duration_ms':round((time.monotonic()-started)*1000, 3)})


@app.get('/api/loops/{identity}')
async def loop_detail(identity: UUID, request: Request):
    async with database() as db:
        principal = await authorize(request, db)
        row = await (await db.execute('''SELECT l.*,jsonb_build_object('id',u.id,'email',u.email,'display_name',u.display_name) AS creator
            FROM loops l LEFT JOIN users u ON u.id=l.created_by WHERE l.id=%s AND l.company_id=%s''', (identity,principal['company_id']))).fetchone()
        if not row: raise HTTPException(404,'Loop not found')
        row['events'] = await (await db.execute('SELECT * FROM events WHERE company_id=%s AND loop_id=%s AND run_id IS NULL ORDER BY created_at',(principal['company_id'],identity))).fetchall()
        row['runs'] = await (await db.execute('''SELECT r.*,jsonb_build_object('id',u.id,'email',u.email,'display_name',u.display_name) AS initiator
            FROM runs r LEFT JOIN users u ON u.id=r.initiated_by JOIN workers w ON w.id=r.worker_id
            WHERE r.loop_id=%s AND r.company_id=%s ORDER BY r.created_at DESC''', (identity,principal['company_id']))).fetchall()
        row['runs'] = [public_run(run) for run in row['runs']]
        for run in row['runs']:
            run['events'] = await (await db.execute("SELECT * FROM events WHERE company_id=%s AND run_id=%s AND stage<>'step_trace' ORDER BY created_at",(principal['company_id'],run['id']))).fetchall()
    # Large run histories must not stall desktop policy replies. Release the
    # database transaction before encoding the same response in a worker thread.
    return await asyncio.to_thread(lambda: JSONResponse(content=jsonable_encoder(row)))


@app.post('/api/loops/{identity}/runs')
async def create_run(identity: UUID, request: Request, body: RunRequest | None = None):
    async with database() as db:
        principal = await authorize(request, db)
        loop = await (await db.execute('SELECT * FROM loops WHERE id=%s AND company_id=%s', (identity,principal['company_id']))).fetchone()
        if not loop or loop['status'] != 'ready': raise HTTPException(409, 'A saved instruction is required')
        if loop['archived_at'] is not None or loop['deleted_at'] is not None:
            raise HTTPException(409, 'Archived or deleted CtrlLoops cannot be started')
        if loop['steps'] is not None and (body is None or body.request_id is None):
            raise HTTPException(422, 'Manual runs require a unique request_id to prevent duplicate execution')
        return await queue_run(db, loop, principal, mode=body.mode if body else 'sequence',
                               start_step=body.start_step if body else 0,
                               request_id=body.request_id if body else None)


@app.patch('/api/runs/{identity}')
async def control_run(identity: UUID, body: RunControl, request: Request):
    async with database() as db:
        principal = await authorize(request, db)
        worker = await assigned_worker(db, principal)
        run = await (await db.execute('''SELECT * FROM runs WHERE id=%s AND company_id=%s
            AND worker_id=%s AND initiated_by=%s FOR UPDATE''',
            (identity,principal['company_id'],worker['id'],acting_user(principal)))).fetchone()
        if not run: raise HTTPException(404, 'Run not found for this desktop')
        if run['steps'] is None: raise HTTPException(409, 'Step controls require a manual workbench run')
        if run['status'] not in ('queued','running','paused'):
            raise HTTPException(409, 'This run has ended; start a new run explicitly to execute again')
        # Only commands which grant more input need the exact cursor revision.
        # Pause/stop latch even if the worker claimed a step since the UI poll.
        if body.command in ('run','step') and run['control_version'] != body.version:
            raise HTTPException(409, 'Run state changed; refresh its status before continuing')
        if run['control'] == 'stop':
            if body.command == 'stop': return public_run(run)
            raise HTTPException(409, 'A stop request is final; wait for this run to finish')
        if body.command in ('run','step'):
            if run['status'] != 'paused' or run['step_in_flight'] is not None:
                raise HTTPException(409, 'Wait for the current step to reach a pause boundary')
            if run['next_step'] >= run_step_end(run): raise HTTPException(409, 'Every selected step has already executed')
            run = await (await db.execute('''UPDATE runs SET control='run',status='running',run_mode=%s,
                control_version=control_version+1 WHERE id=%s RETURNING *''',
                ('step' if body.command == 'step' else 'sequence',identity))).fetchone()
        elif body.command == 'stop' and run['status'] == 'queued':
            run = await (await db.execute("""UPDATE runs SET control='stop',status='interrupted',finished_at=now(),
                request_session_hash=NULL,error='Stopped before dispatch; no steps executed.',
                control_version=control_version+1 WHERE id=%s RETURNING *""", (identity,))).fetchone()
        else:
            # Pause and stop never cancel an in-flight native action. The worker
            # observes this durable request before claiming its next step.
            run = await (await db.execute('''UPDATE runs SET control=%s,control_version=control_version+1
                WHERE id=%s RETURNING *''', (body.command,identity))).fetchone()
        await event(db,run['loop_id'],'run_control', f'{body.command.capitalize()} requested',run_id=identity,
                    data={'command':body.command,'control_version':run['control_version'],'requested_version':body.version,
                          'next_step':run['next_step'],'actor_user_id':str(principal['user_id'])})
        return public_run(run)


async def claim_authored_step(db, worker, body):
    """Grant native execution at most once; ambiguous delivery is never replayed."""
    run = await owned_run(db, body.run_id, worker)
    if run['steps'] is None: raise HTTPException(409, 'This run has no authored steps')
    if worker['active_run'] != run['id'] or worker['mode'] != 'agent':
        raise HTTPException(409, 'This worker does not own desktop execution')
    if body.expected_step is None or body.expected_step != run['next_step']:
        raise HTTPException(409, 'Step cursor changed; execution cannot be replayed')
    if run['step_in_flight'] is not None:
        raise HTTPException(409, 'A step was already dispatched; inspect before starting again')
    if not run['start_step'] <= run['next_step'] <= run_step_end(run):
        raise HTTPException(409, 'Step cursor is outside the selected execution range')
    await db.execute('UPDATE workers SET last_seen=now() WHERE id=%s', (worker['id'],))
    reply = {'step':None, 'step_index':run['next_step'], 'next_step':run['next_step']}
    if run['control'] == 'stop': return {**reply, 'control':'stop'}
    if run['next_step'] == run_step_end(run): return {**reply, 'control':'complete'}
    if run['control'] == 'pause':
        if run['status'] != 'paused':
            await db.execute("UPDATE runs SET status='paused',control_version=control_version+1 WHERE id=%s", (run['id'],))
            await event(db,run['loop_id'],'run_paused','Paused at a step boundary',run_id=run['id'],
                        data={'next_step':run['next_step']})
        return {**reply, 'control':'pause'}
    step = run['steps'][run['next_step']]
    await db.execute('''UPDATE runs SET step_in_flight=next_step,step_started_at=now(),status='running',
        control_version=control_version+1 WHERE id=%s''', (run['id'],))
    await event(db,run['loop_id'],'step_reserved','Authored step dispatched once',run_id=run['id'],
                data={'step_id':step['id'],'step_index':run['next_step'],'action':step['action']})
    return {**reply, 'control':'run', 'step':step}


@app.post('/api/worker/poll')
async def worker_poll(body: WorkerPoll, request: Request):
    async with database() as db:
        if body.run_id is not None:
            worker = await worker_authorize(request, db)
            return await claim_authored_step(db,worker,body)
        # Lock the target and real actor, then actor session, then worker/run.
        # Revocation/reset/logout cannot commit between validation and dispatch.
        worker = await worker_authorize(request, db, lock=False)
        pending = await (await db.execute("SELECT * FROM runs WHERE worker_id=%s AND status='queued'", (worker['id'],))).fetchone()
        principal = None
        if pending and pending['request_session_hash']:
            actor = await session_identity(db, pending['request_session_hash'], lock=True)
            if actor:
                try: principal = await target_context(db, actor, pending['company_id'], pending['initiated_by'])
                except HTTPException: pass
        worker = await worker_authorize(request, db)
        await release_expired_control(db, worker['id'])
        worker = await (await db.execute('UPDATE workers SET last_seen=now(),revision=%s WHERE id=%s RETURNING *', (body.revision,worker['id']))).fetchone()
        if not pending: return {'run': None}
        # A new reservation waits for the next poll; it was not authorized above.
        run = await (await db.execute("""SELECT r.*,l.instruction,l.status AS loop_status
            FROM runs r JOIN loops l ON l.id=r.loop_id AND l.company_id=r.company_id
            WHERE r.id=%s AND r.worker_id=%s AND r.company_id=%s AND r.status='queued' FOR UPDATE OF r""",
            (pending['id'],worker['id'],worker['company_id']))).fetchone()
        if not run: return {'run': None}
        authorized = (principal and principal['target_verified']
                      and run['request_session_hash'] == principal['session_hash']
                      and run['initiated_by'] == principal['target_user_id'] == worker['assigned_user_id']
                      and run['company_id'] == principal['company_id'] == worker['company_id'])
        if not authorized or not run['dispatch_before'] or run['dispatch_before'] <= datetime.now(timezone.utc):
            await interrupt_queued_runs(db, 'Start expired or authorization is no longer valid. Start again when ready.',
                                        clause='id=%s AND company_id=%s', params=(run['id'],worker['company_id']))
            return {'run': None}
        if run['loop_status'] == 'error':
            await interrupt_queued_runs(db, 'Generation failed before dispatch. No actions were sent.',
                                        clause='id=%s AND company_id=%s', params=(run['id'],worker['company_id']))
            return {'run': None}
        if run['steps'] is not None and 'manual_actions_v1' not in body.capabilities:
            await interrupt_queued_runs(db, 'This worker needs the manual workbench update before it can execute authored steps. No actions were sent.',
                                        clause='id=%s AND company_id=%s', params=(run['id'],worker['company_id']))
            return {'run': None}
        if worker['active_run'] or worker['mode'] != 'agent' or run['loop_status'] != 'ready' or not run['instruction']:
            return {'run': None}
        updated = await (await db.execute("UPDATE runs SET status='running',started_at=now(),revision=%s,request_session_hash=NULL,control_version=control_version+1 WHERE id=%s AND company_id=%s RETURNING *", (body.revision,run['id'],worker['company_id']))).fetchone()
        run.update(updated)
        await db.execute('UPDATE workers SET active_run=%s WHERE id=%s', (run['id'],worker['id']))
        await event(db, run['loop_id'], 'delivery_sent', 'Persisted instruction dispatched to the assigned worker',
                    run_id=run['id'], data={'revision': body.revision, 'worker': worker['id'], 'actor_user_id': str(principal['user_id'])})
        return {'run': public_run(run)}


@app.post('/api/worker/heartbeat')
async def worker_heartbeat(request: Request):
    async with database() as db:
        worker = await worker_authorize(request, db)
        await db.execute('UPDATE workers SET last_seen=now() WHERE id=%s',(worker['id'],))
    return {'ok':True, 'worker_id':worker['id'], 'company_id':worker['company_id'],
            'assigned_user_id':worker['assigned_user_id'], 'environment':os.environ['APP_ENV']}


@app.post('/api/worker/deployment')
async def worker_deployment(body: WorkerDeployment, request: Request):
    if body.phase not in ('begin', 'finish'): raise HTTPException(400, 'Invalid deployment phase')
    async with database() as db:
        worker = await worker_authorize(request, db)
        if (body.expected_worker_id != worker['id'] or body.environment != os.environ['APP_ENV']
                or body.company_id != worker['company_id'] or body.assigned_user_id != worker['assigned_user_id']):
            raise HTTPException(409, 'Deployment worker or environment does not match the credential')
        await release_expired_queue(db, worker['id'])
        active = await (await db.execute("SELECT id FROM runs WHERE worker_id=%s AND status IN ('queued','running','paused')", (worker['id'],))).fetchone()
        if active or worker['active_run'] or worker['mode'] == 'human':
            raise HTTPException(409, 'Desktop must be idle under agent control before deployment')
        if body.phase == 'finish' and worker['mode'] != 'maintenance': raise HTTPException(409, 'Desktop is not in maintenance')
        mode = 'maintenance' if body.phase == 'begin' else 'agent'
        await db.execute('UPDATE workers SET mode=%s,human_session=NULL,revision=COALESCE(%s,revision) WHERE id=%s',
                         (mode, body.revision if body.phase == 'finish' else None, worker['id']))
    return {'worker_id': worker['id'], 'company_id': worker['company_id'],
            'assigned_user_id': worker['assigned_user_id'], 'environment': os.environ['APP_ENV'], 'mode': mode}


@app.post('/api/worker/recover')
async def worker_recover(request: Request):
    """Called once after the worker acquires its guest-wide process lock."""
    async with database() as db:
        worker = await worker_authorize(request, db)
        await interrupt_queued_runs(db, 'Worker restarted before dispatch. Start again when ready.',
                                    clause='worker_id=%s AND company_id=%s', params=(worker['id'],worker['company_id']))
        rows=await (await db.execute("""UPDATE runs r SET status='interrupted',finished_at=now(),
            error='Worker restarted or delivery was interrupted. Actions were not replayed.' FROM loops l
            WHERE r.loop_id=l.id AND r.worker_id=%s AND r.initiated_by=%s AND l.company_id=%s
            AND r.status IN ('running','paused') RETURNING r.id,r.loop_id""",
            (worker['id'],worker['assigned_user_id'],worker['company_id']))).fetchall()
        for row in rows:
            await event(db,row['loop_id'],'interrupted','Worker restarted; inspect the desktop before a new run. No actions replayed.',run_id=row['id'])
        await db.execute('UPDATE workers SET active_run=NULL,last_seen=now() WHERE id=%s',(worker['id'],))
    return {'ok':True}


async def owned_run(db, identity, worker, *, active=True):
    run = await (await db.execute('''SELECT r.* FROM runs r JOIN loops l ON l.id=r.loop_id
        WHERE r.id=%s AND r.worker_id=%s AND r.initiated_by=%s AND l.company_id=%s'''
        + (" AND r.status IN ('running','paused')" if active else '') + ' FOR UPDATE OF r',
        (identity,worker['id'],worker['assigned_user_id'],worker['company_id']))).fetchone()
    if not run: raise HTTPException(409,'Run is not owned by this active worker')
    return run


@app.post('/api/worker/runs/{identity}/events')
async def worker_event(identity: UUID, body: WorkerEvent, request: Request):
    async with database() as db:
        worker = await worker_authorize(request, db)
        run = await owned_run(db,identity,worker)
        previous = await (await db.execute('SELECT run_id FROM events WHERE id=%s', (body.id,))).fetchone()
        if previous:
            if previous['run_id'] != identity: raise HTTPException(409, 'Event identity belongs to another run')
            return {'ok':True}
        if body.stage == 'step_trace':
            records = body.data.get('events')
            index = body.data.get('step_index')
            if (run['steps'] is None or type(index) is not int or index != run['step_in_flight']
                    or not 0 <= index < len(run['steps']) or body.data.get('step_id') != run['steps'][index]['id']):
                raise HTTPException(409, 'Trace does not match the dispatched step')
            if (not isinstance(records, list) or any(not isinstance(record,dict) for record in records)
                    or len(json.dumps(body.data,ensure_ascii=False).encode('utf-8')) > 2*1024*1024):
                raise HTTPException(422, 'Step trace must contain bounded existing report records')
        if body.stage == 'step_finished':
            index = body.data.get('step_index')
            if (run['steps'] is None or type(index) is not int or index != run['step_in_flight']
                    or index != run['next_step'] or not 0 <= index < len(run['steps'])
                    or body.data.get('step_id') != run['steps'][index]['id']):
                raise HTTPException(409, 'Step completion does not match the dispatched step')
            status = body.data.get('status')
            if status not in ('succeeded','error') or not isinstance(body.data.get('result'), dict):
                raise HTTPException(422, 'Step completion requires a result and succeeded or error status')
            finished = datetime.now(timezone.utc)
            outcome = {key:body.data[key] for key in ('step_id','step_index','status','result','error','duration_ms','started_at','finished_at') if key in body.data}
            for key in ('started_at','finished_at'):
                value = outcome.get(key)
                if value is not None:
                    try:
                        if not isinstance(value,str) or len(value)>64 or datetime.fromisoformat(value.replace('Z','+00:00')).utcoffset() is None:
                            raise ValueError('A worker timestamp must include its timezone')
                    except ValueError as error:
                        raise HTTPException(422, 'Step timestamps must be ISO timestamps with a timezone') from error
            duration = outcome.get('duration_ms')
            if duration is not None and (type(duration) not in (int,float) or not math.isfinite(duration) or duration<0):
                raise HTTPException(422, 'Step duration must be a non-negative finite number')
            # Worker monotonic duration and wall-clock timestamps remain intact.
            # The reservation and arrival times have a separate server clock.
            outcome.update(server_started_at=run['step_started_at'].isoformat() if run['step_started_at'] else None,
                           server_finished_at=finished.isoformat())
            results = [*run['step_results'],outcome]
            next_step = index + (1 if status == 'succeeded' else 0)
            control = run['control']
            if status != 'succeeded': control = 'stop'
            elif control == 'run' and run['run_mode'] == 'step' and next_step < run_step_end(run): control = 'pause'
            state = 'paused' if control == 'pause' and next_step < run_step_end(run) else 'running'
            await db.execute('''UPDATE runs SET next_step=%s,step_in_flight=NULL,step_started_at=NULL,
                step_results=%s,control=%s,status=%s,control_version=control_version+1 WHERE id=%s''',
                (next_step,Jsonb(results),control,state,identity))
        await event(db,run['loop_id'],body.stage,body.message,run_id=identity,data=body.data,event_id=body.id)
    return {'ok':True}


@app.post('/api/worker/runs/{identity}/finish')
async def worker_finish(identity: UUID, body: WorkerResult, request: Request):
    if body.status not in ('succeeded','error','blocked','interrupted','unverified'): raise HTTPException(400,'Invalid run status')
    async with database() as db:
        worker = await worker_authorize(request, db)
        run = await owned_run(db,identity,worker)
        if body.status == 'succeeded':
            if run['steps'] is None:
                if body.result.get('verified') is not True: raise HTTPException(400,'Explicit task verification evidence is required')
            elif (run['next_step'] != run_step_end(run) or run['step_in_flight'] is not None
                  or [step['step_index'] for step in run['step_results']] != list(range(run['start_step'], run_step_end(run)))
                  or any(step['status'] != 'succeeded' for step in run['step_results'])):
                raise HTTPException(409, 'Every selected step must finish successfully before completing the run')
        result = {**(run.get('result') or {}), **body.result}
        # Only the authenticated archive endpoint owns durable storage metadata.
        if (run.get('result') or {}).get('artifacts'): result['artifacts'] = run['result']['artifacts']
        else: result.pop('artifacts', None)
        await db.execute('UPDATE runs SET status=%s,finished_at=now(),error=%s,result=%s,control_version=control_version+1 WHERE id=%s',(body.status,body.error,Jsonb(result),identity))
        await db.execute('UPDATE workers SET active_run=NULL,last_seen=now() WHERE id=%s',(worker['id'],))
        await event(db,run['loop_id'],'observed_result' if body.status=='succeeded' else body.status,body.error or body.result.get('reason') or 'Worker finished; inspect the evidence and reported verification state.',run_id=identity,data=body.result)
    return {'ok':True}


@app.post('/api/desktop/connection')
async def connect_desktop(request: Request):
    async with database() as db:
        identity = await authorize(request, db)
        worker = await assigned_worker(db, identity)
    try:
        async with asyncio.timeout(25):
            async with desktop_connection(worker, os.environ['APP_ENV']) as (signed, credential, url):
                async with database() as db:
                    # Fleet calls do not hold database locks. Recheck the exact
                    # session/assignment after issuance and before publication.
                    current = await authorize(request, db)
                    assigned = await assigned_worker(db, current)
                    keys = ('id', 'company_id', 'assigned_user_id', 'fleet_namespace', 'fleet_claim')
                    if (current['user_id'] != identity['user_id']
                            or current['session_hash'] != identity['session_hash']
                            or any(assigned[key] != worker[key] for key in keys)):
                        raise HTTPException(409, 'Desktop assignment changed; reconnect')
                    # A selected company/user change invalidates existing guest
                    # connections for that session, including already-open WSs.
                    await db.execute('''DELETE FROM desktop_connections WHERE session_hash=%s
                        AND (worker_id<>%s OR company_id<>%s OR assigned_user_id<>%s
                             OR fleet_namespace<>%s OR fleet_claim<>%s)''',
                        (identity['session_hash'], worker['id'], worker['company_id'], worker['assigned_user_id'],
                         worker['fleet_namespace'], worker['fleet_claim']))
                    await db.execute('''DELETE FROM desktop_connections d USING sessions s
                        WHERE d.session_hash=s.token_hash AND s.expires_at<=now()''')
                    await db.execute('''INSERT INTO desktop_connections(token_hash,session_hash,worker_id,
                        company_id,assigned_user_id,fleet_namespace,fleet_claim,admission_expires_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (token_hash(credential), identity['session_hash'], worker['id'], worker['company_id'],
                         worker['assigned_user_id'], worker['fleet_namespace'], worker['fleet_claim'], signed.expires_at))
                return {'url': url, 'expires_at': signed.expires_at}
    except HTTPException:
        raise
    except Exception as error:
        # SDK and network exceptions may embed credentials. Only record type.
        logger.warning('desktop credential issuance failed error=%s', type(error).__name__)
        raise HTTPException(503, 'Desktop connection is unavailable; reconnecting automatically') from None


async def desktop_policy_worker(db, websocket, credential_hash, *, admission=False):
    binding = await (await db.execute('SELECT * FROM desktop_connections WHERE token_hash=%s',
                                     (credential_hash,))).fetchone()
    if not binding or (admission and binding['admission_expires_at'] <= datetime.now(timezone.utc)):
        raise PermissionError('Desktop connection is no longer authorized')
    identity = await session_identity(db, binding['session_hash'], lock=True)
    if identity is None:
        raise PermissionError('Desktop session is no longer authorized')
    identity = await target_context(db, identity, binding['company_id'], binding['assigned_user_id'])
    # Match the dedicated worker credential as well as the session's target.
    # Lock order stays user -> session -> worker, as for normal browser input.
    worker = await worker_authorize(websocket, db, lock=False)
    keys = ('company_id', 'assigned_user_id', 'fleet_namespace', 'fleet_claim')
    if worker['id'] != binding['worker_id'] or any(worker[key] != binding[key] for key in keys):
        raise PermissionError('Desktop worker assignment changed')
    worker = await (await db.execute('''SELECT * FROM workers WHERE id=%s AND credential_hash=%s
        AND company_id=%s AND assigned_user_id=%s AND fleet_namespace=%s AND fleet_claim=%s
        AND disabled_at IS NULL FOR SHARE''',
        (worker['id'], worker['credential_hash'], binding['company_id'], binding['assigned_user_id'],
         binding['fleet_namespace'], binding['fleet_claim']))).fetchone()
    # Read expiry after taking the existing session lock. A shortened session
    # must not inherit its previous (longer) lifetime in the guest's local gate.
    live = await (await db.execute('''SELECT d.token_hash,s.expires_at FROM desktop_connections d
        JOIN sessions s ON s.token_hash=d.session_hash WHERE d.token_hash=%s FOR SHARE OF d''',
        (credential_hash,))).fetchone()
    if worker is None or live is None:
        raise PermissionError('Desktop access changed')
    allowed = (worker['mode'] == 'human' and worker['human_session'] == identity['session_hash']
               and worker['active_run'] is None)
    scopes = {'connection:' + credential_hash, 'session:' + binding['session_hash'],
              'worker:' + worker['id'], 'user:' + str(identity['user_id']),
              'user:' + str(worker['assigned_user_id']),
              'company:' + str(identity['home_company_id']), 'company:' + str(worker['company_id'])}
    return worker, allowed, live['expires_at'], scopes, binding['session_hash'] if allowed else None


@app.websocket('/api/worker/desktop-policy')
async def desktop_policy(websocket: WebSocket):
    """Admission/daily validation, with immediate committed-change invalidation.

    Ordinary input uses the guest's local gate. No database transaction remains
    open while this policy socket is idle, and no per-input API exchange exists.
    """
    notifications = desktop_policy_notifications
    changed = None
    tasks = []
    try:
        if notifications is None:
            raise PermissionError('Desktop authorization notifications are unavailable')
        changed = notifications.subscribe()

        async def communicate():
            # The outer timeout closes an idle or blocked socket at session
            # expiry, independently of the next requested daily validation.
            async with asyncio.timeout(None) as authorization:
                async def validate(digest, *, admission=False):
                    checked_at = asyncio.get_running_loop().time()
                    async with asyncio.timeout(5):
                        async with database() as db:
                            worker, allowed, expires_at, scopes, control_session = await desktop_policy_worker(
                                db, websocket, digest, admission=admission)
                    notifications.bind(changed, scopes)
                    expires_in_seconds = (expires_at - datetime.now(timezone.utc)).total_seconds()
                    if expires_in_seconds <= 0:
                        raise PermissionError('Desktop session expired')
                    # Daily refresh has a small exchange margin; session expiry
                    # itself is never extended. Transport pings do not query DB.
                    authorization.reschedule(min(asyncio.get_running_loop().time() + expires_in_seconds,
                                                 checked_at + 86400 + 7))
                    response = {'valid': True, 'allowed': allowed,
                                'expires_in_seconds': expires_in_seconds,
                                'control_session': control_session}
                    if admission:
                        response.update(worker_id=worker['id'], company_id=str(worker['company_id']),
                                        assigned_user_id=str(worker['assigned_user_id']), environment=os.environ['APP_ENV'])
                    await asyncio.wait_for(websocket.send_json(response), 2)

                async with asyncio.timeout(5):
                    async with database() as db:
                        await worker_authorize(websocket, db, lock=False)
                    await websocket.accept()
                    initial = await websocket.receive_json()
                    credential = initial.get('credential') if isinstance(initial, dict) else None
                    if (not isinstance(initial, dict) or initial.get('protocol') != 'ctrlloop-direct-v2'
                            or not isinstance(credential, str) or not 20 <= len(credential) <= 4096):
                        raise PermissionError('Invalid desktop policy admission')
                    digest = token_hash(credential)
                    del credential, initial
                    await validate(digest, admission=True)
                while True:
                    message = await websocket.receive_json()
                    if message != {'op': 'check'}:
                        raise ValueError('Invalid desktop policy operation')
                    await validate(digest)

        tasks = [asyncio.create_task(communicate()), asyncio.create_task(changed.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except Exception as error:
        logger.info('desktop policy closed error=%s', type(error).__name__)
    finally:
        for task in tasks:
            task.cancel()
        # Close promptly on a notification even if an in-flight DB query needs
        # additional cancellation/rollback time. No old grant survives the gap.
        with suppress(RuntimeError, OSError, WebSocketDisconnect, TimeoutError):
            await asyncio.wait_for(websocket.close(code=1008, reason='Desktop permission changed; reconnect'), 2)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if changed is not None:
            notifications.unsubscribe(changed)


@app.websocket('/api/desktop')
async def retired_desktop_relay(websocket: WebSocket):
    # Old cached clients must upgrade; a fallback would put frames back on AWS.
    await websocket.close(code=1008, reason='Reload the viewer to use direct Fleet streaming')


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
            # Recheck after the upload body arrives and retain user/assignment
            # locks through storage, so revocation cannot commit during this write.
            worker = await worker_authorize(request, db)
            run = await owned_run(db, identity, worker, active=False)
            try: await asyncio.to_thread(store_archive, run['company_id'], identity, bytes(content))
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
            run = await (await db.execute('''SELECT r.*,jsonb_build_object('id',u.id,'email',u.email,'display_name',u.display_name) AS initiator
                FROM runs r JOIN loops l ON l.id=r.loop_id JOIN workers w ON w.id=r.worker_id
                LEFT JOIN users u ON u.id=r.initiated_by WHERE r.id=%s AND l.company_id=%s AND w.company_id=%s''',
                (identity,principal['company_id'],principal['company_id']))).fetchone()
            if not run: raise HTTPException(404, 'Run not found')
            loop_id = run['loop_id']
            run = public_run(run)
        loop = await (await db.execute('''SELECT l.*,jsonb_build_object('id',u.id,'email',u.email,'display_name',u.display_name) AS creator
            FROM loops l LEFT JOIN users u ON u.id=l.created_by WHERE l.id=%s AND l.company_id=%s''',
            (loop_id,principal['company_id']))).fetchone()
        if not loop: raise HTTPException(404, 'Request not found')
        events = await (await db.execute('SELECT * FROM events WHERE company_id=%s AND loop_id=%s AND (run_id IS NULL OR run_id=%s) ORDER BY created_at,id', (principal['company_id'],loop_id, None if loop_only else identity))).fetchall()
    trace_events = [record for item in events if item['stage'] == 'step_trace'
                    for record in item['data'].get('events',[]) if isinstance(record,dict)]
    events = [item for item in events if item['stage'] != 'step_trace']
    missing = []
    if loop['steps'] is None:
        if not any(item['stage'] == 'llm_request' for item in events): missing.append('Exact OpenAI request was not captured for this earlier request.')
        if not any(item['stage'] == 'llm_response' for item in events): missing.append('OpenAI response is unavailable; generation may predate capture or have been interrupted.')
    artifact = ((run or {}).get('result') or {}).get('artifacts')
    if run and not artifact: missing.append('Worker evidence archive has not been uploaded. Available step traces are included; screenshots and any uncaptured records remain unavailable.')
    if not artifact:
        missing.extend(dict.fromkeys(record.get('reason','A live step trace record was not captured.')
                                     for record in trace_events if record.get('event') == 'capture_gap'))
    if artifact: missing.extend(artifact.get('manifest', {}).get('missing_capture', []))
    if not run: missing.append('No desktop run belongs to this request-only report.')
    return {'loop': loop, 'run': run, 'events': events, 'artifact': artifact,
            'openai_cost_estimate': openai_cost_estimate(loop.get('llm_usage')),
            'missing_capture': missing, 'worker_events': trace_events,
            'timing_note': 'Server and worker timestamps are separate clock domains. Only durations measured on one machine are precise.',
            'cost_note': 'Jev input cost is a usage estimate at $0.042 per million input tokens; output is free. No provider invoice is attached.'}


async def report_content(record):
    if not record['artifact']: return None
    try: return await asyncio.to_thread(load_archive, record['run']['company_id'], record['run']['id'])
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
