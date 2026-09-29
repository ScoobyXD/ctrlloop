"""Personal credentials and the explicitly enabled, additive development migration.

Call migrate_legacy_identity inside a database transaction after schema.sql.
Provisioning commands share these helpers; plaintext credentials never enter rows.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
from uuid import uuid4


def normalize_username(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{1,63}', value):
        raise ValueError('Username must be 2–64 letters, numbers, dots, underscores or hyphens')
    return value


def token_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def password_hash(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 500:
        raise ValueError('Password must contain 12–500 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode('utf-8'), salt=salt, n=32768, r=8, p=1,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return 'scrypt$32768$8$1$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(digest).decode()


def verify_password(value, encoded):
    try:
        algorithm, n, r, p, salt, expected = encoded.split('$')
        if (algorithm, n, r, p) != ('scrypt', '32768', '8', '1'): return False
        digest = hashlib.scrypt(value.encode('utf-8'), salt=base64.b64decode(salt, validate=True),
                                n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(expected, validate=True))
    except (ValueError, TypeError, AttributeError):
        return False


async def create_member(db, workspace_id, username, display_name, role='member'):
    """Create an invitation, or add an existing activated identity to a workspace.

    The caller owns workspace authorization and the transaction. A pending
    identity's activation token can never be replaced by another workspace owner.
    """
    username = normalize_username(username)
    display_name = display_name.strip()
    if not display_name or len(display_name) > 100 or role not in ('owner', 'member'):
        raise ValueError('A display name (1–100 characters) and valid role are required')
    await db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('ctrlloop.username.' + username,))
    # Existing users are not modified. Keep activation/disable state stable
    # without upgrading the caller's shared identity lock (notably self-invites).
    user = await (await db.execute('SELECT * FROM users WHERE username=%s FOR SHARE', (username,))).fetchone()
    raw = None
    if user:
        existing = await (await db.execute('SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s',
                                          (workspace_id, user['id']))).fetchone()
        if existing: raise ValueError('This username already has a membership; use the membership controls')
        if not user['activated_at'] or user['disabled_at']:
            raise ValueError('This username cannot be invited; choose a different username')
    else:
        raw = secrets.token_urlsafe(32)
        user = await (await db.execute('''INSERT INTO users(id,username,display_name,activation_token_hash,activation_expires_at)
            VALUES(%s,%s,%s,%s,now()+interval '72 hours') RETURNING *''',
            (uuid4(), username, display_name, token_hash(raw)))).fetchone()
    await db.execute('INSERT INTO memberships(workspace_id,user_id,role) VALUES(%s,%s,%s)',
                     (workspace_id, user['id'], role))
    return {'member': {'user_id': user['id'], 'username': user['username'], 'display_name': user['display_name'],
                       'role': role, 'status': 'active' if user['activated_at'] else 'pending', 'worker': None},
            'activation_token': raw, 'activation_expires_at': user['activation_expires_at'] if raw else None}


async def enforce_identity_columns(db):
    """Validate historical ownership, then reject writes from the legacy app."""
    invalid = await (await db.execute('''SELECT
        EXISTS(SELECT 1 FROM sessions s WHERE NOT EXISTS(
            SELECT 1 FROM memberships m WHERE m.workspace_id=s.workspace_id AND m.user_id=s.user_id))
        OR EXISTS(SELECT 1 FROM loops l WHERE NOT EXISTS(
            SELECT 1 FROM memberships m WHERE m.workspace_id=l.workspace_id AND m.user_id=l.created_by))
        OR EXISTS(SELECT 1 FROM workers w WHERE NOT EXISTS(
            SELECT 1 FROM memberships m WHERE m.workspace_id=w.workspace_id AND m.user_id=w.assigned_user_id)
            OR NOT COALESCE(w.credential_hash ~ '^[0-9a-f]{64}$',false)
            OR NOT COALESCE(w.fleet_namespace ~ '^[a-z0-9][a-z0-9-]{0,199}$',false)
            OR NOT COALESCE(w.fleet_claim ~ '^[a-z0-9][a-z0-9-]{0,199}$',false))
        OR EXISTS(SELECT 1 FROM runs r JOIN loops l ON l.id=r.loop_id JOIN workers w ON w.id=r.worker_id
            WHERE r.initiated_by IS DISTINCT FROM w.assigned_user_id OR l.workspace_id IS DISTINCT FROM w.workspace_id
            OR NOT EXISTS(SELECT 1 FROM memberships m WHERE m.workspace_id=l.workspace_id AND m.user_id=r.initiated_by))
        AS present''')).fetchone()
    if invalid['present']:
        raise RuntimeError('Identity assignments are incomplete or inconsistent; administrator review is required')
    required = {'sessions': ('user_id', 'workspace_id'),
                'loops': ('workspace_id', 'created_by'), 'runs': ('initiated_by',),
                'workers': ('workspace_id', 'assigned_user_id', 'credential_hash', 'fleet_namespace', 'fleet_claim')}
    for table, columns in required.items():
        known = await (await db.execute('''SELECT attname,attnotnull FROM pg_attribute
            WHERE attrelid=to_regclass(%s) AND attname=ANY(%s) AND NOT attisdropped''', (table,list(columns)))).fetchall()
        if len(known) != len(columns): raise RuntimeError('Required identity schema is missing')
        pending = [row['attname'] for row in known if not row['attnotnull']]
        if pending:
            # Table/column identifiers originate only in the constant allowlist.
            await db.execute('ALTER TABLE '+table+' '+', '.join('ALTER COLUMN '+column+' SET NOT NULL' for column in pending))


async def migrate_legacy_identity(db, settings=None):
    """Bind legacy rows once. Never reset a provisioned password or assignment."""
    settings = os.environ if settings is None else settings
    await db.execute("SELECT pg_advisory_xact_lock(hashtext('ctrlloop.identity.migration'))")
    legacy = await (await db.execute('''SELECT
        EXISTS(SELECT 1 FROM loops WHERE workspace_id IS NULL OR created_by IS NULL)
        OR EXISTS(SELECT 1 FROM runs WHERE initiated_by IS NULL)
        OR EXISTS(SELECT 1 FROM workers WHERE workspace_id IS NULL)
        OR EXISTS(SELECT 1 FROM sessions WHERE user_id IS NULL OR workspace_id IS NULL) AS present''')).fetchone()
    enabled = settings.get('BOOTSTRAP_LEGACY_DEV', '').lower() == 'true'
    if not enabled:
        if legacy['present']:
            raise RuntimeError('Legacy identity rows require the explicit development bootstrap migration')
        await enforce_identity_columns(db)
        return None
    if settings.get('APP_ENV') != 'dev':
        raise RuntimeError('BOOTSTRAP_LEGACY_DEV is allowed only with APP_ENV=dev')
    username = normalize_username(settings.get('BOOTSTRAP_OWNER_USERNAME', 'owner'))
    slug = settings.get('BOOTSTRAP_WORKSPACE_SLUG', 'ctrlloop-development')
    workspace = await (await db.execute('SELECT * FROM workspaces WHERE slug=%s', (slug,))).fetchone()
    # After the migration, startup does not need or consult the old shared secrets.
    if workspace and not legacy['present']:
        await enforce_identity_columns(db)
        return {'workspace_id': workspace['id']}
    for key in ('APP_PASSWORD', 'WORKER_TOKEN', 'WORKER_ID', 'FLEET_NAMESPACE', 'FLEET_CLAIM'):
        if not settings.get(key): raise RuntimeError(f'Missing development bootstrap setting: {key}')
    worker_id = settings['WORKER_ID']
    if settings.get('FLEET_SANDBOX', worker_id) != worker_id:
        raise RuntimeError('Development worker ID must be its Fleet sandbox ID')
    other_workers = await (await db.execute('SELECT id FROM workers WHERE workspace_id IS NULL AND id<>%s', (worker_id,))).fetchall()
    if other_workers:
        raise RuntimeError('Legacy migration requires an explicit assignment for every existing worker')
    if workspace is None:
        workspace = await (await db.execute('INSERT INTO workspaces(id,slug,name) VALUES(%s,%s,%s) RETURNING *',
            (uuid4(), slug, settings.get('BOOTSTRAP_WORKSPACE_NAME', 'CTRL Loop Development')))).fetchone()
    owner = await (await db.execute('SELECT * FROM users WHERE username=%s', (username,))).fetchone()
    if owner is None:
        encoded = await asyncio.to_thread(password_hash, settings['APP_PASSWORD'])
        owner = await (await db.execute('''INSERT INTO users(id,username,display_name,password_hash,activated_at)
            VALUES(%s,%s,%s,%s,now()) RETURNING *''',
            (uuid4(), username, settings.get('BOOTSTRAP_OWNER_DISPLAY_NAME', 'Owner'), encoded))).fetchone()
        await db.execute("INSERT INTO memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
                         (workspace['id'], owner['id']))
    else:
        membership = await (await db.execute("SELECT 1 FROM memberships WHERE workspace_id=%s AND user_id=%s AND role='owner' AND revoked_at IS NULL",
            (workspace['id'], owner['id']))).fetchone()
        if not membership or not owner['activated_at'] or owner['disabled_at']:
            raise RuntimeError('Existing bootstrap owner must already own the development workspace')
    # Supplying every required field also makes an explicit bootstrap safe if
    # constraints already exist; PostgreSQL checks NOT NULL before ON CONFLICT.
    await db.execute('''INSERT INTO workers(id,workspace_id,assigned_user_id,credential_hash,fleet_namespace,fleet_claim)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
        (worker_id,workspace['id'],owner['id'],token_hash(settings['WORKER_TOKEN']),settings['FLEET_NAMESPACE'],settings['FLEET_CLAIM']))
    await db.execute('''UPDATE workers SET workspace_id=%s,assigned_user_id=%s,credential_hash=%s,
        fleet_namespace=%s,fleet_claim=%s WHERE id=%s AND workspace_id IS NULL''',
        (workspace['id'], owner['id'], token_hash(settings['WORKER_TOKEN']), settings['FLEET_NAMESPACE'],
         settings['FLEET_CLAIM'], worker_id))
    await db.execute('UPDATE sessions SET user_id=%s,workspace_id=%s WHERE user_id IS NULL AND workspace_id IS NULL',
                     (owner['id'], workspace['id']))
    await db.execute('UPDATE loops SET workspace_id=%s,created_by=%s WHERE workspace_id IS NULL AND created_by IS NULL',
                     (workspace['id'], owner['id']))
    await db.execute('UPDATE runs SET initiated_by=%s WHERE initiated_by IS NULL', (owner['id'],))
    remaining = await (await db.execute('''SELECT
        EXISTS(SELECT 1 FROM loops WHERE workspace_id IS NULL OR created_by IS NULL)
        OR EXISTS(SELECT 1 FROM sessions WHERE user_id IS NULL OR workspace_id IS NULL) AS present''')).fetchone()
    if remaining['present']:
        raise RuntimeError('Partially assigned legacy rows require explicit administrator review')
    await enforce_identity_columns(db)
    return {'workspace_id': workspace['id'], 'owner_id': owner['id'], 'worker_id': worker_id}
