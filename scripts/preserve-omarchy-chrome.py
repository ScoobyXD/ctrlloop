#!/usr/bin/env python3
"""Preview one exact Chrome scope repair; --apply makes one user-manager call.

Guest introspection: StartTransientUnit ssa(sv)a(sa(sv)) -> o (systemd 261.2).
PIDs is the scope's creation-time au property, independent of delegation:
https://github.com/systemd/systemd/blob/main/src/core/dbus-scope.c
Never stop this scope: it lasts until the preserved Chrome children exit.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

BASE = "/user.slice/user-1000.slice/user@1000.service/app.slice/"
DRIVER_GROUP = BASE + "cua-driver.service"
MAIN_SCOPE = "app-com.google.Chrome-22550.scope"
NEW_SCOPE = "ctrlloop-chrome-preserved-22550.scope"
CHROME = "/home/omarchy/.local/opt/google-chrome/chrome"
ORIGINAL = "/opt/cua-driver/venv/lib/python3.13/site-packages/cua_driver/bin/cua-driver"
ORIGINAL_SHA = "a033485d78a1c5966edd8ce29792d7f2d19a1988bcb58a6e292b6d17255262e1"
MANAGER = ["busctl", "--user", "--allow-interactive-authorization=no"]
BUS = ["org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager"]
EVIDENCE = Path("/home/omarchy/ctrlloop-driver-build/chrome-scope-preservation.json")


def run(args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=15)


def unit(name):
    output = run(["systemctl", "--user", "show", name, "--property=LoadState,ActiveState,ControlGroup,MainPID,Delegate,KillMode,DropInPaths"], False)
    return dict(line.split("=", 1) for line in output.stdout.splitlines() if "=" in line)


def process(pid):
    path = Path("/proc") / str(pid)
    fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
    uids = next(line for line in (path / "status").read_text().splitlines() if line.startswith("Uid:")).split()[1:]
    return {"pid": int(pid), "ppid": int(fields[1]), "starttime": fields[19], "exe": os.readlink(path / "exe"),
            "uids": uids, "cgroup": (path / "cgroup").read_text().strip().split(":", 2)[2]}


def snapshot():
    main = process(22550)
    assert main["starttime"] == "667893" and main["exe"] == CHROME and main["uids"] == ["1000"] * 4
    assert main["cgroup"] == BASE + MAIN_SCOPE
    driver, worker, scope = unit("cua-driver.service"), unit("ctrlloop-worker.service"), unit(MAIN_SCOPE)
    assert driver["ActiveState"] == "active" and driver["MainPID"] == "738"
    assert driver["ControlGroup"] == DRIVER_GROUP and driver["KillMode"] == "control-group" and not driver["DropInPaths"]
    assert worker["ActiveState"] == "inactive" and scope["ActiveState"] == "active" and scope["Delegate"] == "no"
    for name in ("active-run.json", "active-run.tmp"):
        assert not (Path("/home/omarchy/.local/state/ctrlloop-worker") / name).exists(), "Unsettled worker run"
    assert hashlib.sha256(Path(ORIGINAL).read_bytes()).hexdigest() == ORIGINAL_SHA
    chrome = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != 1000:
                continue
            group = (directory / "cgroup").read_text().strip().split(":", 2)[2]
            if group not in {DRIVER_GROUP, BASE + MAIN_SCOPE, BASE + NEW_SCOPE}:
                continue
            if os.readlink(directory / "exe") != CHROME:
                continue
            item = process(directory.name)
            ancestors, parent, seen = [], item["ppid"], set()
            while parent > 0 and parent not in seen:
                seen.add(parent)
                try:
                    ancestor = process(parent)
                except (OSError, StopIteration):
                    break
                ancestors.append(ancestor)
                if parent == 22550:
                    break
                parent = ancestor["ppid"]
            item["ancestry"] = ancestors
            chrome.append(item)
        except (FileNotFoundError, ProcessLookupError):
            continue
    targets = [p for p in chrome if p["cgroup"] == DRIVER_GROUP]
    for item in targets:
        assert item["uids"] == ["1000"] * 4 and any(p["pid"] == 22550 and p["starttime"] == "667893" for p in item["ancestry"])
    return {"main": main, "driver": driver, "worker": worker, "main_scope": scope, "chrome": chrome, "targets": targets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    assert os.getuid() == 1000 and os.environ.get("XDG_RUNTIME_DIR") == "/run/user/1000"
    before = snapshot()
    assert unit(NEW_SCOPE).get("LoadState") == "not-found", "One-use destination scope already exists"
    catalog = run(MANAGER + ["introspect"] + BUS).stdout
    assert any(".StartTransientUnit" in line and "ssa(sv)a(sa(sv))" in line for line in catalog.splitlines())
    assert before["targets"], "No Chrome children require preservation"
    pids = [str(p["pid"]) for p in before["targets"]]
    command = MANAGER + ["call"] + BUS + ["StartTransientUnit", "ssa(sv)a(sa(sv))", NEW_SCOPE, "fail", "4",
        "Description", "s", "Preserved existing CTRL Loop Chrome children", "Slice", "s", "app.slice",
        "Delegate", "b", "false", "PIDs", "au", str(len(pids)), *pids, "0"]
    evidence = {"mode": "apply" if args.apply else "plan", "scope": NEW_SCOPE, "before": before, "command": command}
    if not args.apply:
        print(json.dumps(evidence, indent=2))
        return
    endpoint_sha = hashlib.sha256(b"http://127.0.0.1:3000/mcp").hexdigest()
    with (Path("/run/user/1000/ctrlloop-driver-locks") / (endpoint_sha + ".lock")).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert snapshot() == before, "Desktop processes changed; obtain a new plan"
        EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n")
        response = run(command, check=False)  # Exactly one mutation; never automatically retried.
        evidence["response"] = {"returncode": response.returncode, "stdout": response.stdout, "stderr": response.stderr}
        for _ in range(20):
            if unit(NEW_SCOPE).get("ActiveState") == "active":
                break
            time.sleep(0.1)
        evidence["after"] = snapshot()
        evidence["new_scope"] = unit(NEW_SCOPE)
        EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n")
        assert response.returncode == 0, "Scope creation failed; inspect evidence without retrying"
        assert evidence["new_scope"]["ActiveState"] == "active" and evidence["new_scope"]["Delegate"] == "no"
        observed = {p["pid"]: p for p in evidence["after"]["chrome"]}
        assert not evidence["after"]["targets"], "Chrome children remain in Driver cgroup; do not restart Driver"
        for item in before["targets"]:
            current = observed[item["pid"]]
            assert current["starttime"] == item["starttime"] and current["cgroup"] == BASE + NEW_SCOPE
        assert evidence["after"]["main"] == before["main"] and evidence["after"]["driver"] == before["driver"]
        print(json.dumps({"status": "preserved", "scope": NEW_SCOPE, "pids": pids, "evidence": str(EVIDENCE)}))


if __name__ == "__main__":
    main()
