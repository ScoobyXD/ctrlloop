"""Explicit, transactional upgrade from legacy memberships to one home company.

Ordinary startup initializes an empty database, but never migrates populated
legacy data. Run the management CLI's preflight-companies command, supply its
required ownership/email mappings, then run migrate-companies while the old app
and workers are stopped. Every original entity ID and personal password hash is
retained; old sessions and activation links expire. Historical memberships are
preserved in company_migration_audit, which is not an authorization model.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .identity import normalize_email, token_hash


SCHEMA = Path(__file__).with_name('schema.sql')
LEGACY_TABLES = ('workspaces', 'users', 'memberships', 'sessions', 'workers', 'loops', 'runs', 'events')
COMPANY_TABLES = ('users', 'sessions', 'workers', 'loops', 'runs', 'events', 'auth_tokens', 'company_migration_audit')
MAPPING_FORMAT = {
    'users': {'LEGACY_USER_UUID': {'email': 'verified-later@example.com', 'company_id': 'COMPANY_UUID', 'role': 'user|company_admin (optional)'}},
    'loops': {'LOOP_UUID': {'company_id': 'COMPANY_UUID', 'created_by': 'USER_UUID'}},
    'workers': {'WORKER_ID': {'company_id': 'COMPANY_UUID', 'assigned_user_id': 'USER_UUID', 'credential_token_env': 'ENV_NAME (only if replacing a missing credential)', 'fleet_namespace': 'optional', 'fleet_claim': 'optional'}},
    'runs': {'RUN_UUID': {'initiated_by': 'USER_UUID'}},
    'unowned_sessions': {'company_id': 'COMPANY_UUID', 'user_id': 'USER_UUID'},
    'create_companies': [{'id': 'UUID', 'slug': 'company-slug', 'name': 'Company name'}],
    'create_users': [{'id': 'UUID', 'company_id': 'COMPANY_UUID', 'email': 'person@example.com', 'display_name': 'Name', 'role': 'company_admin', 'password_env': 'OPTIONAL_PERSONAL_PASSWORD_ENV'}],
}


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _email(value):
    if not isinstance(value, str):
        return None
    try:
        return normalize_email(value)
    except ValueError:
        return None


def _json(value):
    return json.loads(json.dumps(value, default=str))


def _mapping(settings):
    location = settings.get('COMPANY_MIGRATION_MAP')
    if not location:
        return {}
    try:
        value = json.loads(Path(location).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('COMPANY_MIGRATION_MAP must point to a readable JSON mapping file') from exc
    allowed = set(MAPPING_FORMAT)
    if not isinstance(value, dict) or set(value) - allowed:
        raise RuntimeError('Company migration mapping must be an object using only the documented mapping sections')
    for key in ('users', 'loops', 'workers', 'runs', 'unowned_sessions'):
        if key in value and not isinstance(value[key], dict):
            raise RuntimeError(f'Migration mapping section {key} must be an object')
    for key in ('create_companies', 'create_users'):
        if key in value and (not isinstance(value[key], list) or any(not isinstance(row, dict) for row in value[key])):
            raise RuntimeError(f'Migration mapping section {key} must be an array of objects')
    for key in ('users', 'loops', 'workers', 'runs'):
        if any(not isinstance(row, dict) for row in value.get(key, {}).values()):
            raise RuntimeError(f'Migration mapping section {key} must map IDs to objects')
    fields = {
        'users': {'email', 'company_id', 'role'},
        'loops': {'company_id', 'created_by'},
        'workers': {'company_id', 'assigned_user_id', 'credential_token_env', 'fleet_namespace', 'fleet_claim'},
        'runs': {'initiated_by'},
        'unowned_sessions': {'company_id', 'user_id'},
        'create_companies': {'id', 'slug', 'name'},
        'create_users': {'id', 'company_id', 'email', 'display_name', 'role', 'password_env'},
    }
    for key, allowed_fields in fields.items():
        rows = value.get(key, []) if key.startswith('create_') else ([value[key]] if key == 'unowned_sessions' and key in value else value.get(key, {}).values())
        for row in rows:
            if set(row) - allowed_fields:
                raise RuntimeError(f'Migration mapping section {key} contains unsupported fields; use the documented mapping format')
            if any(not isinstance(item, str) for item in row.values()):
                raise RuntimeError(f'Migration mapping section {key} fields must contain strings')
    return value


async def _tables(db):
    rows = await (await db.execute('''SELECT table_name FROM information_schema.tables
        WHERE table_schema=current_schema() AND table_type='BASE TABLE' ''')).fetchall()
    return {row['table_name'] for row in rows}


async def _rows(db, table):
    return await (await db.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table)))).fetchall()


async def _current_errors(db, tables):
    errors = []
    for table in ('companies',) + COMPANY_TABLES:
        if table not in tables:
            errors.append({'table': table, 'problem': 'Missing canonical company table; repair schema before startup'})
            continue
        if table == 'companies':
            continue
        row = await (await db.execute('''SELECT a.attnotnull,
            EXISTS(SELECT 1 FROM pg_constraint c WHERE c.conrelid=a.attrelid AND c.contype='f' AND c.convalidated
                AND c.confrelid='companies'::regclass AND c.conkey=ARRAY[a.attnum]::smallint[]) AS company_fk,
            EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=a.attrelid AND i.indisvalid
                AND i.indkey[0]=a.attnum) AS company_index
            FROM pg_attribute a WHERE a.attrelid=to_regclass(%s) AND a.attname='company_id' AND NOT a.attisdropped''', (table,))).fetchone()
        if not row or not all(row.values()):
            errors.append({'table': table, 'problem': 'company_id must be non-null with a company foreign key and leading index'})
    # A mere company_id column is insufficient: every relational path must
    # carry that same tenant. Detect an incomplete/manual schema conversion.
    required_relationships = {
        'sessions': [(('company_id', 'user_id'), 'users', ('company_id', 'id'))],
        'workers': [(('company_id', 'assigned_user_id'), 'users', ('company_id', 'id')),
                    (('company_id', 'active_run', 'id'), 'runs', ('company_id', 'id', 'worker_id'))],
        'loops': [(('company_id', 'created_by'), 'users', ('company_id', 'id'))],
        'runs': [(('company_id', 'loop_id'), 'loops', ('company_id', 'id')),
                 (('company_id', 'initiated_by'), 'users', ('company_id', 'id')),
                 (('company_id', 'worker_id', 'initiated_by'), 'workers', ('company_id', 'id', 'assigned_user_id'))],
        'events': [(('company_id', 'loop_id'), 'loops', ('company_id', 'id')),
                   (('company_id', 'run_id', 'loop_id'), 'runs', ('company_id', 'id', 'loop_id'))],
        'auth_tokens': [(('company_id', 'user_id'), 'users', ('company_id', 'id'))],
    }
    for table, required in required_relationships.items():
        if table not in tables:
            continue
        rows = await (await db.execute('''SELECT target.relname AS target,
            ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(num,ord)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY k.ord) AS own_columns,
            ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(num,ord)
                JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num ORDER BY k.ord) AS target_columns
            FROM pg_constraint c JOIN pg_class target ON target.oid=c.confrelid
            WHERE c.conrelid=to_regclass(%s) AND c.contype='f' AND c.convalidated''', (table,))).fetchall()
        present = {(tuple(row['own_columns']), row['target'], tuple(row['target_columns'])) for row in rows}
        if any(item not in present for item in required):
            errors.append({'table': table, 'problem': 'Required same-company foreign key is missing; repair the canonical schema before startup'})
    return errors


async def _plan(db, settings):
    tables = await _tables(db)
    if 'companies' in tables:
        errors = await _current_errors(db, tables)
        if 'workspaces' in tables or 'memberships' in tables:
            errors.append({'table': 'companies', 'problem': 'Both legacy and current identity models exist; operator review is required'})
        return {'state': 'current', 'ready': not errors, 'errors': errors, 'counts': {}}, None
    present = [table for table in LEGACY_TABLES if table in tables]
    if not present:
        return {'state': 'empty', 'ready': True, 'errors': [], 'counts': {}}, None
    mapping = _mapping(settings)
    source = {table: await _rows(db, table) if table in tables else [] for table in LEGACY_TABLES}
    errors = []

    def problem(table, identifier, message):
        item = {'table': table, 'problem': message}
        if identifier is not None:
            item['id'] = str(identifier)
        errors.append(item)

    now = datetime.now(timezone.utc)
    companies = {str(row['id']): dict(row) for row in source['workspaces']}
    for row in mapping.get('create_companies', []):
        identifier = _uuid(row.get('id'))
        if not identifier or identifier in companies or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', str(row.get('slug', ''))) or not str(row.get('name', '')).strip():
            problem('companies', row.get('id'), 'New company requires a unique UUID, slug and name')
            continue
        companies[identifier] = dict(row, id=identifier, created_at=now)
    slugs = [row.get('slug') for row in companies.values()]
    if len(set(slugs)) != len(slugs):
        problem('companies', None, 'Company slugs must be unique')
    memberships = {}
    for row in source['memberships']:
        memberships.setdefault(str(row['user_id']), []).append(row)
        if str(row['workspace_id']) not in companies:
            problem('memberships', row['user_id'], 'Membership company is missing')
    users = {}
    original_users = {str(row['id']) for row in source['users']}
    for row in source['users']:
        identifier = str(row['id'])
        choice = mapping.get('users', {}).get(identifier, {})
        member_rows = memberships.get(identifier, [])
        active = [item for item in member_rows if item.get('revoked_at') is None]
        company_id = _uuid(choice.get('company_id'))
        # Only a single historical company is unambiguous. Multiple historical
        # memberships require an explicit home even when only one is active.
        if not company_id and len(member_rows) == 1:
            company_id = str(member_rows[0]['workspace_id'])
        if not company_id or company_id not in companies:
            problem('users', identifier, 'Set users[id].company_id explicitly to one existing company; all old memberships remain in audit history')
        email = _email(choice.get('email'))
        if not email:
            problem('users', identifier, 'Set users[id].email to the account email; legacy usernames are never guessed as addresses')
        selected = next((item for item in member_rows if str(item['workspace_id']) == company_id), None)
        role = choice.get('role', 'company_admin' if selected and selected['role'] == 'owner' else 'user')
        if role not in ('user', 'company_admin'):
            problem('users', identifier, 'Mapped role must be user or company_admin')
        disabled = row.get('disabled_at')
        # Revoked memberships must not become active through migration.
        if selected and selected.get('revoked_at'):
            disabled = disabled or selected['revoked_at']
        elif not selected and not active:
            disabled = disabled or now
        users[identifier] = dict(row, company_id=company_id, email=email, role=role,
                                 disabled_at=disabled, is_devadmin=False, email_verified_at=None)
    for row in mapping.get('create_users', []):
        identifier = _uuid(row.get('id'))
        company_id = _uuid(row.get('company_id'))
        email = _email(row.get('email'))
        role = row.get('role', 'user')
        if not identifier or identifier in users or company_id not in companies or not email or role not in ('user', 'company_admin') or not str(row.get('display_name', '')).strip():
            problem('users', row.get('id'), 'New user requires unique UUID, company_id, email, display_name and a valid role')
            continue
        if row.get('password_env') and not settings.get(row['password_env']):
            problem('users', identifier, 'The configured password_env environment variable is missing')
        users[identifier] = dict(row, id=identifier, company_id=company_id, email=email, role=role,
                                 password_hash=None, disabled_at=None, is_devadmin=False, email_verified_at=None, created_at=now)
    email_owners = {}
    for identifier, row in users.items():
        if row['email'] in email_owners:
            problem('users', identifier, 'Mapped email is shared by more than one account; reconcile accounts explicitly')
        email_owners[row['email']] = identifier
    for identifier in companies:
        if not any(row['company_id'] == identifier and row['role'] == 'company_admin'
                   and row['disabled_at'] is None for row in users.values()):
            problem('companies', identifier, 'Map or create at least one enabled company_admin in this company; an admin must verify email before enrollment can open')
    for key, originals in (('users', original_users), ('loops', {str(row['id']) for row in source['loops']}),
                           ('workers', {str(row['id']) for row in source['workers']}), ('runs', {str(row['id']) for row in source['runs']})):
        for identifier in mapping.get(key, {}):
            if identifier not in originals:
                problem(key, identifier, 'Mapping refers to an unknown existing ID (use create_users/create_companies to create new records)')

    def owner(table, row, user_column):
        identifier = str(row['id'])
        choice = mapping.get(table, {}).get(identifier, {})
        company_id = _uuid(choice.get('company_id', row.get('workspace_id')))
        user_id = _uuid(choice.get(user_column, row.get(user_column)))
        if company_id not in companies or user_id not in users or users[user_id]['company_id'] != company_id:
            problem(table, identifier, f'Explicitly map company_id and {user_column} to a user in that company')
        resolved = dict(row)
        resolved.update(choice)
        resolved.update(company_id=company_id)
        resolved[user_column] = user_id
        return resolved

    loops = {str(row['id']): owner('loops', row, 'created_by') for row in source['loops']}
    workers = {str(row['id']): owner('workers', row, 'assigned_user_id') for row in source['workers']}
    assignments, credentials = set(), set()
    for identifier, row in workers.items():
        token_env = row.get('credential_token_env')
        if token_env:
            token = settings.get(token_env)
            if not token:
                problem('workers', identifier, 'The configured credential_token_env environment variable is missing')
            else:
                row['credential_hash'] = token_hash(token)
        if not re.fullmatch(r'[0-9a-f]{64}', row.get('credential_hash') or ''):
            problem('workers', identifier, 'A worker credential is missing; set credential_token_env to its token environment variable')
        elif row['credential_hash'] in credentials:
            problem('workers', identifier, 'Worker credentials must be unique')
        credentials.add(row.get('credential_hash'))
        for field in ('fleet_namespace', 'fleet_claim'):
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,199}', row.get(field) or ''):
                problem('workers', identifier, f'Explicitly map the missing/invalid {field}')
        assignment = (row['company_id'], row['assigned_user_id'])
        if not row.get('disabled_at'):
            if assignment in assignments:
                problem('workers', identifier, 'More than one enabled worker is assigned to this user')
            assignments.add(assignment)
    runs = {}
    active_workers = set()
    for original in source['runs']:
        identifier = str(original['id'])
        choice = mapping.get('runs', {}).get(identifier, {})
        worker = workers.get(str(original['worker_id']))
        loop = loops.get(str(original['loop_id']))
        user_id = _uuid(choice.get('initiated_by', original.get('initiated_by') or (worker or {}).get('assigned_user_id')))
        company_id = loop['company_id'] if loop else None
        if not worker or not loop or worker['company_id'] != company_id or worker['assigned_user_id'] != user_id:
            problem('runs', identifier, 'Run loop, worker and initiated_by must have the same company; initiated_by must be the assigned worker user')
        if original['status'] in ('queued', 'running'):
            if original['worker_id'] in active_workers:
                problem('runs', identifier, 'Multiple queued/running runs use the same worker; reconcile their status first')
            active_workers.add(original['worker_id'])
        runs[identifier] = dict(original, company_id=company_id, initiated_by=user_id)
    for identifier, worker in workers.items():
        active = str(worker['active_run']) if worker.get('active_run') else None
        if active and (active not in runs or str(runs[active]['worker_id']) != identifier or runs[active]['company_id'] != worker['company_id']):
            problem('workers', identifier, 'active_run does not belong to this worker/company; reconcile the legacy active run first')
    for row in source['events']:
        loop = loops.get(str(row['loop_id']))
        run = runs.get(str(row['run_id'])) if row.get('run_id') else None
        if not loop or (row.get('run_id') and (not run or str(run['loop_id']) != str(row['loop_id']))):
            problem('events', row['id'], 'Event references a missing loop or a run belonging to another loop; reconcile the legacy row first')
    sessions = []
    for original in source['sessions']:
        user_id = _uuid(original.get('user_id'))
        if not user_id:
            user_id = _uuid(mapping.get('unowned_sessions', {}).get('user_id'))
            mapped_company = _uuid(mapping.get('unowned_sessions', {}).get('company_id'))
            if user_id not in users or users[user_id]['company_id'] != mapped_company:
                problem('sessions', None, 'Unowned sessions need an explicit unowned_sessions user_id/company_id mapping; tokens will be expired')
        if user_id not in users:
            problem('sessions', None, 'Session user is missing; supply an explicit unowned_sessions mapping')
            continue
        sessions.append(dict(original, user_id=user_id, company_id=users[user_id]['company_id'], expires_at=min(original['expires_at'], now)))
    report = {'state': 'legacy', 'ready': not errors, 'errors': errors,
              'counts': {table: len(rows) for table, rows in source.items()}, 'mapping_format': MAPPING_FORMAT,
              'effects': ['All personal password hashes and entity IDs are retained.',
                          'All migrated accounts require email verification; activation links and sessions expire.',
                          'Legacy company enrollment stays disabled until a company admin sets its join password.',
                          'Legacy memberships and changed ownership are retained as audit provenance.']}
    return report, {'source': source, 'present': present, 'companies': companies, 'users': users,
                    'loops': loops, 'workers': workers, 'runs': runs, 'sessions': sessions, 'now': now}


async def company_preflight(db, settings=None):
    """Read-only JSON-ready findings. Never returns password or token hashes."""
    report, _ = await _plan(db, os.environ if settings is None else settings)
    return report


async def _insert(db, table, row, columns):
    values = [Jsonb(row[column]) if column in ('llm_usage', 'result', 'data', 'legacy_record') and row.get(column) is not None else row.get(column) for column in columns]
    await db.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)),
        sql.SQL(',').join(sql.Placeholder() for _ in columns)), values)


async def _audit(db, company_id, record_type, record):
    await _insert(db, 'company_migration_audit', {'id': uuid4(), 'company_id': company_id,
        'record_type': record_type, 'legacy_record': _json(record)}, ('id', 'company_id', 'record_type', 'legacy_record'))


async def migrate_company_schema(db, settings=None):
    """Initialize or explicitly migrate, atomically. Caller may own an outer transaction."""
    settings = os.environ if settings is None else settings
    async with db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock(hashtext('ctrlloop.company.migration'))")
        tables = await _tables(db)
        if 'companies' not in tables and any(table in tables for table in LEGACY_TABLES):
            if str(settings.get('COMPANY_MIGRATION_APPLY', '')).lower() != 'true':
                raise RuntimeError('Legacy account schema requires explicit migration. Stop the old app/workers, run member_admin.py preflight-companies, then migrate-companies --mapping FILE; startup never guesses ownership.')
            for table in LEGACY_TABLES:
                if table in tables:
                    await db.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(table)))
        report, plan = await _plan(db, settings)
        if not report['ready']:
            # Report contains ownership IDs only; credential material stays internal.
            raise RuntimeError('Company migration preflight requires operator reconciliation: ' + json.dumps(report['errors']))
        if report['state'] != 'legacy':
            await db.execute(SCHEMA.read_text(encoding='utf-8-sig'))
            return dict(report, migrated=False)
        source = plan['source']
        # PostgreSQL table renames leave index names unchanged. Rename indexes
        # first so canonical indexes are created instead of silently skipped.
        for table in plan['present']:
            indexes = await (await db.execute('''SELECT c.oid,c.relname FROM pg_index i
                JOIN pg_class c ON c.oid=i.indexrelid WHERE i.indrelid=to_regclass(%s)''', (table,))).fetchall()
            for index in indexes:
                await db.execute(sql.SQL('ALTER INDEX {} RENAME TO {}').format(sql.Identifier(index['relname']), sql.Identifier('_cm_old_' + str(index['oid']))))
            await db.execute(sql.SQL('ALTER TABLE {} RENAME TO {}').format(sql.Identifier(table), sql.Identifier('_cm_old_' + table)))
        await db.execute(SCHEMA.read_text(encoding='utf-8-sig'))
        for row in plan['companies'].values():
            await _insert(db, 'companies', row, ('id', 'slug', 'name', 'created_at'))
        for row in plan['users'].values():
            if row.get('password_env'):
                from .identity import password_hash
                row['password_hash'] = password_hash(settings[row['password_env']])
            await _insert(db, 'users', row, ('id', 'company_id', 'email', 'display_name', 'password_hash', 'role', 'disabled_at', 'created_at'))
            await _audit(db, row['company_id'], 'legacy_user', {'user_id': row['id'], 'username': row.get('username'),
                'old_activated_at': row.get('activated_at'), 'email_verification_required': True,
                'activation_token_expired': bool(row.get('activation_token_hash'))})
        for row in source['memberships']:
            await _audit(db, row['workspace_id'], 'legacy_membership', row)
        for row in plan['sessions']:
            await _insert(db, 'sessions', row, ('token_hash', 'company_id', 'user_id', 'expires_at'))
            await _audit(db, row['company_id'], 'expired_legacy_session', {'user_id': row['user_id'], 'previous_company_id': row.get('workspace_id'), 'expired_at': row['expires_at']})
        for table, columns in (
            ('workers', ('id', 'company_id', 'assigned_user_id', 'credential_hash', 'fleet_namespace', 'fleet_claim', 'mode', 'human_session', 'last_seen', 'revision', 'active_run', 'disabled_at')),
            ('loops', ('id', 'company_id', 'created_by', 'prompt', 'instruction', 'model', 'status', 'error', 'llm_response_id', 'llm_usage', 'created_at', 'generated_at')),
            ('runs', ('id', 'company_id', 'loop_id', 'worker_id', 'initiated_by', 'status', 'created_at', 'started_at', 'finished_at', 'error', 'result', 'revision')),
        ):
            originals = {str(row['id']): row for row in source[table]}
            for identifier, row in plan[table].items():
                await _insert(db, table, row, columns)
                original = originals[identifier]
                owner_field = {'workers': 'assigned_user_id', 'loops': 'created_by', 'runs': 'initiated_by'}[table]
                await _audit(db, row['company_id'], 'legacy_' + table, {'id': identifier,
                    'previous_company_id': original.get('workspace_id'), 'previous_user_id': original.get(owner_field),
                    'company_id': row['company_id'], 'user_id': row[owner_field]})
        for row in source['events']:
            row = dict(row, company_id=plan['loops'][str(row['loop_id'])]['company_id'])
            await _insert(db, 'events', row, ('id', 'company_id', 'loop_id', 'run_id', 'stage', 'message', 'data', 'created_at'))
        # Verify all copies before removing the old schema. A failure rolls back
        # everything, including renames, indexes, and session expiration.
        expected = {'companies': len(plan['companies']), 'users': len(plan['users']),
                    **{table: len(source[table]) for table in ('sessions', 'workers', 'loops', 'runs', 'events')}}
        for table, count in expected.items():
            copied = await (await db.execute(sql.SQL('SELECT count(*) AS count FROM {}').format(sql.Identifier(table)))).fetchone()
            if copied['count'] != count:
                raise RuntimeError(f'Company migration copy count mismatch for {table}; transaction rolled back')
        await db.execute('SET CONSTRAINTS ALL IMMEDIATE')
        for table in ('events', 'runs', 'loops', 'workers', 'sessions', 'memberships', 'users', 'workspaces'):
            if table in plan['present']:
                # No CASCADE: unrecognized external dependencies block migration.
                await db.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier('_cm_old_' + table)))
        return dict(report, state='current', migrated=True, ready=True)
