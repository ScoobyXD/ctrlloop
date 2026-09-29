"""Deploy an allowlisted revision only to the recorded member's Fleet desktop.

Normal updates reuse the guest's dedicated credentials. Initial --configure
reads only that member's delivery credential and the authorized local Jev key.
The guest service never receives Fleet credentials or OpenAI credentials.
"""
import argparse
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

from cua_sandbox import Sandbox
from dotenv import dotenv_values, load_dotenv
from worker_target import load_target, save_private

ROOT=Path(__file__).resolve().parents[1]

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
    raise SystemExit('Guest configuration differs from the explicit member assignment')
if phase == 'begin' and env_path != active_path and active_path.exists():
    prior = read_env(active_path)
    if (prior.get('CTRLLOOP_API_URL') != target['api_url']
        or prior.get('CTRLLOOP_WORKER_ID', target['id']) != target['id']
        or prior.get('CTRLLOOP_ENVIRONMENT', target['environment']) != target['environment']
        or not hmac.compare_digest(prior.get('WORKER_TOKEN', ''), env['WORKER_TOKEN'])):
        raise SystemExit('Refusing to replace credentials for a different existing worker')
payload = {'phase':phase, 'expected_worker_id':target['id'], 'environment':target['environment']}
request = urllib.request.Request(env['CTRLLOOP_API_URL']+'/api/worker/deployment',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+env['WORKER_TOKEN'],'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(request,timeout=30) as response: result=json.load(response)
except urllib.error.HTTPError as error:
    raise SystemExit('Deployment authorization refused with HTTP '+str(error.code)) from None
if result.get('worker_id')!=target['id'] or result.get('environment')!=target['environment']:
    raise SystemExit('Server assignment mismatch; inspect maintenance state')
print('Worker deployment phase: '+phase)
'''


def bundle(ref=None):
    paths=[ROOT/'pyproject.toml',ROOT/'uv.lock',*sorted((ROOT/'src').glob('*.py'))]
    if ref:
        commit=subprocess.check_output(['git','rev-parse','--verify',ref+'^{commit}'],cwd=ROOT,text=True).strip()
        names=subprocess.check_output(['git','ls-tree','-r','--name-only',commit,'--','src','pyproject.toml','uv.lock'],cwd=ROOT,text=True).splitlines()
        names=[name for name in names if name in ('pyproject.toml','uv.lock') or (name.startswith('src/') and name.endswith('.py') and name.count('/')==1)]
        items=[(name,subprocess.check_output(['git','show',f'{commit}:{name}'],cwd=ROOT)) for name in sorted(names)]
    else:
        items=[(path.relative_to(ROOT).as_posix(),path.read_bytes()) for path in paths]
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
    load_dotenv(args.fleet_env,interpolate=False)
    config,target,local,reference=load_target(args.environment,args.member)
    revision,archive=bundle(args.source_ref)
    remote=f'/home/omarchy/ctrlloop-dev/releases/{revision}'
    async with Sandbox.from_dict(reference) as sandbox:
        if sandbox.name!=target['id']:raise RuntimeError('Refusing a sandbox outside the recorded member assignment')
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
            if private['worker_id']!=target['id'] or private['environment']!=args.environment:
                raise ValueError('Dedicated credentials do not match this worker and environment')
            jev=dotenv_values(args.jev_env).get('TYPESAFE_API_KEY')
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
        phase=f'python {shlex.quote(control+"/deployment_phase.py")} {shlex.quote(json.dumps(target))}'
        if args.resume:
            result=await sandbox.shell.run('flock -n /home/omarchy/ctrlloop-dev/deploy.lock '+phase+' finish')
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
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
'''
        await sandbox.files.make_dir('/home/omarchy/.config/systemd/user')
        await sandbox.files.write_text(control+'/ctrlloop-worker.service',service)
        begin=phase+' begin'+(' '+shlex.quote(staged_env) if staged_env else '')
        install_env=('install -m 600 '+shlex.quote(staged_env)+' '+shlex.quote(env_path)+'\n'
                     +'rm -f '+shlex.quote(staged_env)+'\n') if staged_env else ''
        setup=f'''set -eu
umask 077
exec 9>/home/omarchy/ctrlloop-dev/deploy.lock
flock -n 9
{begin}
test ! -e /home/omarchy/.local/state/ctrlloop-worker/active-run.json
cd {shlex.quote(remote)}
tar -xzf {shlex.quote(control+'/source.tar.gz')}
uv sync --locked --no-dev --python 3.12
.venv/bin/python -m compileall -q src
test ! -e /home/omarchy/.local/state/ctrlloop-worker/active-run.json
{install_env}cp {shlex.quote(control+'/ctrlloop-worker.service')} /home/omarchy/.config/systemd/user/ctrlloop-worker.service
systemctl --user daemon-reload
systemctl --user enable ctrlloop-worker
systemctl --user restart ctrlloop-worker
systemctl --user is-active ctrlloop-worker
{phase} finish
'''
        await sandbox.files.write_text(control+'/setup.sh',setup)
        prefix=control+'/deploy'
        runner=f'bash {shlex.quote(control+"/setup.sh")} > {shlex.quote(prefix+".log")} 2>&1; printf "%s" "$?" > {shlex.quote(prefix+".status")}'
        await sandbox.shell.run(runner,background=True)
        result={'revision':revision,'source_ref':args.source_ref,'environment':args.environment,'member':args.member,'remote':remote,'sandbox':sandbox.name,'deployed_at':datetime.now(timezone.utc).isoformat(),'status':'installing','guest_log':prefix+'.log'}
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
    parser.add_argument('--member',required=True)
    parser.add_argument('--jev-env',type=Path)
    parser.add_argument('--source-ref',help='Bundle an exact Git commit/ref without changing this checkout')
    parser.add_argument('--configure',action='store_true',help='Initial administrator setup from dedicated member credential files')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--resume',action='store_true',help='Release maintenance only after inspecting a failed deployment')
    mode.add_argument('--status',action='store_true')
    mode.add_argument('--stage-only',action='store_true',help='Upload code and install dependencies only; no secret reads, credential transfer, or worker start')
    try:asyncio.run(main(parser.parse_args()))
    except Exception as error:
        raise SystemExit(f'Worker deploy stopped ({type(error).__name__}); inspect private deployment state.') from None
