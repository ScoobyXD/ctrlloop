"""Deploy an allowlisted Linux worker revision to the existing exact Fleet claim.

Credentials are read locally and transferred separately from the code archive.
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
from datetime import datetime, timezone

from cua_sandbox import Sandbox
from dotenv import dotenv_values, load_dotenv

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT/'.local/mvp'
EXPECTED_NAMESPACE='ctrlloop-omarchy-20260927-1708'
EXPECTED_SANDBOX=EXPECTED_NAMESPACE+'-e33048ae'


def bundle():
    paths=[ROOT/'pyproject.toml',ROOT/'uv.lock',*sorted((ROOT/'src').glob('*.py'))]
    digest=hashlib.sha256()
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for path in paths:
            name=path.relative_to(ROOT).as_posix();content=path.read_bytes()
            digest.update(name.encode()+b'\0'+content)
            entry=tarfile.TarInfo(name);entry.size=len(content);entry.mode=0o644
            archive.addfile(entry,io.BytesIO(content))
    return digest.hexdigest()[:16],stream.getvalue()


async def main(args):
    load_dotenv(args.fleet_env,interpolate=False)
    reference=json.loads(args.claim.read_text(encoding='utf-8-sig'))
    if reference['namespace']!=EXPECTED_NAMESPACE or reference['claim']!='omarchy-trial':
        raise RuntimeError('Refusing a different desktop claim')
    revision,archive=bundle()
    remote=f'/home/omarchy/ctrlloop-dev/releases/{revision}'
    async with Sandbox.from_dict(reference) as sandbox:
        if sandbox.name!=EXPECTED_SANDBOX:raise RuntimeError('Refusing a different bound sandbox')
        if args.status:
            r=await sandbox.shell.run('systemctl --user status ctrlloop-worker --no-pager; journalctl --user -u ctrlloop-worker -n 12 --no-pager')
            print(r.stdout);return
        await sandbox.files.make_dir(remote)
        await sandbox.files.write_bytes(remote+'/source.tar.gz',archive)
        if args.stage_only:
            setup=f'''set -eu
umask 077
cd {shlex.quote(remote)}
tar -xzf source.tar.gz
uv sync --locked --no-dev --python 3.12
.venv/bin/python -m compileall -q src
'''
            await sandbox.files.write_text(remote+'/stage.sh',setup)
            await sandbox.shell.run(f'bash {shlex.quote(remote+"/stage.sh")} > {shlex.quote(remote+"/stage.log")} 2>&1; printf "%s" "$?" > {shlex.quote(remote+"/stage.status")}',background=True)
            result={'revision':revision,'remote':remote,'sandbox':sandbox.name,'status':'staging_code_only'}
            (LOCAL/'worker.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result));return
        if await sandbox.files.exists('/home/omarchy/.local/state/ctrlloop-worker/active-run.json'):
            raise RuntimeError('An active or unsettled run exists; finish or resolve it before deployment')
        state=json.loads((LOCAL/'aws.json').read_text())
        secrets=json.loads((LOCAL/'deployment-secrets.json').read_text())
        jev=dotenv_values(args.jev_env).get('TYPESAFE_API_KEY')
        if not jev: raise RuntimeError('TYPESAFE_API_KEY is missing from the authorized local environment')
        settings={'CTRLLOOP_API_URL':state['url'],'WORKER_TOKEN':secrets['WORKER_TOKEN'],
                  'TYPESAFE_API_KEY':jev,'CTRLLOOP_REVISION':revision,
                  'CUA_LOCAL_MCP_URL':'http://127.0.0.1:3000/mcp',
                  'CTRLLOOP_REPORT_DIR':'/home/omarchy/ctrlloop-dev/reports',
                  'CTRLLOOP_WORKER_STATE':'/home/omarchy/.local/state/ctrlloop-worker',
                  'XDG_RUNTIME_DIR':'/run/user/1000','WAYLAND_DISPLAY':'wayland-1',
                  'DISPLAY':':0','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus'}
        # systemd EnvironmentFile quoting: JSON double quotes escape values safely.
        environment='\n'.join(key+'='+json.dumps(value) for key,value in settings.items())+'\n'
        env_path='/home/omarchy/ctrlloop-dev/worker.env'
        protection=await sandbox.shell.run('chmod 700 /home/omarchy/ctrlloop-dev')
        if not protection.success:raise RuntimeError('Cannot secure the worker configuration directory')
        await sandbox.files.write_text(env_path,environment)
        await sandbox.shell.run('chmod 600 '+shlex.quote(env_path))
        service=f'''[Unit]
Description=CTRL Loop assigned desktop worker
After=default.target

[Service]
Type=simple
WorkingDirectory={remote}
EnvironmentFile={env_path}
ExecStart={remote}/.venv/bin/python -m src.cloud_worker
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
'''
        await sandbox.files.make_dir('/home/omarchy/.config/systemd/user')
        await sandbox.files.write_text(remote+'/ctrlloop-worker.service',service)
        setup=f'''set -eu
umask 077
cd {shlex.quote(remote)}
tar -xzf source.tar.gz
uv sync --locked --no-dev --python 3.12
.venv/bin/python -m compileall -q src
cp ctrlloop-worker.service /home/omarchy/.config/systemd/user/ctrlloop-worker.service
systemctl --user daemon-reload
systemctl --user enable ctrlloop-worker
systemctl --user restart ctrlloop-worker
systemctl --user is-active ctrlloop-worker
'''
        await sandbox.files.write_text(remote+'/setup.sh',setup)
        runner=f'bash {shlex.quote(remote+"/setup.sh")} > {shlex.quote(remote+"/setup.log")} 2>&1; printf "%s" "$?" > {shlex.quote(remote+"/setup.status")}'
        await sandbox.shell.run(runner,background=True)
        result={'revision':revision,'remote':remote,'sandbox':sandbox.name,'deployed_at':datetime.now(timezone.utc).isoformat(),'status':'installing'}
        (LOCAL/'worker.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result))
        for _ in range(60):
            if await sandbox.files.exists(remote+'/setup.status'):
                code=(await sandbox.files.read_text(remote+'/setup.status')).strip()
                print(await sandbox.files.read_text(remote+'/setup.log'))
                result['status']='running' if code=='0' else 'failed'
                (LOCAL/'worker.json').write_text(json.dumps(result,indent=2))
                if code!='0':raise RuntimeError('Guest dependency install or service startup failed')
                return
            await asyncio.sleep(2)
        print('Install continues in guest; inspect setup.log and setup.status')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fleet-env',type=Path,required=True)
    parser.add_argument('--jev-env',type=Path,required=True)
    parser.add_argument('--claim',type=Path,required=True)
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--stage-only',action='store_true',help='Upload code and install dependencies only; no secret reads, credential transfer, or worker start')
    try:asyncio.run(main(parser.parse_args()))
    except Exception as error:
        raise SystemExit(f'Worker deploy stopped ({type(error).__name__}); inspect private deployment state.') from None
