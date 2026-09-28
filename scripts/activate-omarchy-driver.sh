#!/usr/bin/env bash
# Explicit activation/rollback of the reviewed native entry-value fix.
# The worker stays stopped until a separate fresh native readback is verified.
set -Eeuo pipefail
activation_shell_pid=$BASHPID
readonly activation_shell_pid
umask 077
mode=${1:-}
[[ $# -eq 1 && ( $mode == --activate || $mode == --rollback ) ]] || {
  printf 'Usage: %s --activate|--rollback\n' "$0" >&2; exit 2;
}
[[ $(id -un) == omarchy && $HOME == /home/omarchy ]] || exit 1
root=/home/omarchy/ctrlloop-driver-build
commit=4b3396d9fe4bd3cf723b0eb8db83c18a8764b520
original=/opt/cua-driver/venv/lib/python3.13/site-packages/cua_driver/bin/cua-driver
original_sha=a033485d78a1c5966edd8ce29792d7f2d19a1988bcb58a6e292b6d17255262e1
unit_file=/etc/systemd/user/cua-driver.service
unit_sha=8cd9f496587bd9c470e732f2d869436584f99d838e6becdab86d190d98c89e17
dropin_dir=/home/omarchy/.config/systemd/user/cua-driver.service.d
dropin="$dropin_dir/50-ctrlloop-entry-value.conf"
socket=/home/omarchy/.cache/cua-driver/cua-driver.sock
state=/home/omarchy/.local/state/ctrlloop-worker
verify_sha() { printf '%s  %s\n' "$1" "$2" | sha256sum --check --status; }
verify_sha "$original_sha" "$original"
verify_sha "$unit_sha" "$unit_file"
[[ $(systemctl --user show cua-driver.service --property=FragmentPath --value) == "$unit_file" ]] || exit 1

manifest="$root/build-manifest.json"
if [[ $mode == --rollback ]]; then manifest="$root/activated-entry-value-manifest.json"; fi
built_sha=$(python - "$manifest" "$root" "$commit" "$original" "$original_sha" "$mode" <<'PY'
import hashlib, json, pathlib, re, sys
filename, root, commit, original, original_sha, mode = sys.argv[1:]
m = json.loads(pathlib.Path(filename).read_text())
expected = f"{root}/target-{commit}/x86_64-unknown-linux-gnu/release/cua-driver"
assert m["source_repository"] == "https://github.com/trycua/cua.git"
assert m["source_commit"] == commit and m["rust_toolchain"] == "1.97.1"
assert m["original_binary"] == original and m["original_sha256"] == original_sha
current_patch = "3f201b2498c7024de8c7ef77d7a56df8fc99e2e3a83a020a24fc4a02a67ef035"
previous_patch = "7a16a2d8befc169a5bfe709e67cff77f15b87a851884d0b3d51f283d598167ee"
allowed_patches = (current_patch,) if mode == '--activate' else (current_patch, previous_patch)
assert m["patch_sha256"] in allowed_patches
assert m["built_binary"] == expected and re.fullmatch(r"[0-9a-f]{64}", m["built_sha256"])
if mode == '--activate':
    assert hashlib.sha256(pathlib.Path(expected).read_bytes()).hexdigest() == m["built_sha256"]
print(m["built_sha256"])
PY
)
built="$root/target-$commit/x86_64-unknown-linux-gnu/release/cua-driver"
release="$root/releases/$built_sha"
backup="$root/backups/$original_sha"
destination="$release/cua-driver"
expected_dropin=$(printf '[Service]\nExecStart=\nExecStart=%s serve --socket %s\n' "$destination" "$socket")
dropin_sha=$(printf '%s\n' "$expected_dropin" | sha256sum | cut -d' ' -f1)
existing_dropins=$(systemctl --user show cua-driver.service --property=DropInPaths --value)
if [[ $mode == --activate ]]; then
  [[ -z $existing_dropins && ! -e $dropin && ! -L $dropin ]] || {
    printf 'Unexpected existing Driver drop-in; activation stopped.\n' >&2; exit 1;
  }
  [[ $(systemctl --user show cua-driver.service --property=ExecStart --value) == \
    '{ path=/usr/local/bin/cua-driver ; argv[]=/usr/local/bin/cua-driver serve --socket /home/omarchy/.cache/cua-driver/cua-driver.sock ; '* ]] || exit 1
else
  [[ $existing_dropins == "$dropin" && ! -L $dropin ]] || exit 1
  verify_sha "$dropin_sha" "$dropin"
fi

# Take the same native-endpoint lock as the worker before stopping an idle worker.
# A raced delivery is journaled before native activity and blocks activation below.
lock_dir=/run/user/1000/ctrlloop-driver-locks
mkdir -p "$lock_dir"
endpoint_sha=$(printf '%s' http://127.0.0.1:3000/mcp | sha256sum | cut -d' ' -f1)
exec 9>"$lock_dir/$endpoint_sha.lock"
flock --nonblock 9 || { printf 'A native session is active; nothing was stopped.\n' >&2; exit 1; }
[[ ! -e $state/active-run.json && ! -e $state/active-run.tmp ]] || {
  printf 'An active or unsettled run exists; nothing was stopped.\n' >&2; exit 1;
}
systemctl --user stop ctrlloop-worker.service
[[ $(systemctl --user show ctrlloop-worker.service --property=ActiveState --value) == inactive ]]
[[ ! -e $state/active-run.json && ! -e $state/active-run.tmp ]] || {
  printf 'A delivery raced the stop. Worker stays stopped; inspect its journal.\n' >&2; exit 1;
}

mkdir -p "$release" "$backup"
[[ ! -L $release && ! -L $backup && ! -L $dropin_dir ]] || exit 1
if [[ ! -e $backup/cua-driver ]]; then cp --preserve=mode,timestamps "$original" "$backup/cua-driver"; fi
if [[ ! -e $backup/cua-driver.service ]]; then cp --preserve=mode,timestamps "$unit_file" "$backup/cua-driver.service"; fi
verify_sha "$original_sha" "$backup/cua-driver"
verify_sha "$unit_sha" "$backup/cua-driver.service"
printf '%s  cua-driver\n%s  cua-driver.service\n' "$original_sha" "$unit_sha" > "$backup/SHA256SUMS"
systemctl --user cat cua-driver.service > "$release/daemon-unit-before-$mode.txt"
systemctl --user show cua-driver.service --property=ExecStart,MainPID,FragmentPath,DropInPaths > "$release/daemon-properties-before-$mode.txt"

capture_desktop() {
  python - "$1" <<'PY'
import json, os, pathlib, subprocess, sys
def property_value(unit, name):
    return subprocess.run(['systemctl', '--user', 'show', unit, '--property='+name, '--value'],
                          check=True, capture_output=True, text=True).stdout.strip()
driver_group = '/user.slice/user-1000.slice/user@1000.service/app.slice/cua-driver.service'
reported_group = property_value('cua-driver.service', 'ControlGroup')
assert reported_group in ('', driver_group), 'Unexpected Driver cgroup'
if not reported_group:
    assert property_value('cua-driver.service', 'ActiveState') in ('inactive', 'failed'), 'Missing active Driver cgroup'
result = {"processes": [], "services": {}, "driver_cgroup": driver_group, "protected_cgroups": {}}
def require_separate_cgroup(pid):
    groups = [line.split(':', 2)[2] for line in (pathlib.Path('/proc')/str(pid)/'cgroup').read_text().splitlines()
              if line.startswith('0::')]
    assert len(groups) == 1 and groups[0].startswith('/'), f'Cannot verify cgroup for PID {pid}'
    group = groups[0]
    assert group != driver_group and not group.startswith(driver_group+'/'), f'Protected PID {pid} shares Driver cgroup'
    result['protected_cgroups'][str(pid)] = group
for directory in pathlib.Path('/proc').iterdir():
    if not directory.name.isdigit(): continue
    try:
        executable = os.readlink(directory / 'exe')
        name = pathlib.Path(executable).name.lower()
        if name not in ('hyprland', 'chrome', 'google-chrome', 'wayvnc'): continue
        args = (directory / 'cmdline').read_bytes().split(b'\0')
        fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    except (OSError, IndexError): pass
    else:
        # Check every Chrome process before selecting stable main processes for
        # the before/after comparison. KillMode=control-group also kills children.
        require_separate_cgroup(directory.name)
        if name in ('chrome', 'google-chrome') and any(a.startswith(b'--type=') for a in args): continue
        result['processes'].append({"pid": int(directory.name), "starttime": fields[19], "exe": executable})
for unit in ('cua-driver-mcp.service', 'wayvnc.service', 'omarchy-websockify.service'):
    pid = property_value(unit, 'MainPID')
    assert pid.isdigit() and int(pid) > 0, f'Protected service {unit} is not running'
    require_separate_cgroup(pid)
    result['services'][unit] = pid
assert any(pathlib.Path(p['exe']).name.lower() == 'hyprland' for p in result['processes'])
assert any(pathlib.Path(p['exe']).name.lower() in ('chrome', 'google-chrome') for p in result['processes'])
assert all(pid.isdigit() and int(pid) > 0 for pid in result['services'].values())
pathlib.Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + '\n')
PY
}
capture_desktop "$release/desktop-before-$mode.json"

restore_original_daemon() {
  # Recovery must also prove that restarting this cgroup cannot kill the desktop.
  capture_desktop "$release/desktop-before-restore.json" || return 1
  verify_sha "$dropin_sha" "$dropin" || return 1
  rm -- "$dropin" || return 1
  systemctl --user daemon-reload || return 1
  systemctl --user restart cua-driver.service || return 1
  systemctl --user is-active --quiet cua-driver.service || return 1
}
changed=0
on_failure() {
  local result=$1 failed_line=$2 event=$3
  trap - ERR INT TERM
  # ERR is inherited by command substitutions. Only the original shell owns
  # service recovery; its failed command will invoke this handler once more.
  [[ $BASHPID == "$activation_shell_pid" ]] || exit "$result"
  printf 'Driver helper failed at line %s (%s, status %s).\n' "$failed_line" "$event" "$result" >&2
  if [[ $changed == 1 ]]; then
    if restore_original_daemon; then
      printf 'Activation failed; original Driver restored. Worker remains stopped.\n' >&2
    else
      printf 'Activation and automatic rollback failed. Worker remains stopped; inspect Driver only.\n' >&2
    fi
  fi
  exit "$result"
}
trap 'on_failure "$?" "$LINENO" ERR' ERR
trap 'on_failure 130 "$LINENO" INT' INT
trap 'on_failure 143 "$LINENO" TERM' TERM
if [[ $mode == --activate ]]; then
  if [[ ! -e $destination ]]; then install -m755 "$built" "$destination"; fi
  [[ ! -L $destination ]]
  verify_sha "$built_sha" "$destination"
  [[ $("$destination" --version) == 'cua-driver 0.24.0' ]]
  cp -- "$root/build-manifest.json" "$release/build-manifest.json"
  cp -- "$root/build-manifest.json" "$root/activated-entry-value-manifest.json"
  capture_desktop "$release/desktop-preflight-activate.json"
  mkdir -p "$dropin_dir"
  pending_dropin=$(mktemp "$dropin_dir/.ctrlloop-entry-value.XXXXXX")
  printf '%s\n' "$expected_dropin" > "$pending_dropin"
  chmod 644 "$pending_dropin"
  mv -T -- "$pending_dropin" "$dropin"
  changed=1
  systemctl --user daemon-reload
  systemctl --user restart cua-driver.service
  # Type=simple may report success before exec completes. Observe the exact
  # executable for up to five seconds without sending any native command.
  python - "$destination" <<'PY'
import json, os, subprocess, sys, time
expected = sys.argv[1]
deadline = time.monotonic() + 5.0
last = 'not_observed'
last_pid = None
last_executable = None
while (remaining := deadline - time.monotonic()) > 0:
    last_executable = None
    try:
        observed = subprocess.run(
            ['systemctl', '--user', 'show', 'cua-driver.service', '--property=MainPID,ActiveState'],
            check=True, capture_output=True, text=True, timeout=min(1.0, remaining))
        properties = dict(line.split('=', 1) for line in observed.stdout.splitlines() if '=' in line)
        pid = properties.get('MainPID', '')
        last_pid = pid or None
        if properties.get('ActiveState') == 'active' and pid.isdigit() and int(pid) > 0:
            last_executable = os.readlink(f'/proc/{pid}/exe')
            if last_executable == expected:
                sys.exit(0)
            last = 'executable_not_ready'
        else:
            last = 'service_not_active'
    except (OSError, subprocess.SubprocessError) as error:
        last = type(error).__name__
    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
print('Driver executable readiness timed out after five seconds: '+json.dumps(
    {'reason': last, 'observed_pid': last_pid, 'observed_executable': last_executable}), file=sys.stderr)
sys.exit(1)
PY
else
  restore_original_daemon
fi
capture_desktop "$release/desktop-after-$mode.json"
python - "$release/desktop-before-$mode.json" "$release/desktop-after-$mode.json" <<'PY'
import json, pathlib, sys
before, after = (json.loads(pathlib.Path(p).read_text()) for p in sys.argv[1:])
assert before['services'] == after['services'], 'A protected desktop service PID changed'
assert all(p in after['processes'] for p in before['processes']), 'A protected desktop process changed'
PY
verify_sha "$original_sha" "$original"
verify_sha "$unit_sha" "$unit_file"
trap - ERR INT TERM
printf '%s complete. Original /opt binary preserved. Worker remains stopped.\n' "$mode"
printf 'Establish a fresh MCP session and verify native URL readback before restarting the worker.\n'
