"""Bundle the historical Windows CLI from Git and the preserved local runtime."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import zipfile


SOURCE_COMMIT = "2d30d84b269e8a0493f01ff55ddb2b1ab8acaa17"
CUA_COMMIT = "fc188250b4ca8549b8e61f937fdb1fb560770e86"
REPO = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_clean(source: Path, destination: Path) -> None:
    """Copy installed software, omitting caches and virtualenv-only hooks."""
    def ignored(directory, names):
        return [name for name in names if name == "__pycache__"
                or name.endswith((".pyc", ".pyo"))
                or name in {"_virtualenv.py", "_virtualenv.pth", "direct_url.json"}]
    shutil.copytree(source, destination, ignore=ignored)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True,
                        help="Preserved desktop demo containing .runtime and .venv")
    parser.add_argument("--output", type=Path, required=True,
                        help="New folder to create; existing paths are refused")
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    output = args.output.resolve()
    zip_path = output.with_suffix(".zip")
    if output.exists() or zip_path.exists():
        parser.error("Output folder or ZIP already exists; choose a new output name.")

    preservation = json.loads((archive / "PRESERVATION.json").read_text())
    for name, expected in preservation["cua_binary_sha256"].items():
        if sha256(archive / ".runtime/cua-driver" / name) != expected:
            raise RuntimeError(f"Preserved CUA hash mismatch: {name}")

    paths = ["src", "pyproject.toml", "uv.lock", "scripts/build-cua-driver.ps1",
             "scripts/patches/cua-driver-0.28.2-left-windows-key.patch",
             "scripts/patches/cua-driver-0.28.2-direct-accessibility-only.patch"]
    historical = subprocess.check_output(
        ["git", "-c", "core.autocrlf=false", "archive", "--format=zip", SOURCE_COMMIT, "--", *paths], cwd=REPO)
    output.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(historical)) as source_zip:
        source_zip.extractall(output)
    source_hashes = {p.relative_to(output).as_posix(): sha256(p)
                     for p in sorted(output.rglob("*")) if p.is_file()}

    runtime = output / "runtime/python"
    runtime.mkdir(parents=True)
    original_python = archive / ".runtime/python"
    for source in original_python.iterdir():
        if source.is_file() and (source.suffix in {".exe", ".dll"}
                                 or source.name in {"LICENSE.txt", "BUILD"}):
            shutil.copy2(source, runtime / source.name)
    for name in ("DLLs", "tcl"):
        copy_clean(original_python / name, runtime / name)
    # Use the runtime's own standard library, with exactly the demo's installed
    # dependencies instead of its unrelated bootstrap pip/setuptools packages.
    (runtime / "Lib").mkdir()
    for source in (original_python / "Lib").iterdir():
        if source.name in {"site-packages", "__pycache__"}:
            continue
        if source.is_dir():
            copy_clean(source, runtime / "Lib" / source.name)
        elif source.suffix not in {".pyc", ".pyo"}:
            shutil.copy2(source, runtime / "Lib" / source.name)
    copy_clean(archive / ".venv/Lib/site-packages", runtime / "Lib/site-packages")
    driver = output / "runtime/cua-driver"
    driver.mkdir()
    for name in preservation["cua_binary_sha256"]:
        shutil.copy2(archive / ".runtime/cua-driver" / name, driver / name)
    license_text = subprocess.check_output(
        ["git", "show", f"{CUA_COMMIT}:LICENSE.md"],
        cwd=REPO / ".local/cua-driver/source")
    (driver / "LICENSE.md").write_bytes(license_text)

    for name in ("launch.py", "demo.cmd", "README.md", "THIRD_PARTY_NOTICES.md", ".env.example"):
        shutil.copy2(TEMPLATE / name, output / name)

    manifest = {
        "package": output.name,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": "Windows x86_64",
        "source_repository": "https://github.com/ScoobyXD/ctrlloop",
        "source_commit": SOURCE_COMMIT,
        "source_files_unmodified_sha256": source_hashes,
        "jev_model": "jev-1.13.0",
        "python": "3.11.15",
        "cua_version": "0.28.2-direct-accessibility-only",
        "cua_upstream_commit": CUA_COMMIT,
        "cua_binary_sha256": preservation["cua_binary_sha256"],
        "packaging_changes": ["Relative launcher and bundled Python dependencies",
                              "Launch-scoped CUA daemon on a private pipe",
                              "Recipient-provided credentials"],
        "excluded": ["Hermes", "Browser Use", "credentials", "existing reports",
                     "application profiles", "Git history", "virtualenv absolute paths"],
    }
    (output / "PROVENANCE.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    inventory = {p.relative_to(output).as_posix(): sha256(p)
                 for p in sorted(output.rglob("*")) if p.is_file()}
    (output / "SHA256SUMS.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for source in sorted(output.rglob("*")):
            if source.is_file():
                bundle.write(source, Path(output.name) / source.relative_to(output))
    print(json.dumps({"folder": str(output), "zip": str(zip_path),
                      "zip_bytes": zip_path.stat().st_size, "sha256": sha256(zip_path)}, indent=2))


if __name__ == "__main__":
    main()
