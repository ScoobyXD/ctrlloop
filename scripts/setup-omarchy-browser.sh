#!/usr/bin/env bash
# Per-user Chrome for the disposable Omarchy guest; no automatic updates.
# Run as the desktop user. This installs files/defaults but never launches the GUI.
set -euo pipefail

URL="https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
ROOT="$HOME/.local/opt/google-chrome"
WRAPPER="$HOME/.local/bin/google-chrome"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APPS="$DATA_HOME/applications"
DESKTOP="google-chrome.desktop"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/omarchy-browser"
STAMP="$(date -u +%Y%m%dT%H%M%S)-$$"

fail() { printf 'Chrome setup: %s\n' "$*" >&2; exit 1; }
[[ "$WRAPPER" =~ ^/[a-zA-Z0-9_./-]+$ ]] || fail "Omarchy's browser launcher requires an absolute path without spaces or special characters."
[[ "$(uname -m)" == "x86_64" ]] || fail "This official package requires x86_64."
[[ "$EUID" -ne 0 ]] || fail "Run this as the desktop user, not root."
for tool in curl ar tar ldd xdg-settings xdg-mime; do
    command -v "$tool" >/dev/null || fail "Required command is missing: $tool"
done
if [[ -f /proc/sys/kernel/unprivileged_userns_clone ]]; then
    [[ "$(cat /proc/sys/kernel/unprivileged_userns_clone)" == 1 ]] ||
        fail "Unprivileged user namespaces are unavailable; normal Chrome sandboxing is required."
fi

install -d "$HOME/.local/opt" "$HOME/.local/bin" "$APPS" "$CONFIG_HOME" "$CACHE"
WORK="$(mktemp -d "$CACHE/setup.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT

check_dependencies() {
    local binary="$1" dependencies
    dependencies="$(ldd "$binary" 2>&1)" || fail "Cannot inspect dependencies for $binary: $dependencies"
    if [[ "$dependencies" == *"not found"* ]]; then
        printf '%s\n' "$dependencies" >&2
        fail "Missing system libraries. Ask the image administrator to install them; nothing disables the sandbox."
    fi
}

if [[ ! -x "$ROOT/google-chrome" || ! -x "$ROOT/chrome" ]]; then
    ARCHIVE="$CACHE/google-chrome-stable_current_amd64.deb"
    if [[ ! -s "$ARCHIVE" ]]; then
        curl --fail --location --proto '=https' --proto-redir '=https' \
            --retry 3 --output "$WORK/chrome.deb" "$URL"
        mv -- "$WORK/chrome.deb" "$ARCHIVE"
    fi
    MEMBER="$(ar t "$ARCHIVE" | awk '/^data\.tar(\.(xz|gz|zst|bz2))?$/ {print; exit}')"
    [[ -n "$MEMBER" ]] || fail "The downloaded file does not contain Debian package data."
    ar p "$ARCHIVE" "$MEMBER" > "$WORK/$MEMBER"
    install -d "$WORK/unpacked"
    tar --extract --file "$WORK/$MEMBER" --directory "$WORK/unpacked" --no-same-owner
    CANDIDATE="$WORK/unpacked/opt/google/chrome"
    [[ -x "$CANDIDATE/google-chrome" && -x "$CANDIDATE/chrome" ]] || fail "The official Chrome binaries are missing."
    check_dependencies "$CANDIDATE/chrome"
    if [[ -e "$ROOT" || -L "$ROOT" ]]; then
        mv -- "$ROOT" "$ROOT.backup-$STAMP"
    fi
    mv -- "$CANDIDATE" "$ROOT"
fi
check_dependencies "$ROOT/chrome"
if [[ -x "$ROOT/chrome_crashpad_handler" ]]; then
    check_dependencies "$ROOT/chrome_crashpad_handler"
fi
[[ -f "$ROOT/product_logo_256.png" ]] || fail "The official Chrome icon is missing."

write_file() {
    local source="$1" target="$2" mode="$3"
    if [[ -f "$target" ]] && cmp -s "$source" "$target"; then
        return
    fi
    if [[ -e "$target" || -L "$target" ]]; then
        mv -- "$target" "$target.backup-$STAMP"
    fi
    install -m "$mode" "$source" "$target"
}

cat > "$WORK/wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec "$HOME/.local/opt/google-chrome/google-chrome" "$@"
EOF
write_file "$WORK/wrapper" "$WRAPPER" 755

# Omarchy extracts the first Exec token literally, so this validated path is unquoted.
cat > "$WORK/desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Google Chrome
Comment=Web browser (per-user installation; no automatic updates)
Exec=$WRAPPER %U
Icon=$ROOT/product_logo_256.png
Terminal=false
Categories=Network;WebBrowser;
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
StartupNotify=true
StartupWMClass=Google-chrome
EOF
write_file "$WORK/desktop" "$APPS/$DESKTOP" 644

# Keep the previous user defaults before xdg-utils updates them in place.
shopt -s nullglob
for defaults in "$CONFIG_HOME/mimeapps.list" "$CONFIG_HOME"/*-mimeapps.list \
    "$APPS/mimeapps.list" "$APPS"/*-mimeapps.list; do
    if [[ -e "$defaults" || -L "$defaults" ]]; then
        cp -a -- "$defaults" "$defaults.backup-$STAMP"
    fi
done
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$APPS"
fi
xdg-settings set default-web-browser "$DESKTOP"
for mime in text/html x-scheme-handler/http x-scheme-handler/https; do
    xdg-mime default "$DESKTOP" "$mime"
    [[ "$(xdg-mime query default "$mime")" == "$DESKTOP" ]] || fail "Could not save the default for $mime."
done
[[ "$(xdg-settings get default-web-browser)" == "$DESKTOP" ]] || fail "Could not save the default browser."
for field in "Name=Google Chrome" "Exec=$WRAPPER %U" \
    "Icon=$ROOT/product_logo_256.png" "Terminal=false"; do
    grep -Fxq -- "$field" "$APPS/$DESKTOP" || fail "Desktop launcher field verification failed: $field"
done
MIME_TYPES="$(sed -n 's/^MimeType=//p' "$APPS/$DESKTOP")"
for mime in text/html x-scheme-handler/http x-scheme-handler/https; do
    [[ ";$MIME_TYPES;" == *";$mime;"* ]] || fail "Desktop launcher is missing MIME type: $mime"
done
"$WRAPPER" --version
printf '%s\n' "Default browser: $DESKTOP" "Launcher: $APPS/$DESKTOP" \
    "Installed per user on this disposable VM. No automatic updates; no GUI was launched."
