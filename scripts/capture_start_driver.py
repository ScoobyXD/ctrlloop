"""Read-only reproduction of Start discovery through installed CUA Driver.

Open Start manually first. This script never opens/closes UI or calls Jev.
It connects to the existing daemon and captures only an actually discovered
Start window. Output can contain personal pinned/recommended item names.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--screenshot', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    executable = Path(os.environ['LOCALAPPDATA']) / 'Programs/Cua/cua-driver/bin/cua-driver.exe'
    replies = queue.Queue()
    with (args.output / 'stderr.txt').open('w', encoding='utf-8') as errors:
        process = subprocess.Popen(
            [str(executable), 'mcp', '--socket', r'\\.\pipe\cua-driver'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, encoding='utf-8', bufsize=1,
        )
        def receive():
            try:
                for line in process.stdout:
                    replies.put(json.loads(line))
            except Exception as error:
                replies.put(error)
            finally:
                replies.put(RuntimeError('CUA transport closed; inspect stderr.txt'))
        threading.Thread(target=receive, daemon=True).start()
        sequence = 0
        def rpc(method, params):
            nonlocal sequence
            sequence += 1
            process.stdin.write(json.dumps({'jsonrpc':'2.0','id':sequence,'method':method,'params':params})+'\n')
            process.stdin.flush()
            while True:
                response = replies.get(timeout=60)
                if isinstance(response, Exception):
                    raise response
                if response.get('id') == sequence:
                    if 'error' in response:
                        raise RuntimeError(response['error'])
                    return response['result']
        def save(name, result):
            for i, block in enumerate(result.get('content', [])):
                if block.get('type') == 'image':
                    image_path = args.output / f'{name}-{i}.png'
                    image_path.write_bytes(base64.b64decode(block['data']))
                    block['data'] = '[saved to '+image_path.name+']'
            (args.output / f'{name}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        def call(name, arguments, filename):
            result = rpc('tools/call', {'name':name,'arguments':arguments})
            save(filename, result)
            if result.get('isError'):
                raise RuntimeError(f'{name} refused/failed; inspect {filename}.json')
            return result.get('structuredContent', {})
        try:
            rpc('initialize', {'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'read-only-start-probe','version':'1'}})
            process.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n')
            process.stdin.flush()
            definitions = rpc('tools/list', {})
            save('read-tool-definitions', {'tools':[t for t in definitions['tools'] if t['name'] in ('list_windows','get_window_state','get_accessibility_tree')]})
            discovered = call('list_windows', {}, 'windows')
            matches = [w for w in discovered['windows'] if w.get('title') == 'Start' and w.get('app_name','').lower() == 'startmenuexperiencehost.exe']
            if len(matches) != 1:
                print(f'Observed {len(matches)} matching Start windows. No target synthesized and no action taken.')
                return
            window = matches[0]
            snapshot = call('get_window_state', {
                'pid':window['pid'], 'window_id':window['window_id'],
                'include_screenshot':args.screenshot, 'include_accessibility_tree':True,
                'max_elements':5000, 'max_depth':25,
            }, 'start')
            print(json.dumps({'pid':window['pid'],'window_id':window['window_id'],'count':snapshot.get('returned_element_count'),'search_controls':[e for e in snapshot.get('elements',[]) if 'search' in e.get('label','').lower()]}, indent=2))
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
