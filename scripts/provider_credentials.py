"""Retrieve the existing shared provider credentials using an AWS SSO profile.

Only the explicit seed-jev operation writes AWS. Neither operation creates or
rotates a provider key; member worker tokens are never exported by this tool.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values

from deployment_config import ROOT, load_environment

PROVIDER_NAMES = ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "CUA_CLIENT_ID", "CUA_CLIENT_SECRET")


def ignored_path(path):
    """Keep plaintext credentials inside this checkout's ignored local storage."""
    path = path.absolute()
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Credential paths must not traverse symbolic links")
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        raise ValueError("Credential files must be inside the checkout") from None
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", str(relative)], cwd=ROOT,
                             capture_output=True, check=False)
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", str(relative)], cwd=ROOT,
                             capture_output=True, check=False)
    if ignored.returncode != 0 or tracked.returncode == 0:
        raise ValueError("Credential files must be ignored and untracked by Git")
    return path


def required_value(value, name):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"Missing or invalid {name}; no credential values were printed")
    return value


def env_text(values):
    """Single quotes keep shell expansions inert and work with python-dotenv."""
    lines = ["# Shared provider credentials. Private local file; never commit or print."]
    for name in PROVIDER_NAMES:
        value = required_value(values.get(name), name)
        # Shell and dotenv disagree on escaped quotes/backslashes. Fail rather
        # than silently alter an opaque credential or emit executable shell.
        if "'" in value or "\\" in value:
            raise ValueError(f"{name} cannot be represented in a shared shell/dotenv file")
        lines.append(f"{name}='{value}'")
    result = "\n".join(lines) + "\n"
    if dict(dotenv_values(stream=io.StringIO(result), interpolate=False)) != values:
        raise ValueError("Credential file round-trip validation failed")
    return result


def write_env(path, values):
    path = ignored_path(path)
    text = env_text(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # NamedTemporaryFile is owner-only from creation on macOS/Linux.
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                prefix=".shared-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if dict(dotenv_values(path, interpolate=False)) != values:
        raise ValueError("Saved credential file verification failed")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("Credential file is not owner-only")


def aws_clients(config, profile):
    if not config["enabled"]:
        raise ValueError("Selected environment is disabled")
    session = boto3.Session(profile_name=profile or config["default_profile"], region_name=config["region"])
    if session.client("sts").get_caller_identity()["Account"] != config["account"]:
        raise ValueError("AWS SSO profile belongs to a different account")
    return session.client("secretsmanager")


def app_secret(sm, config):
    result = sm.get_secret_value(SecretId=config["app_secret_name"], VersionStage="AWSCURRENT")
    prefix = f"arn:aws:secretsmanager:{config['region']}:{config['account']}:secret:{config['app_secret_name']}-"
    if not result["ARN"].startswith(prefix):
        raise ValueError("Project secret does not belong to the selected environment")
    value = json.loads(result["SecretString"])
    if not isinstance(value, dict):
        raise ValueError("Project secret must be a JSON object")
    return result, value


def fetch(sm, config):
    app, values = app_secret(sm, config)
    openai = sm.get_secret_value(SecretId=config["openai_secret"], VersionStage="AWSCURRENT")
    prefix = f"arn:aws:secretsmanager:{config['region']}:{config['account']}:secret:"
    if not openai["ARN"].startswith(prefix):
        raise ValueError("OpenAI secret belongs to another account or region")
    # ECS already injects this separate secret's entire SecretString unchanged.
    providers = {name: required_value(values.get(name), name) for name in PROVIDER_NAMES if name != "OPENAI_API_KEY"}
    providers["OPENAI_API_KEY"] = required_value(openai.get("SecretString"), "OPENAI_API_KEY")
    destination = config.state_dir / "shared.env"
    write_env(destination, providers)
    return {"environment": config.name, "saved": str(destination), "variables": list(PROVIDER_NAMES),
            "app_secret_version": app["VersionId"], "openai_secret_version": openai["VersionId"],
            "exact_file_readback": True, "worker_tokens_exported": False}


def seed_jev(sm, config, source):
    key = required_value(dotenv_values(ignored_path(source), interpolate=False).get("TYPESAFE_API_KEY"), "TYPESAFE_API_KEY")
    original, values = app_secret(sm, config)
    if "TYPESAFE_API_KEY" in values:
        if values["TYPESAFE_API_KEY"] != key:
            raise ValueError("An existing different Jev key is stored; refusing to replace or rotate it")
        return {"environment": config.name, "changed": False, "same_existing_jev_key": True,
                "unrelated_fields_preserved": True, "app_secret_version": original["VersionId"]}
    merged = {**values, "TYPESAFE_API_KEY": key}
    version = str(uuid4())
    stage = "ctrlloop-import-" + version
    sm.put_secret_value(SecretId=original["ARN"], ClientRequestToken=version,
                        SecretString=json.dumps(merged), VersionStages=[stage])
    try:
        # RemoveFromVersionId is checked by AWS: a concurrent AWSCURRENT move
        # makes this fail instead of overwriting the other writer's changes.
        sm.update_secret_version_stage(SecretId=original["ARN"], VersionStage="AWSCURRENT",
                                       MoveToVersionId=version, RemoveFromVersionId=original["VersionId"])
        current, readback = app_secret(sm, config)
        if current["VersionId"] != version or readback != merged:
            raise ValueError("Project secret changed during import; inspect current version before retrying")
    finally:
        sm.update_secret_version_stage(SecretId=original["ARN"], VersionStage=stage, RemoveFromVersionId=version)
    return {"environment": config.name, "changed": True, "same_existing_jev_key": True,
            "unrelated_fields_preserved": all(readback.get(k) == v for k, v in values.items()),
            "app_secret_version": version, "previous_app_secret_version": original["VersionId"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "seed-jev"))
    parser.add_argument("--environment", choices=("dev", "prod"), required=True)
    parser.add_argument("--profile", help="AWS SSO profile; defaults to the selected environment's profile")
    parser.add_argument("--jev-env", type=Path, help="Existing ignored Jev env file; required only for seed-jev")
    args = parser.parse_args()
    if (args.command == "seed-jev") != bool(args.jev_env):
        parser.error("--jev-env is required only with seed-jev")
    # SDK debug logs can include decrypted payloads. This CLI never enables them.
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    try:
        config = load_environment(args.environment)
        sm = aws_clients(config, args.profile)
        result = seed_jev(sm, config, args.jev_env) if args.command == "seed-jev" else fetch(sm, config)
        print(json.dumps(result))
    except ClientError as exc:
        # Never render provider responses, request bodies, or decrypted values.
        print(json.dumps({"error": "AWS request failed", "code": exc.response["Error"]["Code"]}))
        raise SystemExit(1) from None
    except (BotoCoreError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": "Credential operation failed", "type": type(exc).__name__,
                          "hint": "Check SSO login, configured secret fields and ignored local paths; values were not printed"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
