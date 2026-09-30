"""Deploy an allowlisted revision only to the recorded user's Fleet desktop.

Normal updates reuse the guest's dedicated credentials. Initial --configure
reads only that user's delivery credential and the authorized local Jev key.
The guest service never receives Fleet credentials or OpenAI credentials.
"""
import argparse
import ast
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tarfile
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cua_sandbox import Sandbox
from dotenv import dotenv_values, load_dotenv
from worker_target import load_target, save_private

ROOT=Path(__file__).resolve().parents[1]

GATE_HEALTH = '''import json, shlex, subprocess, sys, time, urllib.request
target = json.loads(sys.argv[1])
if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'omarchy-websockify.service']).returncode == 0:
    raise SystemExit('The unguarded desktop bridge is still running')
legacy = subprocess.run(['systemctl', '--user', 'is-enabled', 'omarchy-websockify.service'],capture_output=True,text=True)
if legacy.stdout.strip() != 'masked':
    raise SystemExit('The unguarded desktop bridge must remain masked')
if len(sys.argv) > 2:
    revision = sys.argv[2]
else:
    configured = subprocess.check_output(['systemctl', '--user', 'show', 'ctrlloop-worker', '-p', 'Environment', '--value'],text=True)
    values = dict(item.split('=', 1) for item in shlex.split(configured) if '=' in item)
    revision = values.get('CTRLLOOP_REVISION')
if not revision:
    raise SystemExit('The installed worker has no explicit revision')
release = '/home/omarchy/ctrlloop-dev/releases/' + revision
import pathlib
if pathlib.Path(release, 'src/desktop_display.py').exists():
    subprocess.run([release + '/.venv/bin/python', '-m', 'src.desktop_display', '--verify'], cwd=release, check=True)
expected = {'protocol':'ctrlloop-direct-v2', 'revision':revision, 'worker_id':target['id'],
            'environment':target['environment'], 'company_id':target['company_id'],
            'assigned_user_id':target['assigned_user_id']}
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen('http://127.0.0.1:5900/health', timeout=3) as response:
            health = json.load(response)
        if isinstance(health, dict) and all(health.get(key) == value for key, value in expected.items()):
            print('Direct desktop gate is ready for the assigned worker revision')
            break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit('Direct desktop gate is not ready for this assignment and revision; maintenance remains enabled')
'''

DEPLOYMENT_PHASE = '''import hmac, json, pathlib, sys, urllib.request, urllib.error
target, phase = json.loads(sys.argv[1]), sys.argv[2]
active_path = pathlib.Path('/home/omarchy/ctrlloop-dev/worker.env')
env_path = pathlib.Path(sys.argv[3]) if len(sys.argv)>3 else active_path
def read_env(path):
    env = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator: env[key] = json.loads(value)
    return env
env = read_env(env_path)
if env.get('CTRLLOOP_API_URL') != target['api_url'] or env.get('CTRLLOOP_WORKER_ID') != target['id'] or env.get('CTRLLOOP_ENVIRONMENT') != target['environment']:
    raise SystemExit('Guest configuration differs from the explicit user assignment')
if phase == 'begin' and env_path != active_path and active_path.exists():
    prior = read_env(active_path)
    previous_origin = sys.argv[4] if len(sys.argv)>4 else target['api_url']
    if (prior.get('CTRLLOOP_API_URL') != previous_origin
        or prior.get('CTRLLOOP_WORKER_ID', target['id']) != target['id']
        or prior.get('CTRLLOOP_ENVIRONMENT', target['environment']) != target['environment']
        or not hmac.compare_digest(prior.get('WORKER_TOKEN', ''), env['WORKER_TOKEN'])):
        raise SystemExit('Refusing to replace credentials for a different existing worker')
payload = {'phase':phase, 'expected_worker_id':target['id'], 'environment':target['environment'], 'company_id':target['company_id'], 'assigned_user_id':target['assigned_user_id']}
request = urllib.request.Request(env['CTRLLOOP_API_URL']+'/api/worker/deployment',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+env['WORKER_TOKEN'],'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(request,timeout=30) as response: result=json.load(response)
except urllib.error.HTTPError as error:
    raise SystemExit('Deployment authorization refused with HTTP '+str(error.code)) from None
if (result.get('worker_id')!=target['id'] or result.get('environment')!=target['environment']
        or result.get('company_id')!=target['company_id'] or result.get('assigned_user_id')!=target['assigned_user_id']):
    raise SystemExit('Server assignment mismatch; inspect maintenance state')
print('Worker deployment phase: '+phase)
'''


def bundle(ref=None, *, require_direct=True):
    paths=[ROOT/'pyproject.toml',ROOT/'uv.lock',*sorted((ROOT/'src').glob('*.py'))]
    if ref:
        commit=subprocess.check_output(['git','rev-parse','--verify',ref+'^{commit}'],cwd=ROOT,text=True).strip()
        names=subprocess.check_output(['git','ls-tree','-r','--name-only',commit,'--','src','pyproject.toml','uv.lock'],cwd=ROOT,text=True).splitlines()
        names=[name for name in names if name in ('pyproject.toml','uv.lock') or (name.startswith('src/') and name.endswith('.py') and name.count('/')==1)]
        items=[(name,subprocess.check_output(['git','show',f'{commit}:{name}'],cwd=ROOT)) for name in sorted(names)]
    else:
        items=[(path.relative_to(ROOT).as_posix(),path.read_bytes()) for path in paths]
    if require_direct:
        contents = dict(items)
        gate = contents.get('src/desktop_stream.py', b'')
        worker = ast.parse(contents.get('src/cloud_worker.py', b''))
        if (b'ctrlloop-direct-v2' not in gate
                or not any(isinstance(node, ast.ImportFrom) and node.module == 'desktop_stream'
                           for node in ast.walk(worker))):
            raise SystemExit('Selected revision has no compatible direct desktop gate. Deploy a direct-streaming worker revision; older --source-ref values cannot be rolled back onto this endpoint.')
    digest=hashlib.sha256()
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for name,content in items:
            digest.update(name.encode()+b'\0'+content)
            entry=tarfile.TarInfo(name);entry.size=len(content);entry.mode=0o644
            archive.addfile(entry,io.BytesIO(content))
    return digest.hexdigest()[:16],stream.getvalue()


async def main(args):
    if args.configure and (args.resume or args.status or args.stage_only):
        raise ValueError('--configure is only valid for a normal deployment')
    if args.previous_api_url and not args.configure:
        raise ValueError('--previous-api-url requires --configure')
    load_dotenv(args.fleet_env,interpolate=False)
    config,target,local,reference=load_target(args.environment,args.email)
    if args.previous_api_url:
        previous = urlsplit(args.previous_api_url)
        if (previous.scheme != 'https' or not previous.hostname or previous.path not in ('', '/')
                or previous.username or previous.password or previous.query or previous.fragment):
            raise ValueError('Previous application URL must be a credential-free HTTPS origin')
        assigned_origin = urlsplit(target['api_url'])
        if (previous.hostname, previous.port or 443) == (assigned_origin.hostname, assigned_origin.port or 443):
            raise ValueError('Previous application URL must differ from the new assignment origin')
    revision,archive=bundle(args.source_ref, require_direct=not (args.status or args.resume))
    remote=f'/home/omarchy/ctrlloop-dev/releases/{revision}'
    async with Sandbox.from_dict(reference) as sandbox:
        if sandbox.name!=target['id']:raise RuntimeError('Refusing a sandbox outside the recorded user assignment')
        if args.status:
            r=await sandbox.shell.run('systemctl --user show ctrlloop-worker -p ActiveState -p SubState -p MainPID -p ExecMainStatus -p FragmentPath')
            print(r.stdout);return
        attempt=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        control='/home/omarchy/ctrlloop-dev/deployments/'+attempt
        await sandbox.files.make_dir(remote)
        await sandbox.files.make_dir(control)
        await sandbox.files.write_bytes(control+'/source.tar.gz',archive)
        if args.stage_only:
            setup=f'''set -eu
umask 077
exec 9>/home/omarchy/ctrlloop-dev/deploy.lock
flock -n 9
cd {shlex.quote(remote)}
tar -xzf {shlex.quote(control+'/source.tar.gz')}
uv sync --locked --no-dev --python 3.12
.venv/bin/python -m compileall -q src
'''
            await sandbox.files.write_text(control+'/stage.sh',setup)
            await sandbox.shell.run(f'bash {shlex.quote(control+"/stage.sh")} > {shlex.quote(control+"/stage.log")} 2>&1; printf "%s" "$?" > {shlex.quote(control+"/stage.status")}',background=True)
            result={'revision':revision,'remote':remote,'sandbox':sandbox.name,'status':'staging_code_only','guest_log':control+'/stage.log'}
            save_private(local/'deployment.json',result)
            print(json.dumps(result));return
        if await sandbox.files.exists('/home/omarchy/.local/state/ctrlloop-worker/active-run.json'):
            raise RuntimeError('An active or unsettled run exists; finish or resolve it before deployment')
        env_path='/home/omarchy/ctrlloop-dev/worker.env'
        staged_env=None
        if args.configure:
            if not args.jev_env:raise ValueError('--configure requires --jev-env')
            private=json.loads((local/'credentials.json').read_text())
            if (private['worker_id']!=target['id'] or private['environment']!=args.environment
                    or private.get('company_id')!=target['company_id'] or private.get('assigned_user_id')!=target['assigned_user_id']):
                raise ValueError('Dedicated credentials do not match this worker and environment')
            jev=dotenv_values(args.jev_env,interpolate=False).get('TYPESAFE_API_KEY')
            if not jev:raise RuntimeError('TYPESAFE_API_KEY is missing')
            settings={'CTRLLOOP_API_URL':target['api_url'],'WORKER_TOKEN':private['WORKER_TOKEN'],
                  'CTRLLOOP_WORKER_ID':target['id'],'CTRLLOOP_ENVIRONMENT':args.environment,
                  'TYPESAFE_API_KEY':jev,
                  'CUA_LOCAL_MCP_URL':'http://127.0.0.1:3000/mcp',
                  'CTRLLOOP_REPORT_DIR':'/home/omarchy/ctrlloop-dev/reports',
                  'CTRLLOOP_WORKER_STATE':'/home/omarchy/.local/state/ctrlloop-worker',
                  'XDG_RUNTIME_DIR':'/run/user/1000','WAYLAND_DISPLAY':'wayland-1',
                  'DISPLAY':':0','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus'}
            # systemd EnvironmentFile quoting: JSON double quotes escape values safely.
            environment='\n'.join(key+'='+json.dumps(value) for key,value in settings.items())+'\n'
            protection=await sandbox.shell.run('chmod 700 /home/omarchy/ctrlloop-dev')
            if not protection.success:raise RuntimeError('Cannot secure the worker configuration directory')
            staged_env=control+'/worker.env'
            await sandbox.files.write_text(staged_env,environment)
            protection=await sandbox.shell.run('chmod 600 '+shlex.quote(staged_env))
            if not protection.success:raise RuntimeError('Cannot secure the staged worker configuration')
        elif not await sandbox.files.exists(env_path):
            raise RuntimeError('Administrator must configure the initial dedicated worker credentials')
        await sandbox.files.write_text(control+'/deployment_phase.py',DEPLOYMENT_PHASE)
        await sandbox.files.write_text(control+'/gate_health.py',GATE_HEALTH)
        phase=f'python {shlex.quote(control+"/deployment_phase.py")} {shlex.quote(json.dumps(target))}'
        health=f'python {shlex.quote(control+"/gate_health.py")} {shlex.quote(json.dumps(target))}'
        if args.resume:
            recovery=health+' && '+phase+' finish'
            result=await sandbox.shell.run('flock -n /home/omarchy/ctrlloop-dev/deploy.lock bash -c '+shlex.quote(recovery))
            if not result.success:raise RuntimeError('Maintenance release refused; inspect the worker')
            print(json.dumps({'worker_id':target['id'],'status':'maintenance_released'}));return
        service=f'''[Unit]
Description=CTRL Loop assigned desktop worker
After=default.target

[Service]
Type=simple
WorkingDirectory={remote}
EnvironmentFile={env_path}
Environment=CTRLLOOP_REVISION={revision}
ExecStart={remote}/.venv/bin/python -m src.cloud_worker
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
'''
        await sandbox.files.make_dir('/home/omarchy/.config/systemd/user')
        await sandbox.files.write_text(control+'/ctrlloop-worker.service',service)
        begin=phase+' begin'+(' '+shlex.quote(staged_env) if staged_env else '')
        if args.previous_api_url: begin+=' '+shlex.quote(args.previous_api_url)
        install_env=('install -m 600 '+shlex.quote(staged_env)+' '+shlex.quote(env_path)+'\n'
                     +'rm -f '+shlex.quote(staged_env)+'\n') if staged_env else ''
        setup=f'''set -eu
umask 077
exec 9>/home/omarchy/ctrlloop-dev/deploy.lock
flock -n 9
{begin}
test ! -e /home/omarchy/.local/state/ctrlloop-worker/active-run.json
# Retire the service-wide input bridge before installing or starting the gate.
if systemctl --user is-active --quiet omarchy-websockify.service || systemctl --user cat omarchy-websockify.service >/dev/null 2>&1; then
    systemctl --user disable --now omarchy-websockify.service
fi
ln -sfn /dev/null /home/omarchy/.config/systemd/user/omarchy-websockify.service
touch /home/omarchy/ctrlloop-dev/direct-streaming-required
systemctl --user daemon-reload
if systemctl --user is-active --quiet omarchy-websockify.service; then
    echo 'The unguarded desktop bridge did not stop' >&2
    exit 1
fi
cd {shlex.quote(remote)}
tar -xzf {shlex.quote(control+'/source.tar.gz')}
uv sync --locked --no-dev --python 3.12
.venv/bin/python -m compileall -q src
test ! -e /home/omarchy/.local/state/ctrlloop-worker/active-run.json
if test -f src/desktop_display.py; then
    .venv/bin/python -m src.desktop_display
fi
{install_env}cp {shlex.quote(control+'/ctrlloop-worker.service')} /home/omarchy/.config/systemd/user/ctrlloop-worker.service
systemctl --user daemon-reload
systemctl --user enable ctrlloop-worker
systemctl --user restart ctrlloop-worker
systemctl --user is-active ctrlloop-worker
{health} {shlex.quote(revision)}
{phase} finish
'''
        await sandbox.files.write_text(control+'/setup.sh',setup)
        prefix=control+'/deploy'
        runner=f'bash {shlex.quote(control+"/setup.sh")} > {shlex.quote(prefix+".log")} 2>&1; printf "%s" "$?" > {shlex.quote(prefix+".status")}'
        await sandbox.shell.run(runner,background=True)
        result={'revision':revision,'source_ref':args.source_ref,'environment':args.environment,'email':args.email,'remote':remote,'sandbox':sandbox.name,'deployed_at':datetime.now(timezone.utc).isoformat(),'status':'installing','guest_log':prefix+'.log'}
        save_private(local/'deployment.json',result)
        print(json.dumps(result))
        for _ in range(60):
            if await sandbox.files.exists(prefix+'.status'):
                code=(await sandbox.files.read_text(prefix+'.status')).strip()
                result['status']='running' if code=='0' else 'failed'
                save_private(local/'deployment.json',result)
                if code!='0':raise RuntimeError('Guest deployment failed; inspect the recorded private guest_log. Worker remains in maintenance until explicit --resume.')
                print(json.dumps({'revision':revision,'worker_id':target['id'],'status':result['status']}))
                return
            await asyncio.sleep(2)
        print('Install continues in guest; inspect the recorded guest_log and matching .status file')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fleet-env',type=Path,required=True)
    parser.add_argument('--environment',choices=('dev','prod'),required=True)
    parser.add_argument('--email',required=True)
    parser.add_argument('--jev-env',type=Path)
    parser.add_argument('--source-ref',help='Bundle an exact Git commit/ref without changing this checkout')
    parser.add_argument('--configure',action='store_true',help='Initial administrator setup from dedicated user credential files')
    parser.add_argument('--previous-api-url',help='With --configure only: require this exact prior HTTPS origin during an explicit origin migration')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--resume',action='store_true',help='Release maintenance only after inspecting a failed deployment')
    mode.add_argument('--status',action='store_true')
    mode.add_argument('--stage-only',action='store_true',help='Upload code and install dependencies only; no secret reads, credential transfer, or worker start')
    try:asyncio.run(main(parser.parse_args()))
    except Exception as error:
        raise SystemExit(f'Worker deploy stopped ({type(error).__name__}); inspect private deployment state.') from None
