# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["cua-sandbox==0.7.0", "cua-fleet==0.1.17", "python-dotenv==1.2.2"]
# ///
"""One explicitly recorded Omarchy Fleet desktop per state directory."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import sys
import uuid

from cua_sandbox import Image, Pool, Sandbox, WarmPoolAutoscaling
from cua_sandbox.transport.cyclops_http_client import CyclopsHttpClient
from dotenv import load_dotenv
from fleet_sdk import (
    CyclopsClient, CyclopsConfiguration, CyclopsCredentials,
    CyclopsTokenProviderConfiguration, SdkError,
)

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local" / "omarchy-fleet"
IMAGE = (
    "public.ecr.aws/k5j5w0x5/cua-omarchy-workspace"
    "@sha256:dc448dcc986f443980dfb3586f1b91d8699091ad96a74b369016535f0dbb0342"
)
def fleet_client():
    config = dict(
        base_url="https://run.cua.ai", pool_poll_interval_ms=2000,
        pool_poll_limit=300, claim_poll_interval_ms=2000, claim_poll_limit=300,
    )
    if token := os.environ.get("FLEETS_TOKEN", "").strip():
        return CyclopsClient.connect_with_access_token(
            CyclopsTokenProviderConfiguration(**config), token, CyclopsHttpClient(),
        )
    if not (os.environ.get("CUA_CLIENT_ID") and os.environ.get("CUA_CLIENT_SECRET")):
        raise RuntimeError("Set CUA_CLIENT_ID and CUA_CLIENT_SECRET locally, or FLEETS_TOKEN.")
    return CyclopsClient.connect(CyclopsConfiguration(
        **config,
        token_url="https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token",
        credentials=CyclopsCredentials(os.environ["CUA_CLIENT_ID"], os.environ["CUA_CLIENT_SECRET"]),
    ), CyclopsHttpClient())


def save(name, value):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / name).write_text(resource_json(value) + "\n", encoding="utf-8")


def resource_json(value):
    # Generated Fleet records have empty dataclass field tables; asdict loses their data.
    # Only use this on returned resources, never a client or credential configuration.
    def encode(obj):
        if isinstance(obj, Enum):
            return obj.name
        if hasattr(obj, "__dict__"):
            return {key: item for key, item in vars(obj).items() if not key.startswith("_")}
        return str(obj)
    return json.dumps(value, indent=2, default=encode)


async def provision(client, name, *, claim_name="omarchy-trial", ttl_hours=6):
    if not name or not re.fullmatch(r"ctrlloop-omarchy-[a-z0-9-]{1,45}", name):
        raise ValueError("Use --pool ctrlloop-omarchy-<unique-lowercase-suffix>.")
    if (STATE / "pool.txt").exists():
        raise RuntimeError("A desktop is already recorded here. Use status/verify; never overwrite its identity.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,48}", claim_name):
        raise ValueError("Claim name must be a lowercase DNS label.")
    if not 1 <= ttl_hours <= 72:
        raise ValueError("Choose a finite lifetime of 1–72 hours.")
    ttl = ttl_hours * 3600
    if any(ns.name == name for ns in await client.list_namespaces()):
        raise RuntimeError("That namespace already exists; this script only creates a new dedicated trial.")
    STATE.mkdir(parents=True, exist_ok=True)
    # Record intent before the first write, including if a remote response is interrupted.
    with (STATE / "pool.txt").open("x", encoding="utf-8") as stream:
        stream.write(name + "\n")
    print(f"Creating one 4 vCPU / 6144 MiB worker in {name}; {ttl_hours}-hour pool and claim TTL.", flush=True)
    pool = await Pool.apply(
        Image.from_registry(IMAGE, os_type="linux", kind="vm"),
        name=name, replicas=1, cpu=4, memory_mb=6144,
        services={"server": 8000, "mcp": 3000, "vnc": 5900},
        autoscaling=WarmPoolAutoscaling(min_pool_size=1, initial_pool_size=1, max_pool_size=1),
        ttl_seconds_after_created=ttl,
    )
    save("pool.json", pool.resource)
    print(f"Pool created at {pool.resource.metadata.creation_timestamp}", flush=True)
    claim = await pool.create_claim(name=claim_name, ttl_seconds_after_created=ttl)
    save("claim.json", claim.to_dict())
    print(f"Claim {claim.name} recorded; waiting for the desktop service...", flush=True)
    sandbox = await claim.wait(time_to_start=1800)
    try:
        print(f"Computer-server ready: {sandbox.name}. Run verify, then viewer.", flush=True)
    finally:
        await sandbox.disconnect()  # Retain the claim for manual use and reconnects.


async def verify(sandbox):
    # PEP 723 runs this file independently; import the shared adapter without
    # loading the CLI or its TypeSafe dependencies.
    sys.path.insert(0, str(ROOT))
    from src.driver import CuaDriver, DriverError

    result = await sandbox.shell.run("pgrep -a Hyprland && cat /etc/os-release && cua-driver --version")
    if not result.success:
        raise RuntimeError(f"Guest verification failed: {result.stderr}")
    print(result.stdout.strip())
    save("guest.json", {"verified_at": datetime.now(timezone.utc).isoformat(), "output": result.stdout})
    driver = CuaDriver(STATE / "claim.json")
    try:
        try:
            await driver.start()
            observation = await driver.call("list_windows")
            (STATE / "desktop.png").write_bytes(await driver.screenshot())
            save("driver.json", {
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "serverInfo": driver.server_info, "session": driver.session_info,
                "tool": "list_windows", "observation": observation,
            })
            print(f"Desktop screenshot: {STATE / 'desktop.png'}")
            print(f"Driver MCP verified: {driver.server_info}; list_windows succeeded.")
            print(json.dumps(observation, ensure_ascii=True)[:6000])
        finally:
            await driver.close()  # End this Driver session; retain the Fleet claim.
    except DriverError as error:
        raise RuntimeError(f"Driver verification failed ({error.code}): {error}") from None


async def main(args):
    global STATE
    if args.fleet_env:
        load_dotenv(args.fleet_env, interpolate=False)
    if args.state_dir:
        STATE = args.state_dir.expanduser().resolve()
    client = fleet_client()
    if args.command == "access":
        print(f"Fleet access verified: {len(await client.list_namespaces())} namespace(s).")
        return
    if args.command == "create":
        await provision(client, args.pool, claim_name=args.claim_name, ttl_hours=args.ttl_hours)
        return
    name = (STATE / "pool.txt").read_text(encoding="utf-8").strip()
    if args.command == "cleanup":
        if args.pool != name:
            raise ValueError(f"Cleanup requires the recorded exact pool name: --pool {name}")
        try:
            await client.delete_namespace(name)
        except SdkError.Status as error:
            if error.status != 404:
                raise
        for _ in range(12):
            if all(ns.name != name for ns in await client.list_namespaces()):
                print(f"Deleted and verified absent: {name}. Local evidence retained in {STATE}.")
                return
            await asyncio.sleep(5)
        raise RuntimeError("Deletion requested but still pending; check Fleet before assuming billing stopped.")
    if args.command == "status":
        try:
            pool = await client.get_pool(name)
        except SdkError.Status as error:
            if error.status != 404:
                raise
            pool = None
        save("pool.json", pool)
        try:
            claims = await client.list_claims(name)
        except SdkError.Status as error:
            if error.status != 404:
                raise
            claims = []
        save("claims.json", claims)
        print(resource_json({"pool": pool, "claims": claims}))
        now = datetime.now(timezone.utc)
        expired = []
        for resource in ([pool] if pool else []) + claims:
            created = resource.metadata.creation_timestamp
            ttl = resource.spec.ttl_seconds_after_created
            if created and ttl is not None:
                expiry = datetime.fromisoformat(created.replace("Z", "+00:00")) + timedelta(seconds=ttl)
                print(f"{resource.metadata.name} creation-age expiry: {expiry.isoformat()}")
                if expiry <= now:
                    expired.append(resource.metadata.name)
        print("Claim lifecycle shutdown_time is included above; controller deletion is asynchronous.")
        if pool is None:
            if not claims:
                raise RuntimeError("Trial is absent (expired or deleted): no pool or claims remain.")
            raise RuntimeError(f"Trial pool is absent (expired or deleted), but {len(claims)} claim(s) remain. Inspect the namespace.")
        if not claims:
            raise RuntimeError("Trial claim is absent (expired or deleted), but the pool remains. Inspect the namespace.")
        if len(claims) != 1:
            raise RuntimeError(f"Expected one claim; Fleet reports {len(claims)}. Inspect the trial namespace.")
        claim = claims[0]
        lifecycle = claim.spec.lifecycle
        if lifecycle and lifecycle.shutdown_time:
            shutdown = datetime.fromisoformat(lifecycle.shutdown_time.replace("Z", "+00:00"))
            if shutdown <= now and claim.metadata.name not in expired:
                expired.append(claim.metadata.name)
        if expired:
            raise RuntimeError(f"Trial expiry has passed for {', '.join(expired)}; resources are still present and deletion is pending.")
        # Fleet ready_replicas counts available, unclaimed capacity. Zero is
        # expected for a fully claimed one-worker pool; verify checks its services.
        if not pool.status or pool.status.replicas != 1:
            raise RuntimeError("Fleet does not report exactly one worker. Inspect status above.")
        phase = claim.status.phase if claim.status else None
        if phase != "Bound":
            raise RuntimeError(f"Expected one Bound claim; Fleet reports phase {phase or 'unknown'}. Inspect status above.")
        print(f"Active reservation: one worker and one Bound claim; {pool.status.ready_replicas} available/unclaimed worker(s). Expiry has not passed. Use verify for live service health.")
        return
    reference = json.loads((STATE / "claim.json").read_text(encoding="utf-8"))
    async with Sandbox.from_dict(reference) as sandbox:  # Disconnects, does not release.
        if args.command == "verify":
            await verify(sandbox)
        else:
            await sandbox.files.write_text("/tmp/ctrlloop-setup-novnc.sh", (ROOT / "scripts/setup-omarchy-novnc.sh").read_text(encoding="utf-8"))
            # Fleet's HTTP transport times out sooner than a package installation.
            # Run it once in the guest and poll a completion file via short requests.
            prefix = f"/tmp/ctrlloop-novnc-{uuid.uuid4().hex}"
            runner = (
                f"timeout 240 bash /tmp/ctrlloop-setup-novnc.sh > {prefix}.log 2>&1\n"
                f"printf '%s' \"$?\" > {prefix}.status\n"
            )
            await sandbox.files.write_text(f"{prefix}.sh", runner)
            await sandbox.shell.run(f"bash {prefix}.sh", background=True)
            for _ in range(150):
                if await sandbox.files.exists(f"{prefix}.status"):
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError(f"noVNC setup completion is unknown; inspect {prefix}.log inside the guest.")
            print(await sandbox.files.read_text(f"{prefix}.log"))
            if (await sandbox.files.read_text(f"{prefix}.status")).strip() != "0":
                raise RuntimeError("noVNC setup failed; see the guest install log above.")
            response = await sandbox.services.request("vnc", method="GET", path="/vnc.html")
            response.raise_for_status()
            if "noVNC" not in response.text:
                raise RuntimeError("Fleet service did not return the noVNC page.")
        base = f"https://run.cua.ai/api/svc/{reference['namespace']}/{sandbox.name}"
        print(f"Desktop: {base}-vnc/vnc.html?autoconnect=1&resize=scale&reconnect=1")
        print(f"Driver: {base}-mcp/mcp (requires Fleet authentication)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("access", "create", "status", "verify", "viewer", "cleanup"))
    parser.add_argument("--pool", help="New pool name for create; exact recorded name for cleanup")
    parser.add_argument("--fleet-env", type=Path, help="Local Fleet credentials file; never copied into a desktop")
    parser.add_argument("--state-dir", type=Path, help="Dedicated identity/evidence directory; existing identity is never replaced")
    parser.add_argument("--claim-name", default="omarchy-trial", help="Name for a newly created claim only")
    parser.add_argument("--ttl-hours", type=int, default=6, help="New pool/claim lifetime, 1–72 hours; never renews existing resources")
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as error:
        # Never dump credentials, native SDK request bodies, or token-bearing tracebacks.
        if isinstance(error, (RuntimeError, ValueError, FileNotFoundError)):
            message = str(error)
        else:
            message = f"{type(error).__name__}; HTTP status: {getattr(error, 'status', 'unavailable')}"
        for key in ("CUA_CLIENT_ID", "CUA_CLIENT_SECRET", "FLEETS_TOKEN"):
            if secret := os.environ.get(key):
                message = message.replace(secret, "[redacted]")
        raise SystemExit(f"Fleet trial stopped: {message}") from None
