"""Shared, non-secret environment configuration for deployment and worker tools."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENTS = ("dev", "prod")


@dataclass(frozen=True)
class DeploymentConfig:
    name: str
    raw: dict

    @property
    def state_dir(self):
        return ROOT / ".local" / "environments" / self.name

    @property
    def state_path(self):
        return self.state_dir / "aws.json"

    @property
    def secret_path(self):
        return self.state_dir / "deployment-secrets.json"

    @property
    def worker_state_path(self):
        return self.state_dir / "worker.json"

    @property
    def resource_name(self):
        return self.raw["resource_name"]

    def worker_dir(self, username: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,63}", username):
            raise ValueError("Worker username must be a safe lowercase identifier")
        return self.state_dir / "workers" / username

    def __getitem__(self, key):
        return self.raw[key]


def load_environment(name: str) -> DeploymentConfig:
    if name not in ENVIRONMENTS:
        raise ValueError("Environment must be explicitly dev or prod")
    data = json.loads((ROOT / "config" / "environments" / f"{name}.json").read_text(encoding="utf-8"))
    if data.get("environment") != name or data.get("schema_version") != 1:
        raise ValueError("Environment configuration identity/version mismatch")
    if not re.fullmatch(r"\d{12}", data["account"]):
        raise ValueError("A specific AWS account is required")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,25}", data["resource_name"]):
        raise ValueError("Invalid environment resource name")
    expected_domain = "dev.controlloop.io" if name == "dev" else "controlloop.io"
    if data["requested_domain"] != expected_domain:
        raise ValueError("Requested domain must exactly match the configured environment")
    if name == "prod":
        dev = load_environment("dev")
        for key in ("resource_name", "db_name", "db_user", "app_secret_name", "openai_secret"):
            if data[key] == dev[key]:
                raise ValueError(f"Production must have a distinct {key}")
        if data.get("bootstrap_legacy_dev") or data.get("bootstrap_worker"):
            raise ValueError("Production cannot bootstrap the legacy development workspace or worker")
    return DeploymentConfig(name, data)


def migrate_legacy_dev() -> dict:
    """Deliberate non-overwriting copy; the legacy evidence remains untouched."""
    config = load_environment("dev")
    source = ROOT / ".local" / "mvp"
    config.state_dir.mkdir(parents=True, exist_ok=True)
    copied, unchanged = [], []
    for filename in ("aws.json", "deployment-secrets.json", "worker.json", "access.md"):
        old, new = source / filename, config.state_dir / filename
        if not old.exists():
            continue
        if new.exists():
            # State acquires environment metadata after migration, so never
            # overwrite it even if the old deployment subsequently diverges.
            unchanged.append(filename)
            continue
        shutil.copy2(old, new)
        copied.append(filename)
    if not config.state_path.exists() or not config.secret_path.exists():
        raise RuntimeError("Legacy dev AWS state and credential files are required")
    return {"environment": "dev", "destination": str(config.state_dir), "copied": copied,
            "already_present": unchanged, "legacy_preserved": True}
