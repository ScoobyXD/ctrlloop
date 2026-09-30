"""Apply the 720p experiment inside an idle, maintenance-gated Omarchy guest."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time


MODE = '1280x720@60'
RULE = 'hl.monitor({{ output = "", mode = "{}", position = "0x0", scale = 1 }})'


def main(verify=False):
    if Path.home() != Path('/home/omarchy'):
        raise RuntimeError('Display setup requires the assigned Omarchy desktop user')
    env = {**os.environ, 'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}'}
    instances = json.loads(subprocess.check_output(['hyprctl', '-j', 'instances'], env=env, timeout=10))
    if len(instances) != 1:
        raise RuntimeError('Expected exactly one Hyprland session')
    command = ['hyprctl', '-i', instances[0]['instance']]

    def hypr(*args):
        return subprocess.check_output([*command, *args], env=env, text=True, timeout=10)

    def monitor():
        monitors = json.loads(hypr('-j', 'monitors'))
        if len(monitors) != 1 or monitors[0]['name'] != 'Virtual-1':
            raise RuntimeError('Expected the single assigned Virtual-1 display')
        return {key: monitors[0][key] for key in
                ('name', 'width', 'height', 'refreshRate', 'x', 'y', 'scale', 'transform')}

    def is_720p(value):
        return (value['width'] == 1280 and value['height'] == 720
                and abs(value['refreshRate'] - 60) < 0.1
                and value['scale'] == 1 and value['transform'] == 0
                and value['x'] == 0 and value['y'] == 0)

    config = Path.home() / '.config/hypr/monitors.lua'
    before_text = config.read_text()
    before = monitor()
    desired = RULE.format(MODE)
    old = RULE.format('1920x1080@60')
    rules = [line.strip() for line in before_text.splitlines()
             if line.strip() and not line.lstrip().startswith('--')
             and line.strip() != 'hl.env("GDK_SCALE", "1")']
    if rules not in ([old], [desired]):
        raise RuntimeError('Monitor configuration changed; refusing to replace unrelated settings')
    if verify:
        if rules != [desired] or not is_720p(before):
            raise RuntimeError('The persistent display configuration and live output must be 1280x720')
        print(json.dumps({'display': before, 'verified': True}))
        return
    if (before['scale'] != 1 or before['transform'] != 0 or before['x'] != 0 or before['y'] != 0
            or abs(before['refreshRate'] - 60) >= 0.1):
        raise RuntimeError('Unexpected monitor layout; refusing to change more than resolution')
    if rules == [desired] and is_720p(before):
        print(json.dumps({'before': before, 'after': before, 'changed': False}))
        return
    backup_dir = Path.home() / '.local/state/ctrlloop-worker/display'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ('monitors-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.lua')
    backup.write_text(before_text)

    def replace_config(text):
        temporary = config.with_name('monitors.lua.ctrlloop-tmp')
        temporary.write_text(text)
        temporary.chmod(config.stat().st_mode & 0o777)
        temporary.replace(config)
        hypr('reload')

    try:
        replace_config(before_text.replace(old, desired))
        for _ in range(30):
            after = monitor()
            if is_720p(after):
                print(json.dumps({'before': before, 'after': after, 'backup': str(backup), 'changed': True}))
                return
            time.sleep(0.2)
        raise RuntimeError('Hyprland did not apply 1280x720; restoring previous configuration')
    except BaseException:
        replace_config(before_text)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true')
    main(parser.parse_args().verify)
