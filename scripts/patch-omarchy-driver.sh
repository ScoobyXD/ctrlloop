#!/usr/bin/env bash
# Build the exact installed Driver release with the AT-SPI entry-value patch.
# Does not replace the installed Driver, edit active services, or restart anything.
set -euo pipefail
umask 077

[[ $# -eq 0 ]] || { printf 'Usage: %s\n' "$0" >&2; exit 2; }
[[ $(id -un) == omarchy && $HOME == /home/omarchy && $(uname -m) == x86_64 ]] || {
  printf 'This helper is scoped to the assigned x86_64 Omarchy desktop user.\n' >&2
  exit 1
}

commit=4b3396d9fe4bd3cf723b0eb8db83c18a8764b520
toolchain_version=1.97.1
target=x86_64-unknown-linux-gnu
original=/opt/cua-driver/venv/lib/python3.13/site-packages/cua_driver/bin/cua-driver
original_sha=a033485d78a1c5966edd8ce29792d7f2d19a1988bcb58a6e292b6d17255262e1
source_sha=de702ccbd43e6338bb7a73bddb2e5cafef0f8f0de994f0666f14ecc373922a10
previous_patched_source_sha=f13f77627eac5a6db16e08a449f4e9ea2c19eb3ac8241414d083d1f4fc6561f3
patched_source_sha=c056bf05190cc370e468c6e4eddcedb3e8c70e1d6afa95b826f2996f68097177
patch_sha=3f201b2498c7024de8c7ef77d7a56df8fc99e2e3a83a020a24fc4a02a67ef035
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
patch_file="$script_dir/patches/cua-driver-0.24.0-atspi-entry-value.patch"
build_root=/home/omarchy/ctrlloop-driver-build
source_dir="$build_root/source-$commit"
native_file=libs/cua-driver/rust/crates/platform-linux/src/atspi/native.rs
rust_root="$build_root/rust-$toolchain_version"
target_dir="$build_root/target-$commit"
unit=ctrlloop-driver-build

verify_sha() { printf '%s  %s\n' "$1" "$2" | sha256sum --check --status; }
verify_sha "$original_sha" "$original" || { printf 'Installed native binary differs from the reviewed release.\n' >&2; exit 1; }
verify_sha "$patch_sha" "$patch_file" || { printf 'Patch differs from the reviewed patch.\n' >&2; exit 1; }
[[ $("$original" --version) == 'cua-driver 0.24.0' ]] || exit 1
for command in git curl tar xz sha256sum python systemd-run systemctl cc pkg-config flock; do
  command -v "$command" >/dev/null || { printf 'Missing prerequisite: %s\n' "$command" >&2; exit 1; }
done
pkg-config --exists x11 xi xtst xext wayland-client xkbcommon
[[ $(df --output=avail -B1 /home/omarchy | tail -n1) -gt 6000000000 ]] || {
  printf 'At least 6 GB free disk is required before staging.\n' >&2; exit 1;
}
mkdir -p "$build_root/downloads" "$build_root/toolchain-manifests"
exec 9>"$build_root/build.lock"
flock --nonblock 9 || { printf 'Another Driver build is running.\n' >&2; exit 1; }
[[ ! -L $source_dir && ! -L $rust_root && ! -L $target_dir ]] || exit 1

# Pin the official repository and verify the full commit before applying any patch.
if [[ ! -d $source_dir/.git ]]; then
  [[ ! -e $source_dir ]] || { printf 'Unrecognized source directory exists.\n' >&2; exit 1; }
  git init --quiet "$source_dir"
  git -C "$source_dir" remote add origin https://github.com/trycua/cua.git
  git -C "$source_dir" fetch --depth=1 origin "$commit"
  git -C "$source_dir" checkout --quiet --detach FETCH_HEAD
fi
[[ $(git -C "$source_dir" remote get-url origin) == https://github.com/trycua/cua.git ]] || exit 1
[[ $(git -C "$source_dir" rev-parse HEAD) == "$commit" ]] || exit 1
git -C "$source_dir" diff --cached --quiet || { printf 'Unexpected staged source edits.\n' >&2; exit 1; }
[[ -z $(git -C "$source_dir" status --porcelain --untracked-files=all) ]] || {
  # Only one of the two reviewed patch revisions is reusable.
  [[ $(git -C "$source_dir" diff --name-only) == "$native_file" ]] || exit 1
  [[ -z $(git -C "$source_dir" ls-files --others --exclude-standard) ]] || exit 1
  if ! verify_sha "$patched_source_sha" "$source_dir/$native_file" && \
     ! verify_sha "$previous_patched_source_sha" "$source_dir/$native_file"; then
    printf 'Source contains edits beyond the reviewed patch.\n' >&2; exit 1;
  fi
}
# Upgrade only the exact reviewed v1 source after the single-file guard above.
# Reconstruct that one file from the pinned commit; preserve all cached builds.
if verify_sha "$previous_patched_source_sha" "$source_dir/$native_file"; then
  original_copy=$(mktemp "$build_root/native-original.XXXXXX")
  git -C "$source_dir" show "$commit:$native_file" > "$original_copy"
  verify_sha "$source_sha" "$original_copy"
  chmod 644 "$original_copy"
  mv -- "$original_copy" "$source_dir/$native_file"
fi
if verify_sha "$source_sha" "$source_dir/$native_file"; then
  git -C "$source_dir" apply --check --whitespace=error "$patch_file"
  git -C "$source_dir" apply --whitespace=error "$patch_file"
else
  git -C "$source_dir" apply --reverse --check "$patch_file"
fi
verify_sha "$patched_source_sha" "$source_dir/$native_file"
git -C "$source_dir" diff --check

# Install only the three pinned official Rust components under this build root.
# The release-side SHA-256 file is verified before executing its official installer.
for component in rustc cargo rust-std; do
  archive_name="$component-$toolchain_version-$target.tar.xz"
  archive="$build_root/downloads/$archive_name"
  checksum="$build_root/toolchain-manifests/$archive_name.sha256"
  url="https://static.rust-lang.org/dist/$archive_name"
  curl --fail --silent --show-error --location --retry 2 --proto '=https' --proto-redir '=https' "$url.sha256" -o "$checksum.part"
  read -r expected_sha _ < "$checksum.part"
  [[ $expected_sha =~ ^[0-9a-f]{64}$ ]] || exit 1
  if [[ ! -f $archive ]] || ! verify_sha "$expected_sha" "$archive"; then
    curl --fail --silent --show-error --location --retry 2 --proto '=https' --proto-redir '=https' "$url" -o "$archive.part"
    verify_sha "$expected_sha" "$archive.part"
    mv -- "$archive.part" "$archive"
  fi
  mv -- "$checksum.part" "$checksum"
  component_dir="$build_root/${archive_name%.tar.xz}"
  if [[ ! -f $rust_root/.installed-$component ]]; then
    tar -tf "$archive" > "$archive.files"
    if grep -Eq '(^/|(^|/)\.\.(/|$))' "$archive.files"; then
      printf 'Unexpected path in official toolchain archive.\n' >&2; exit 1;
    fi
    tar -xf "$archive" --no-same-owner --no-same-permissions -C "$build_root"
    "$component_dir/install.sh" --prefix="$rust_root" --disable-ldconfig
    touch "$rust_root/.installed-$component"
  fi
done
[[ $("$rust_root/bin/rustc" --version) == "rustc $toolchain_version "* ]] || exit 1
"$rust_root/bin/cargo" --version

# A separate resource-limited user service protects the existing desktop session.
# No fallback silently drops resource limits, and an OOM is a failed build.
cat > "$build_root/compile-reviewed-driver.sh" <<'BUILD'
#!/usr/bin/env bash
set -euo pipefail
for property in MemoryMax MemorySwapMax CPUQuotaPerSecUSec Nice; do
  systemctl --user show ctrlloop-driver-build.service --property="$property"
done
[[ $(systemctl --user show ctrlloop-driver-build.service --property=MemoryMax --value) == 1610612736 ]]
[[ $(systemctl --user show ctrlloop-driver-build.service --property=MemorySwapMax --value) == 268435456 ]]
[[ $(systemctl --user show ctrlloop-driver-build.service --property=CPUQuotaPerSecUSec --value) == 1s ]]
[[ $(systemctl --user show ctrlloop-driver-build.service --property=Nice --value) == 10 ]]
group=$(systemctl --user show ctrlloop-driver-build.service --property=ControlGroup --value)
[[ $group == /user.slice/*/ctrlloop-driver-build.service && $group != *..* ]]
[[ $(cat "/sys/fs/cgroup$group/memory.max") == 1610612736 ]]
[[ $(cat "/sys/fs/cgroup$group/memory.swap.max") == 268435456 ]]
read -r quota period < "/sys/fs/cgroup$group/cpu.max"
[[ $quota =~ ^[0-9]+$ && $period =~ ^[0-9]+$ && $quota -eq $period ]]
exec cargo build --locked -p cua-driver --bin cua-driver --release --features portal-input --target x86_64-unknown-linux-gnu
BUILD
systemd-run --user --wait --pipe --collect --unit="$unit" \
  --property=MemoryMax=1536M --property=MemorySwapMax=256M \
  --property=CPUQuota=100% --property=Nice=10 \
  --property="WorkingDirectory=$source_dir/libs/cua-driver/rust" \
  /usr/bin/env "PATH=$rust_root/bin:$PATH" "CARGO_HOME=$build_root/cargo-home" \
  "CARGO_TARGET_DIR=$target_dir" "RUSTC=$rust_root/bin/rustc" \
  CARGO_BUILD_JOBS=1 CARGO_PROFILE_RELEASE_LTO=off CARGO_PROFILE_RELEASE_DEBUG=0 \
  CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 CARGO_INCREMENTAL=0 \
  /usr/bin/bash "$build_root/compile-reviewed-driver.sh"

output="$target_dir/$target/release/cua-driver"
[[ $("$output" --version) == 'cua-driver 0.24.0' ]] || exit 1
ldd "$output" > "$build_root/binary.ldd.txt"
if grep -Eq 'not found|version .* not found' "$build_root/binary.ldd.txt"; then
  cat "$build_root/binary.ldd.txt" >&2; exit 1;
fi
python - "$build_root/build-manifest.json" "$commit" "$toolchain_version" "$original" "$original_sha" "$patch_file" "$patch_sha" "$output" <<'PY'
import datetime, hashlib, json, pathlib, sys
manifest, commit, toolchain, original, original_sha, patch, patch_sha, output = sys.argv[1:]
pathlib.Path(manifest).write_text(json.dumps({
    "source_repository": "https://github.com/trycua/cua.git", "source_commit": commit,
    "rust_toolchain": toolchain, "original_binary": original, "original_sha256": original_sha,
    "patch": patch, "patch_sha256": patch_sha, "built_binary": output,
    "built_sha256": hashlib.sha256(pathlib.Path(output).read_bytes()).hexdigest(),
    "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "activated": False,
}, indent=2) + "\n")
PY
printf 'Built and checked: %s\nManifest: %s/build-manifest.json\nNo active Driver files or services were changed.\n' "$output" "$build_root"
