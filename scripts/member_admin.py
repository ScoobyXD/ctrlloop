"""Explicit environment administration using the existing identity/worker records.

Requires that environment's private database credential. Website members do not
need this access. Secrets and activation codes are written only to ignored files.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import shutil
import sys
from uuid import uuid4

from cua_sandbox import Sandbox
from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from deployment_config import ROOT, load_environment
from worker_target import save_private, validate_application_origin
sys.path.insert(0, str(ROOT))
from src.identity import create_member, migrate_legacy_identity, normalize_username, token_hash


class AdministrationError(ValueError):
    """A deliberately credential-free error safe to show to the operator."""


async def main(args):
    config = load_environment(args.environment)
    if not config['enabled']: raise AdministrationError('This environment is not enabled for administration')
    private = json.loads(config.secret_path.read_text(encoding='utf-8'))
    state = json.loads(config.state_path.read_text(encoding='utf-8'))
    validate_application_origin(config, state['url'], state)
    connection = conninfo_to_dict(private['DATABASE_URL'])
    if connection.get('host') != config['db_host'] or connection.get('dbname') != config['db_name'] or connection.get('user') != config['db_user'] or connection.get('sslmode') not in ('require', 'verify-ca', 'verify-full'):
        raise AdministrationError('Database identity or TLS mode differs from the explicit environment')
    async with await AsyncConnection.connect(private['DATABASE_URL'], row_factory=dict_row, connect_timeout=15) as db:
        if args.command == 'migrate-legacy-dev':
            if args.environment != 'dev': raise AdministrationError('Legacy migration is development-only')
            # Lock before the first snapshot read. This deliberately pauses legacy
            # requests for the backup/migration transaction: no old application
            # insert can slip between the snapshot and identity assignment. Taking
            # the DDL-strength locks now also avoids upgrading weaker table locks
            # while another transaction is waiting to write. Locks release on
            # either commit or rollback; a busy database fails without migrating.
            await db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            await db.execute("SET LOCAL lock_timeout='10s'")
            await db.execute('LOCK TABLE sessions,workers,loops,runs,events IN ACCESS EXCLUSIVE MODE')
            busy = await (await db.execute('''SELECT
                EXISTS(SELECT 1 FROM runs WHERE status IN ('queued','running')) AS active_runs,
                EXISTS(SELECT 1 FROM loops WHERE status='generating') AS generating_loops''')).fetchone()
            if busy['active_runs'] or busy['generating_loops']:
                raise AdministrationError('Finish queued/running runs and instruction generation before the identity cutover')
            backup = {'environment': 'dev', 'created_at': datetime.now(timezone.utc).isoformat(),
                      'isolation': 'repeatable read', 'legacy_table_lock': 'access exclusive', 'tables': {}}
            for table in ('sessions', 'workers', 'loops', 'runs', 'events'):
                backup['tables'][table] = await (await db.execute('SELECT row_to_json(t) AS value FROM '+table+' t')).fetchall()
            filename = config.state_dir/'backups'/('identity-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
            save_private(filename, backup)
            await db.execute((ROOT/'src/schema.sql').read_text())
            worker = config['bootstrap_worker']
            settings = {**private, 'APP_ENV': 'dev', 'BOOTSTRAP_LEGACY_DEV': 'true',
                'WORKER_ID': worker['id'], 'FLEET_NAMESPACE': worker['namespace'], 'FLEET_SANDBOX': worker['id'],
                'FLEET_CLAIM': worker['claim'], 'BOOTSTRAP_OWNER_USERNAME': config['bootstrap_owner_username'],
                'BOOTSTRAP_OWNER_DISPLAY_NAME': config['bootstrap_owner_display_name'],
                'BOOTSTRAP_WORKSPACE_SLUG': config['bootstrap_workspace_slug'], 'BOOTSTRAP_WORKSPACE_NAME': config['bootstrap_workspace_name']}
            result = await migrate_legacy_identity(db, settings)
            await db.commit()
            print(json.dumps({'migration': result, 'backup': str(filename), 'preserved_rows': {name:len(rows) for name,rows in backup['tables'].items()}},default=str)); return
        if args.command == 'list':
            rows = await (await db.execute('''SELECT s.slug,u.username,m.role,m.revoked_at,
                u.activated_at,w.id AS worker_id,w.mode,w.last_seen,w.revision,w.fleet_namespace,w.fleet_claim
                FROM workspaces s JOIN memberships m ON m.workspace_id=s.id
                JOIN users u ON u.id=m.user_id LEFT JOIN workers w ON w.workspace_id=m.workspace_id AND w.assigned_user_id=m.user_id
                ORDER BY s.slug,u.username''')).fetchall()
            print(json.dumps(rows,default=str,indent=2)); return
        if args.command == 'create-workspace':
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,63}', args.workspace): raise AdministrationError('Invalid workspace slug')
            await db.execute((ROOT/'src/schema.sql').read_text())
            workspace = await (await db.execute('INSERT INTO workspaces(id,slug,name) VALUES(%s,%s,%s) RETURNING *', (uuid4(),args.workspace,args.name))).fetchone()
        else:
            workspace = await (await db.execute('SELECT * FROM workspaces WHERE slug=%s FOR UPDATE', (args.workspace,))).fetchone()
            if not workspace: raise AdministrationError('Workspace does not exist in this environment')
        member = normalize_username(args.member)
        local = config.worker_dir(member)
        if args.command in ('create-workspace','add-member'):
            value = await create_member(db,workspace['id'],member,args.display_name,'owner' if args.command=='create-workspace' else 'member')
            filename = local/'activation.json'
            if filename.exists(): raise AdministrationError('An activation receipt already exists; inspect it before creating another')
            save_private(filename, {**value, 'environment':args.environment,'workspace':workspace['slug'],'url':state['url']})
            print(json.dumps({'member':value['member'],'activation_receipt':str(filename),'activation_expires_at':value['activation_expires_at']},default=str)); return
        user = await (await db.execute('''SELECT u.id FROM users u JOIN memberships m ON m.user_id=u.id
            WHERE u.username=%s AND m.workspace_id=%s AND m.revoked_at IS NULL AND u.disabled_at IS NULL FOR UPDATE OF m''', (member,workspace['id']))).fetchone()
        if not user: raise AdministrationError('An active workspace membership is required')
        load_dotenv(args.fleet_env,interpolate=False)
        reference = json.loads(args.claim.read_text(encoding='utf-8-sig'))
        async with Sandbox.from_dict(reference) as sandbox:
            if sandbox.name != args.expected_sandbox: raise AdministrationError('Bound Fleet sandbox differs from the explicit expected identity')
            identity = sandbox.name
        existing = await (await db.execute('SELECT * FROM workers WHERE id=%s FOR UPDATE', (identity,))).fetchone()
        if existing and (existing['workspace_id'] != workspace['id'] or existing['assigned_user_id'] != user['id'] or existing['fleet_namespace'] != reference['namespace'] or existing['fleet_claim'] != reference['claim']):
            raise AdministrationError('Existing worker assignment cannot be repurposed')
        if existing and existing['disabled_at'] is not None:
            raise AdministrationError('The recorded worker is disabled; explicit administrator review is required')
        assigned = await (await db.execute('''SELECT id FROM workers WHERE workspace_id=%s
            AND assigned_user_id=%s AND disabled_at IS NULL FOR UPDATE''', (workspace['id'],user['id']))).fetchone()
        if assigned and assigned['id'] != identity:
            raise AdministrationError('This membership already has an enabled worker; its assignment was not changed')
        credentials_path = local/'credentials.json'
        if credentials_path.exists():
            credential = json.loads(credentials_path.read_text())
            if credential['worker_id'] != identity or credential['environment'] != args.environment: raise AdministrationError('Existing local credentials belong to a different assignment')
            raw = credential['WORKER_TOKEN']
        elif existing:
            if args.environment != 'dev' or identity != config['bootstrap_worker']['id'] or token_hash(private['WORKER_TOKEN']) != existing['credential_hash']:
                raise AdministrationError('Existing worker credential is not available; do not silently rotate it')
            raw = private['WORKER_TOKEN']
        else:
            raw = secrets.token_urlsafe(32)
        if existing and token_hash(raw) != existing['credential_hash']: raise AdministrationError('Local credential differs from the registered worker')
        save_private(credentials_path, {'environment':args.environment,'worker_id':identity,'WORKER_TOKEN':raw})
        if not existing:
            await db.execute('''INSERT INTO workers(id,workspace_id,assigned_user_id,credential_hash,fleet_namespace,fleet_claim,mode)
                VALUES(%s,%s,%s,%s,%s,%s,'maintenance')''',(identity,workspace['id'],user['id'],token_hash(raw),reference['namespace'],reference['claim']))
        claim_path = local/'fleet'/'claim.json'
        if claim_path.exists() and json.loads(claim_path.read_text(encoding='utf-8-sig')) != reference:
            raise AdministrationError('Recorded member claim already refers to another desktop')
        if not claim_path.exists():
            claim_path.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(args.claim,claim_path)
        target = {'environment':args.environment,'member':member,'id':identity,'workspace_id':workspace['id'],
            'assigned_user_id':user['id'],'fleet_namespace':reference['namespace'],'fleet_claim':reference['claim'],'api_url':state['url']}
        save_private(local/'target.json',target)
        print(json.dumps({'assigned':target,'state_directory':str(local)},default=str,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment',choices=('dev','prod'),required=True)
    commands = parser.add_subparsers(dest='command',required=True)
    commands.add_parser('migrate-legacy-dev'); commands.add_parser('list')
    for command in ('create-workspace','add-member','assign-worker'):
        sub = commands.add_parser(command)
        sub.add_argument('--workspace',required=True); sub.add_argument('--member',required=True)
        if command == 'assign-worker':
            sub.add_argument('--claim',type=Path,required=True); sub.add_argument('--expected-sandbox',required=True)
            sub.add_argument('--fleet-env',type=Path,required=True)
        else:
            sub.add_argument('--display-name',required=True)
            if command == 'create-workspace': sub.add_argument('--name',required=True)
    try: asyncio.run(main(parser.parse_args()))
    except Exception as error:
        # SDK/database errors can include request details. Only our own controlled
        # validation messages are safe to print; private credentials stay local.
        message = str(error) if isinstance(error,AdministrationError) else type(error).__name__
        raise SystemExit('Member administration stopped: '+message) from None
