"""Read the explicit worker assignment shared by deployment and desktop setup."""
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from deployment_config import load_environment


def validate_application_origin(config, url, state=None):
    origin = urlsplit(url)
    if origin.scheme != 'https' or not origin.hostname or origin.path not in ('', '/') or origin.username or origin.password or origin.query or origin.fragment:
        raise ValueError('Worker application must be a credential-free HTTPS origin')
    allowed = {config['expected_url'], 'https://' + config['requested_domain']}
    if state:
        if state.get('environment', 'dev') != config.name:
            raise ValueError('Environment deployment state mismatch')
        if config['expected_url'] and state['url'].rstrip('/') != config['expected_url'].rstrip('/'):
            raise ValueError('Deployment state URL differs from the pinned environment origin')
        if config.name == 'prod' and state['url'].rstrip('/') == load_environment('dev')['expected_url'].rstrip('/'):
            raise ValueError('Production cannot target the development application')
        allowed.add(state['url'])
    if url.rstrip('/') not in {item.rstrip('/') for item in allowed if item}:
        raise ValueError('Application origin is not registered for this environment')


def load_target(environment, member):
    config = load_environment(environment)
    if not config['enabled']: raise ValueError('This environment is not enabled for worker operations')
    directory = config.worker_dir(member)
    target = json.loads((directory / 'target.json').read_text(encoding='utf-8'))
    if target.get('environment') != environment or target.get('member') != member:
        raise ValueError('Worker target environment/member mismatch')
    for key in ('id', 'fleet_namespace', 'fleet_claim'):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', target.get(key, '')):
            raise ValueError(f'Invalid recorded {key}')
    if not target['id'].startswith(target['fleet_namespace'] + '-'):
        raise ValueError('Sandbox is outside the assigned namespace')
    state = json.loads(config.state_path.read_text(encoding='utf-8')) if config.state_path.exists() else None
    # Member onboarding can use just its assignment, without AWS credentials or
    # another person's state. A locally available environment state must agree.
    validate_application_origin(config, target['api_url'], state)
    reference = json.loads((directory / 'fleet' / 'claim.json').read_text(encoding='utf-8-sig'))
    if reference['namespace'] != target['fleet_namespace'] or reference['claim'] != target['fleet_claim']:
        raise ValueError('Claim does not match the recorded member assignment')
    return config, target, directory, reference


def save_private(path, value):
    """Write a local credential/assignment file without printing its contents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions, then replace atomically. A failed or
    # interrupted write must not truncate the only usable activation/worker key.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                                         prefix='.'+path.name+'.',delete=False) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o600)
            stream.write(json.dumps(value, indent=2, default=str) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists(): temporary.unlink()
