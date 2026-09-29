"""Prepare only an explicitly identified existing Omarchy worker desktop.

Connects to a recorded claim; never creates or releases Fleet capacity. Transfers
only the reviewed setup scripts/patch, never application/provider credentials.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import sys
from urllib.parse import urlsplit

from cua_sandbox import Sandbox
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/setup-omarchy-browser.sh", "scripts/setup-omarchy-novnc.sh",
    "scripts/patch-omarchy-driver.sh", "scripts/activate-omarchy-driver.sh",
    "scripts/patches/cua-driver-0.24.0-atspi-entry-value.patch",
)
INSPECT = r'''set -eu
test "$(id -un)" = omarchy
test "$HOME" = /home/omarchy
id
uptime -s
uname -m
for path in /home/omarchy/ctrlloop-dev /home/omarchy/ctrlloop-driver-build /home/omarchy/.config/google-chrome /home/omarchy/.local/opt/google-chrome /home/omarchy/.local/share/omarchy-vnc; do
  if test -e "$path"; then du -sh "$path"; else printf 'MISSING %s\n' "$path"; fi
done
systemctl --user show cua-driver.service cua-driver-mcp.service wayvnc.service omarchy-websockify.service ctrlloop-worker.service --property=Id,ActiveState,MainPID,FragmentPath,DropInPaths
/opt/cua-driver/venv/lib/python3.13/site-packages/cua_driver/bin/cua-driver --version
sha256sum /opt/cua-driver/venv/lib/python3.13/site-packages/cua_driver/bin/cua-driver /etc/systemd/user/cua-driver.service
ss -ltnp | grep -E ':3000|:5900|:5901|:5902' || true
curl --silent --show-error --output /dev/null --write-out 'MCP HTTP %{http_code}\n' --max-time 10 http://127.0.0.1:3000/mcp
df -h /home/omarchy
'''


async def main(args):
    reference = json.loads(args.claim_file.read_text(encoding="utf-8-sig"))
    if reference.get("provider") != "fleet" or reference.get("namespace") != args.expected_namespace or reference.get("claim") != args.expected_claim:
        raise RuntimeError("Claim file does not match the explicit namespace and claim")
    if not args.expected_sandbox.startswith(args.expected_namespace + "-"):
        raise RuntimeError("Expected sandbox does not belong to the named namespace")
    load_dotenv(args.fleet_env, interpolate=False)
    if args.diagnose_transport:
        from cua_sandbox.transport.cyclops_http_client import CyclopsHttpClient
        original_execute = CyclopsHttpClient.execute

        async def execute(client, request):
            # Only hostname/method/status: never URLs, headers, body or tokens.
            safe = {"transport_host": urlsplit(request.url).hostname, "method": request.method}
            try:
                response = await original_execute(client, request)
            except Exception as exc:
                print(json.dumps({**safe, "error_type": type(exc).__name__}), flush=True)
                raise
            print(json.dumps({**safe, "status": response.status}), flush=True)
            return response
        CyclopsHttpClient.execute = execute
    output = args.output_dir or args.claim_file.resolve().parent.parent / "desktop-setup"
    output.mkdir(parents=True, exist_ok=True)
    payloads = {name: (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n") for name in FILES}
    digest = hashlib.sha256()
    for name, content in payloads.items():
        digest.update(name.encode() + b"\0" + content.encode())
    revision = digest.hexdigest()[:16]
    remote = f"/home/omarchy/ctrlloop-desktop-setup/{revision}"
    async with Sandbox.from_dict(reference) as sandbox:
        if sandbox.name != args.expected_sandbox:
            raise RuntimeError("Resolved sandbox differs from the exact authorized guest")

        async def run(command):
            result = await sandbox.shell.run(command)
            record = {"at": datetime.now(timezone.utc).isoformat(), "sandbox": sandbox.name,
                      "phase": args.phase, "success": result.success, "stdout": result.stdout, "stderr": result.stderr}
            encoded = json.dumps(record, indent=2) + "\n"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            (output / f"{args.phase}-{stamp}.json").write_text(encoded, encoding="utf-8")
            (output / f"{args.phase}-latest.json").write_text(encoded, encoding="utf-8")
            print(json.dumps(record, indent=2))
            if not result.success:
                raise RuntimeError(f"Desktop {args.phase} failed; inspect the retained output")
            return result

        if args.phase == "inspect":
            await run(INSPECT)
            return
        if args.phase == "status":
            await run(f'''set -eu
systemctl --user show ctrlloop-desktop-install.service ctrlloop-desktop-build.service ctrlloop-desktop-activate.service ctrlloop-driver-build.service --property=Id,ActiveState,SubState,Result,MainPID,ExecMainStatus,ExecMainExitTimestamp,CPUUsageNSec,MemoryCurrent,MemoryPeak
ps -u omarchy -o pid,ppid,stat,etime,time,comm | grep -E 'cargo|rustc|systemd-run|compile-review|patch-omarchy' || true
for name in build.status build.log install.status install.log activate.status activate.log; do
  if test -f {shlex.quote(remote)}/"$name"; then printf '\\n%s\\n' "$name"; tail -n 18 {shlex.quote(remote)}/"$name"; fi
done
for manifest in /home/omarchy/ctrlloop-driver-build/build-manifest.json /home/omarchy/ctrlloop-driver-build/activated-entry-value-manifest.json; do
  if test -f "$manifest"; then cat "$manifest"; fi
done
group=$(systemctl --user show ctrlloop-driver-build.service --property=ControlGroup --value)
case "$group" in /user.slice/*/ctrlloop-driver-build.service)
  for name in memory.events memory.max memory.swap.max cpu.max; do printf '\\n%s\\n' "$name"; cat "/sys/fs/cgroup$group/$name"; done
;; esac
''')
            for name in ("build-manifest.json", "activated-entry-value-manifest.json"):
                path = "/home/omarchy/ctrlloop-driver-build/" + name
                if await sandbox.files.exists(path):
                    content = await sandbox.files.read_text(path)
                    (output / name).write_text(content, encoding="utf-8")
            for phase in ("install", "build", "activate"):
                if await sandbox.files.exists(remote + "/" + phase + ".status"):
                    for suffix in ("status", "log"):
                        name = phase + "." + suffix
                        content = await sandbox.files.read_text(remote + "/" + name)
                        (output / name).write_text(content, encoding="utf-8")
            return
        if args.phase == "verify":
            health = await run(r'''set -eu
test "$(systemctl --user is-active cua-driver.service)" = active
test "$(systemctl --user is-active cua-driver-mcp.service)" = active
test "$(systemctl --user is-active omarchy-websockify.service)" = active
test "$(systemctl --user is-active wayvnc.service)" = active
pid=$(systemctl --user show cua-driver.service --property=MainPID --value)
readlink "/proc/$pid/exe"
sha256sum "/proc/$pid/exe"
python - "$pid" <<'PY'
import hashlib, json, os, pathlib, sys
manifest = json.loads(pathlib.Path('/home/omarchy/ctrlloop-driver-build/activated-entry-value-manifest.json').read_text())
executable = pathlib.Path('/proc') / sys.argv[1] / 'exe'
assert os.readlink(executable) == '/home/omarchy/ctrlloop-driver-build/releases/' + manifest['built_sha256'] + '/cua-driver'
assert hashlib.sha256(executable.read_bytes()).hexdigest() == manifest['built_sha256']
assert manifest['patch_sha256'] == '3f201b2498c7024de8c7ef77d7a56df8fc99e2e3a83a020a24fc4a02a67ef035'
print('Active executable independently matches reviewed patch/build manifest')
PY
systemctl --user show cua-driver.service cua-driver-mcp.service wayvnc.service omarchy-websockify.service ctrlloop-worker.service ctrlloop-fresh-chrome.service --property=Id,ActiveState,MainPID,FragmentPath,DropInPaths
curl --fail --silent --show-error --output /dev/null --write-out 'noVNC HTTP %{http_code}\n' --max-time 10 http://127.0.0.1:5900/vnc.html
''')
            sys.path.insert(0, str(ROOT))
            from src.driver import CuaDriver
            driver = CuaDriver(claim_file=args.claim_file)
            try:
                await driver.start()
                if driver.session_info.get("sandbox") != args.expected_sandbox:
                    raise RuntimeError("Native readback resolved a different sandbox")
                windows = await driver.call("list_windows", {"on_screen_only": True})
                snapshots = []
                for window in windows.get("windows", []):
                    if "chrome" in (str(window.get("app_name", "")) + " " + str(window.get("title", ""))).lower():
                        snapshots.append(await driver.call("get_window_state", {
                            "pid": window["pid"], "window_id": window["window_id"], "include_screenshot": False,
                        }))
                record = {"at": datetime.now(timezone.utc).isoformat(), "sandbox": args.expected_sandbox,
                          "server": driver.server_info, "windows": windows, "chrome_snapshots": snapshots,
                          "input_actions_sent": 0, "activation_verified": True,
                          "service_and_executable_readback": health.stdout,
                          "address_value_readback_verified": False,
                          "readback_scope": "Native observation and runtime identity only; no consent or address input was sent."}
                (output / "native-readback.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
                print(json.dumps({"sandbox": args.expected_sandbox, "server": driver.server_info,
                                  "native_windows": windows, "chrome_snapshot_count": len(snapshots),
                                  "input_actions_sent": 0, "readback": str(output / "native-readback.json")}, indent=2))
            finally:
                await driver.close()
            return
        # Every mutating phase first validates the user/session and absence of
        # configured work, so this helper cannot replace an active worker setup.
        await run('''set -eu
test "$(id -un)" = omarchy
test "$HOME" = /home/omarchy
test ! -e /home/omarchy/ctrlloop-dev/worker.env
test ! -e /home/omarchy/.local/state/ctrlloop-worker/active-run.json
test ! -e /home/omarchy/.local/state/ctrlloop-worker/active-run.tmp
test "$(systemctl --user show ctrlloop-worker.service --property=ActiveState --value)" != active
''')
        for name, content in payloads.items():
            destination = remote + "/" + name
            await sandbox.files.make_dir(str(Path(destination).parent).replace("\\", "/"))
            await sandbox.files.write_text(destination, content)
        (output / "setup-manifest.json").write_text(json.dumps({
            "sandbox": sandbox.name, "namespace": args.expected_namespace, "claim": args.expected_claim,
            "revision": revision, "remote": remote,
            "files": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in payloads.items()},
            "credentials_transferred": False,
        }, indent=2) + "\n", encoding="utf-8")
        if args.phase == "install":
            script = f'''#!/usr/bin/env bash
set -euo pipefail
umask 077
bash {shlex.quote(remote + '/scripts/setup-omarchy-browser.sh')}
bash {shlex.quote(remote + '/scripts/setup-omarchy-novnc.sh')}
'''
        elif args.phase == "build":
            script = f'''#!/usr/bin/env bash
set -euo pipefail
umask 077
bash {shlex.quote(remote + '/scripts/patch-omarchy-driver.sh')}
'''
        else:
            if not args.allow_fresh_placeholder:
                raise RuntimeError("Fresh activation requires --allow-fresh-placeholder for the inert worker unit")
            script = f'''#!/usr/bin/env bash
set -euo pipefail
umask 077
test "$(cat {shlex.quote(remote + '/install.status')})" = 0
test "$(cat {shlex.quote(remote + '/build.status')})" = 0
unit=/home/omarchy/.config/systemd/user/ctrlloop-worker.service
if ! systemctl --user cat ctrlloop-worker.service >/dev/null 2>&1; then
  test ! -e "$unit"
  install -d -m 700 /home/omarchy/.config/systemd/user
  printf '[Unit]\\nDescription=Fresh desktop paused worker placeholder\\n[Service]\\nType=oneshot\\nExecStart=/usr/bin/true\\n' > "$unit"
  systemctl --user daemon-reload
else
  grep -Fxq 'Description=Fresh desktop paused worker placeholder' "$unit"
fi
# The unchanged activation helper requires a Chrome process outside the Driver
# cgroup. The fresh first-run dialog is left for the user; nothing accepts it.
if ! pgrep -x chrome >/dev/null; then
  test ! -e /home/omarchy/.config/google-chrome
  systemd-run --user --unit=ctrlloop-fresh-chrome --collect --setenv=DISPLAY=:0 --setenv=WAYLAND_DISPLAY=wayland-1 --setenv=XDG_RUNTIME_DIR=/run/user/1000 /home/omarchy/.local/bin/google-chrome --force-renderer-accessibility --ozone-platform=wayland about:blank
  for attempt in $(seq 1 30); do pgrep -x chrome >/dev/null && break; sleep 1; done
fi
bash {shlex.quote(remote + '/scripts/activate-omarchy-driver.sh')} --activate
'''
        runner = remote + "/" + args.phase + "-stage.sh"
        await sandbox.files.write_text(runner, script)
        # An independent user unit survives the short Fleet transport request.
        wrapper = remote + "/" + args.phase + "-runner.sh"
        await sandbox.files.write_text(wrapper, f'''#!/usr/bin/env bash
umask 077
bash {shlex.quote(runner)} > {shlex.quote(remote + '/' + args.phase + '.log')} 2>&1
result=$?
printf '%s\\n' "$result" > {shlex.quote(remote + '/' + args.phase + '.status')}
exit "$result"
''')
        await run(f"systemd-run --user --unit=ctrlloop-desktop-{args.phase} --collect /usr/bin/bash {shlex.quote(wrapper)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("inspect", "install", "build", "activate", "status", "verify"))
    parser.add_argument("--claim-file", type=Path, required=True)
    parser.add_argument("--expected-namespace", required=True)
    parser.add_argument("--expected-claim", required=True)
    parser.add_argument("--expected-sandbox", required=True)
    parser.add_argument("--fleet-env", type=Path, default=ROOT / ".env.fleet")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--allow-fresh-placeholder", action="store_true")
    parser.add_argument("--diagnose-transport", action="store_true")
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as error:
        # SDK exception bodies may contain request details; keep failures safe.
        detail = str(error) if isinstance(error, (RuntimeError, ValueError, FileNotFoundError)) else type(error).__name__
        raise SystemExit(json.dumps({"error": detail})) from None
