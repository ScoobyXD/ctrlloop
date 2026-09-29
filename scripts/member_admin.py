"""Explicit company migration, verified-account privilege, and VM assignment.

Requires the selected environment's private database credential. Signup stays
in the normal website flow; this tool never invents users or sends email.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys

from cua_sandbox import Sandbox
from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from deployment_config import ROOT, load_environment
from worker_target import save_private, validate_application_origin
sys.path.insert(0, str(ROOT))
from src.company_migration import company_preflight, migrate_company_schema
from src.identity import normalize_email, token_hash


class AdministrationError(ValueError):
    """A deliberately credential-free error safe to show to the operator."""


async def migrate(db, config, args):
    settings = dict(os.environ)
    if args.mapping:
        settings['COMPANY_MIGRATION_MAP'] = str(args.mapping.resolve())
    try:
        report = await company_preflight(db, settings)
    except RuntimeError as error:
        raise AdministrationError(str(error)) from None
    if args.command == 'preflight-companies':
        print(json.dumps(report, default=str, indent=2))
        return
    if not report['ready']:
        print(json.dumps(report, default=str, indent=2))
        raise AdministrationError('Company migration is blocked; resolve every preflight error using explicit operator mappings')
    # Database access cannot establish whether an old application is still live.
    if report['state'] == 'legacy' and not args.application_stopped:
        raise AdministrationError('Stop every old web process and worker, then pass --application-stopped before the schema cutover')
    settings['COMPANY_MIGRATION_APPLY'] = 'true'
    await db.commit()
    async with db.transaction():
        await db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        await db.execute("SET LOCAL lock_timeout='10s'")
        await db.execute("SELECT pg_advisory_xact_lock(hashtext('ctrlloop.company.migration'))")
        # Old names exist here solely to preserve source data during migration.
        tables = []
        for table in ('companies', 'workspaces', 'memberships', 'users', 'sessions', 'workers', 'loops', 'runs', 'events', 'auth_tokens', 'company_migration_audit'):
            if (await (await db.execute('SELECT to_regclass(%s) AS name', (table,))).fetchone())['name']:
                tables.append(table)
        if tables:
            await db.execute('LOCK TABLE '+','.join(tables)+' IN ACCESS EXCLUSIVE MODE')
        if 'runs' in tables and (await (await db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1")).fetchone()):
            raise AdministrationError('Finish queued/running runs before the company cutover')
        if 'loops' in tables and (await (await db.execute("SELECT 1 FROM loops WHERE status='generating' LIMIT 1")).fetchone()):
            raise AdministrationError('Finish instruction generation before the company cutover')
        backup = {'environment': config.name, 'created_at': datetime.now(timezone.utc).isoformat(), 'tables': {}}
        for table in tables:
            backup['tables'][table] = [row['value'] for row in await (await db.execute('SELECT row_to_json(t) AS value FROM '+table+' t')).fetchall()]
        filename = config.state_dir/'backups'/('companies-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
        save_private(filename, backup)
        try:
            result = await migrate_company_schema(db, settings)
        except RuntimeError as error:
            raise AdministrationError(str(error)) from None
    print(json.dumps({'migration': result, 'backup': str(filename),
                      'preserved_rows': {name: len(rows) for name, rows in backup['tables'].items()}}, default=str, indent=2))


async def main(args):
    config = load_environment(args.environment)
    if not config['enabled']:
        raise AdministrationError('This environment is not enabled for administration')
    private = json.loads(config.secret_path.read_text(encoding='utf-8'))
    state = json.loads(config.state_path.read_text(encoding='utf-8'))
    validate_application_origin(config, state['url'], state)
    connection = conninfo_to_dict(private['DATABASE_URL'])
    if (connection.get('host') != config['db_host'] or connection.get('dbname') != config['db_name']
            or connection.get('user') != config['db_user'] or connection.get('sslmode') not in ('require', 'verify-ca', 'verify-full')):
        raise AdministrationError('Database identity or TLS mode differs from the explicit environment')
    async with await AsyncConnection.connect(private['DATABASE_URL'], row_factory=dict_row, connect_timeout=15) as db:
        if args.command in ('preflight-companies', 'migrate-companies'):
            await migrate(db, config, args)
            return
        if args.command == 'list':
            rows = await (await db.execute('''SELECT c.id AS company_id,c.slug,c.name,u.id AS user_id,u.email,
                u.role,u.is_devadmin,u.email_verified_at,u.disabled_at,
                w.id AS worker_id,w.mode,w.last_seen,w.revision,w.fleet_namespace,w.fleet_claim
                FROM companies c LEFT JOIN users u ON u.company_id=c.id
                LEFT JOIN workers w ON w.company_id=u.company_id AND w.assigned_user_id=u.id
                ORDER BY c.slug,u.email''')).fetchall()
            print(json.dumps(rows, default=str, indent=2))
            return
        email = normalize_email(args.email)
        await db.execute("SELECT pg_advisory_xact_lock(hashtext('ctrlloop.account.management'))")
        user = await (await db.execute('SELECT id,company_id,email,email_verified_at,disabled_at FROM users WHERE email=%s FOR UPDATE', (email,))).fetchone()
        if not user:
            raise AdministrationError('No account has that email; complete normal signup first')
        if args.command == 'set-devadmin':
            if args.grant and (user['email_verified_at'] is None or user['disabled_at'] is not None):
                raise AdministrationError('Devadmin can be granted only to an enabled, email-verified account')
            await db.execute('UPDATE users SET is_devadmin=%s WHERE id=%s AND company_id=%s', (args.grant, user['id'], user['company_id']))
            await db.execute("""UPDATE workers SET mode=CASE WHEN mode='human' THEN 'agent' ELSE mode END,human_session=NULL
                WHERE human_session IN (SELECT token_hash FROM sessions WHERE user_id=%s AND company_id=%s)""",
                (user['id'], user['company_id']))
            await db.execute('DELETE FROM sessions WHERE user_id=%s AND company_id=%s', (user['id'], user['company_id']))
            await db.commit()
            print(json.dumps({'email': email, 'is_devadmin': args.grant, 'sessions_revoked': True}))
            return
        company = await (await db.execute('SELECT id,slug FROM companies WHERE slug=%s FOR UPDATE', (args.company,))).fetchone()
        if not company or company['id'] != user['company_id'] or user['disabled_at'] is not None or user['email_verified_at'] is None:
            raise AdministrationError('An enabled, verified user in the explicitly selected company is required')
        local = config.worker_dir(email)
        load_dotenv(args.fleet_env, interpolate=False)
        reference = json.loads(args.claim.read_text(encoding='utf-8-sig'))
        async with Sandbox.from_dict(reference) as sandbox:
            if sandbox.name != args.expected_sandbox:
                raise AdministrationError('Bound Fleet sandbox differs from the explicit expected identity')
            identity = sandbox.name
        existing = await (await db.execute('SELECT * FROM workers WHERE id=%s FOR UPDATE', (identity,))).fetchone()
        if existing and (existing['company_id'] != company['id'] or existing['assigned_user_id'] != user['id']
                or existing['fleet_namespace'] != reference['namespace'] or existing['fleet_claim'] != reference['claim']):
            raise AdministrationError('Existing worker assignment cannot be repurposed')
        if existing and existing['disabled_at'] is not None:
            raise AdministrationError('The recorded worker is disabled; explicit administrator review is required')
        assigned = await (await db.execute('''SELECT id FROM workers WHERE company_id=%s
            AND assigned_user_id=%s AND disabled_at IS NULL FOR UPDATE''', (company['id'], user['id']))).fetchone()
        if assigned and assigned['id'] != identity:
            raise AdministrationError('This user already has an enabled worker; its assignment was not changed')
        credentials_path = local/'credentials.json'
        source = credentials_path if credentials_path.exists() else args.existing_credentials
        if source:
            credential = json.loads(source.read_text(encoding='utf-8-sig'))
            if credential['worker_id'] != identity or credential['environment'] != args.environment:
                raise AdministrationError('Existing credentials belong to a different assignment')
            raw = credential['WORKER_TOKEN']
        elif existing:
            raise AdministrationError('Existing worker credential is unavailable; supply --existing-credentials with its dedicated credential file')
        else:
            raw = secrets.token_urlsafe(32)
        if existing and token_hash(raw) != existing['credential_hash']:
            raise AdministrationError('Credential differs from the registered worker; no rotation was performed')
        claim_path = local/'fleet'/'claim.json'
        if claim_path.exists() and json.loads(claim_path.read_text(encoding='utf-8-sig')) != reference:
            raise AdministrationError('Recorded claim already refers to another desktop')
        target = {'environment': args.environment, 'email': email, 'id': identity, 'company_id': company['id'],
                  'assigned_user_id': user['id'], 'fleet_namespace': reference['namespace'], 'fleet_claim': reference['claim'], 'api_url': state['url']}
        save_private(credentials_path, {'environment': args.environment, 'worker_id': identity,
                     'company_id': company['id'], 'assigned_user_id': user['id'], 'WORKER_TOKEN': raw})
        save_private(claim_path, reference)
        save_private(local/'target.json', target)
        if not existing:
            await db.execute('''INSERT INTO workers(id,company_id,assigned_user_id,credential_hash,fleet_namespace,fleet_claim,mode)
                VALUES(%s,%s,%s,%s,%s,%s,'maintenance')''',
                (identity, company['id'], user['id'], token_hash(raw), reference['namespace'], reference['claim']))
        await db.commit()
        print(json.dumps({'assigned': target, 'state_directory': str(local)}, default=str, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=('dev', 'prod'), required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('preflight-companies', 'migrate-companies'):
        sub = commands.add_parser(command)
        sub.add_argument('--mapping', type=Path)
        if command == 'migrate-companies':
            sub.add_argument('--application-stopped', action='store_true', help='Attest all old web processes and workers are stopped for this cutover')
    commands.add_parser('list')
    sub = commands.add_parser('set-devadmin')
    sub.add_argument('--email', required=True)
    change = sub.add_mutually_exclusive_group(required=True)
    change.add_argument('--grant', action='store_true')
    change.add_argument('--revoke', action='store_true')
    sub = commands.add_parser('assign-worker')
    sub.add_argument('--company', required=True)
    sub.add_argument('--email', required=True)
    sub.add_argument('--claim', type=Path, required=True)
    sub.add_argument('--expected-sandbox', required=True)
    sub.add_argument('--fleet-env', type=Path, required=True)
    sub.add_argument('--existing-credentials', type=Path)
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main(parser.parse_args()))
    except Exception as error:
        # SDK/database errors can contain secrets. Print controlled messages only.
        message = str(error) if isinstance(error, AdministrationError) else type(error).__name__
        raise SystemExit('Company administration stopped: '+message) from None
