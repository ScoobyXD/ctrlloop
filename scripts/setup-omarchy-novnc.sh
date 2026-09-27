#!/usr/bin/env bash
# Run as the Omarchy desktop user inside the claimed Fleet guest.
# https://cua.ai/docs/how-to-guides/sandbox/connect-to-omarchy-with-novnc
set -euo pipefail

NOVNC_VERSION="1.7.0"
WEBSOCKIFY_VERSION="0.13.0"
VNC_ROOT="$HOME/.local/share/omarchy-vnc"
NOVNC_ARCHIVE="$VNC_ROOT/noVNC-v${NOVNC_VERSION}.tar.gz"
NOVNC_DIR="$VNC_ROOT/noVNC-${NOVNC_VERSION}"
WEBSOCKIFY_VENV="$VNC_ROOT/websockify-venv"

install -d "$VNC_ROOT"
if [[ ! -f "$NOVNC_DIR/vnc.html" ]]; then
    if [[ ! -f "$NOVNC_ARCHIVE" ]]; then
        curl --fail --location \
            "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
            --output "${NOVNC_ARCHIVE}.part"
        mv "${NOVNC_ARCHIVE}.part" "$NOVNC_ARCHIVE"
    fi
    tar --extract --gzip --file "$NOVNC_ARCHIVE" --directory "$VNC_ROOT"
fi

if [[ ! -x "$WEBSOCKIFY_VENV/bin/python" ]]; then
    python3 -m venv "$WEBSOCKIFY_VENV"
fi
"$WEBSOCKIFY_VENV/bin/python" -m pip install "websockify==${WEBSOCKIFY_VERSION}"

install -d "$HOME/.config/systemd/user/wayvnc.service.d"
cat > "$HOME/.config/systemd/user/wayvnc.service.d/override.conf" <<'EOF'
[Service]
ExecStart=
# Avoid Cua synthetic seats; Hyprland's cursor-capture protocol expects its native seat.
ExecStart=/usr/bin/wayvnc --seat=Hyprland 127.0.0.1 5902
EOF

cat > "$HOME/.config/systemd/user/omarchy-websockify.service" <<'EOF'
[Unit]
Description=noVNC bridge for the Omarchy graphical session
After=graphical-session.target wayvnc.service
Requires=wayvnc.service
PartOf=graphical-session.target
ConditionEnvironment=WAYLAND_DISPLAY

[Service]
Type=simple
ExecStart="%h/.local/share/omarchy-vnc/websockify-venv/bin/websockify" "--web=%h/.local/share/omarchy-vnc/noVNC-1.7.0" 0.0.0.0:5900 127.0.0.1:5902
Restart=always
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
systemctl --user restart wayvnc.service
systemctl --user enable --now omarchy-websockify.service

for service in wayvnc.service omarchy-websockify.service; do
    systemctl --user is-active "$service"
done
curl --fail --silent --show-error --retry 5 --retry-delay 1 --retry-connrefused \
    --output /dev/null "http://127.0.0.1:5900/vnc.html"
printf '%s\n' "noVNC ${NOVNC_VERSION} / websockify ${WEBSOCKIFY_VERSION} ready on port 5900."
