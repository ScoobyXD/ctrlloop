#!/usr/bin/env bash
# User-space copies of official packages; no pacman install or root writes.
# Default stages and checks helpers. Use --activate after reviewing the result.
set -euo pipefail

mode=${1:---stage}
[[ $# -le 1 && ( $mode == --stage || $mode == --activate ) ]] || {
  printf 'Usage: %s [--stage|--activate]\n' "$0" >&2
  exit 2
}
[[ $EUID -ne 0 ]] || { printf 'Run as the desktop user, not root.\n' >&2; exit 1; }

base="$HOME/.local/opt/omarchy-helpers"
mkdir -p "$base/downloads" "$base/releases"
manifest=$(mktemp "$base/packages.XXXXXX")
trap 'rm -f -- "$manifest"' EXIT
pacman -Sp --print-format '%n|%v|%l|%h' xdg-terminal-exec libvips > "$manifest"
[[ -s $manifest ]] || { printf 'Package resolution returned no packages.\n' >&2; exit 1; }
release="$base/releases/$(sha256sum "$manifest" | cut -c1-16)"
mkdir -p "$release"
cp "$manifest" "$release/packages.txt"

fetch_package_file() {
  local primary=$1 fallback=$2 destination=$3
  if curl --silent --show-error --fail --location --retry 2 --proto '=https' --proto-redir '=https' "$primary" -o "$destination"; then
    return
  fi
  [[ -n $fallback ]] || return 1
  # Omarchy metadata can outlive files on its stable mirror. The official Arch
  # archive retains that exact filename; the same repository checksum and
  # detached signature still have to verify before extraction.
  printf 'Configured mirror unavailable; fetching exact official archive: %s\n' "$fallback"
  curl --silent --show-error --fail --location --retry 2 --proto '=https' --proto-redir '=https' "$fallback" -o "$destination"
}

while IFS='|' read -r package version url checksum; do
  # Keep this repair bounded to the two missing helpers and their observed
  # missing dependency closure. A changed closure needs another review.
  case "$package" in
    xdg-terminal-exec|libvips|cfitsio|libexif|imath|openjph|openexr|libcgif) ;;
    *) printf 'Unexpected dependency: %s; review before extending this repair.\n' "$package" >&2; exit 1 ;;
  esac
  case "$url" in
    https://pkgs.omarchy.org/*|https://stable-mirror.omarchy.org/*) ;;
    *) printf 'Unexpected configured package host for %s.\n' "$package" >&2; exit 1 ;;
  esac
  [[ $checksum =~ ^[0-9a-f]{64}$ ]] || { printf 'Missing repository checksum for %s.\n' "$package" >&2; exit 1; }
  filename=${url##*/}
  [[ $filename == *.pkg.tar.zst && $filename != *..* ]] || exit 1
  fallback=
  if [[ $url == https://stable-mirror.omarchy.org/* ]]; then
    fallback="https://archive.archlinux.org/packages/${package:0:1}/$package/$filename"
  fi
  archive="$base/downloads/$filename"
  if ! [[ -f $archive ]] || ! printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c --status; then
    fetch_package_file "$url" "$fallback" "$archive.part"
    printf '%s  %s\n' "$checksum" "$archive.part" | sha256sum -c --status
    mv -- "$archive.part" "$archive"
  fi
  # Omarchy's xdg-terminal-exec metadata publishes a SHA-256 checksum only.
  # Arch packages additionally have detached signatures verified by the
  # guest's existing distribution keyring; no keys or trust policy are changed.
  if [[ $package != xdg-terminal-exec ]]; then
    fetch_package_file "$url.sig" "${fallback:+$fallback.sig}" "$archive.sig"
    gpgv --keyring /etc/pacman.d/gnupg/pubring.gpg "$archive.sig" "$archive"
  fi
  bsdtar -tf "$archive" > "$archive.files"
  if grep -Eq '(^/|(^|/)\.\.(/|$))' "$archive.files"; then
    printf 'Unsafe package archive path: %s.\n' "$package" >&2
    exit 1
  fi
  # Only package payload files are extracted. Package install hooks are never run.
  bsdtar -xf "$archive" --no-same-owner --no-same-permissions -C "$release" usr
  printf 'Staged %s %s (repository SHA-256 verified).\n' "$package" "$version"
done < "$manifest"

for executable in xdg-terminal-exec vips vipsthumbnail; do
  [[ -x "$release/usr/bin/$executable" ]] || { printf 'Missing binary: %s.\n' "$executable" >&2; exit 1; }
done

mkdir -p "$release/bin"
for executable in vips vipsthumbnail; do
  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf 'helper_prefix=%q\n' "$release/usr"
    printf 'export LD_LIBRARY_PATH="$helper_prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
    # Official libvips supports relocation through VIPSHOME.
    printf 'export VIPSHOME="$helper_prefix"\n'
    printf 'exec "$helper_prefix/bin/%s" "$@"\n' "$executable"
  } > "$release/bin/$executable"
  chmod 755 "$release/bin/$executable"
  LD_LIBRARY_PATH="$release/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    ldd "$release/usr/bin/$executable" > "$release/$executable.ldd.txt"
  if grep -Eq 'not found|version .* not found' "$release/$executable.ldd.txt"; then
    cat "$release/$executable.ldd.txt" >&2
    printf 'Unresolved shared libraries; helper activation stopped.\n' >&2
    exit 1
  fi
done
ln -sfn -- "$release/usr/bin/xdg-terminal-exec" "$release/bin/xdg-terminal-exec"
"$release/bin/vips" --version
"$release/bin/vipsthumbnail" --version
"$release/bin/xdg-terminal-exec" --help > /dev/null

if [[ $mode == --activate ]]; then
  mkdir -p "$HOME/.local/bin"
  # Validate every destination before changing any active command resolution.
  for executable in xdg-terminal-exec vips vipsthumbnail; do
    destination="$HOME/.local/bin/$executable"
    if [[ -e $destination || -L $destination ]]; then
      [[ -L $destination && $(readlink -f -- "$destination") == "$release/usr/bin/$executable" ]] || \
      [[ -L $destination && $(readlink -- "$destination") == "$release/bin/$executable" ]] || {
        printf 'An unrelated local command already exists: %s.\n' "$destination" >&2
        exit 1
      }
    fi
  done
  for executable in xdg-terminal-exec vips vipsthumbnail; do
    ln -sfn -- "$release/bin/$executable" "$HOME/.local/bin/$executable"
  done
  printf 'Activated three helper commands in %s/.local/bin.\n' "$HOME"
else
  printf 'Staged without changing active PATH commands. Review %s, then rerun with --activate.\n' "$release"
fi
