#!/usr/bin/env bash
# Run in the assigned VM, under deploy_worker.py's maintenance/deploy lock.
# The original 0.24.0 driver and MCP endpoint remain installed for compatibility.
set -eu
umask 077
test ! -e /home/omarchy/.local/state/ctrlloop-worker/active-run.json
dir=/home/omarchy/.local/share/ctrlloop-browser-driver/0.28.2
mkdir -p "$dir" /home/omarchy/.config/systemd/user
if ! test -x "$dir/cua-driver"; then
    archive="$dir/cua-driver.tar.gz"
    curl -fLsS https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.28.2/cua-driver-rs-0.28.2-linux-x86_64-binary.tar.gz -o "$archive"
    printf '%s  %s\n' a1d99fd04bb4927ef5ffdbe60eb91ed8b51a2bab60e10fc604a75bd59ce69c3e "$archive" | sha256sum -c -
    tar -xzf "$archive" -C "$dir"
    chmod 700 "$dir/cua-driver"
fi
"$dir/cua-driver" --version | grep -q '0.28.2'
cat > /home/omarchy/.config/systemd/user/ctrlloop-browser-driver.service <<EOF
[Unit]
Description=CTRL Loop pinned browser-capable Cua Driver
After=default.target
[Service]
ExecStart=$dir/cua-driver serve --socket $dir/driver.sock
Environment=CUA_DRIVER_RS_ENABLE_WAYLAND=1
Restart=on-failure
RestartSec=2
UMask=0077
[Install]
WantedBy=default.target
EOF
cat > /home/omarchy/.config/systemd/user/ctrlloop-browser-mcp.service <<EOF
[Unit]
Description=CTRL Loop private browser MCP transport
After=ctrlloop-browser-driver.service
Requires=ctrlloop-browser-driver.service
[Service]
ExecStart=/usr/local/bin/supergateway --stdio "$dir/cua-driver mcp --socket $dir/driver.sock" --outputTransport streamableHttp --stateful --sessionTimeout 3600000 --port 3001 --streamableHttpPath /mcp --healthEndpoint /healthz --logLevel none
Restart=on-failure
RestartSec=2
UMask=0077
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable ctrlloop-browser-driver ctrlloop-browser-mcp
systemctl --user restart ctrlloop-browser-driver ctrlloop-browser-mcp
for attempt in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:3001/healthz >/dev/null; then exit 0; fi
    sleep 1
done
echo 'Browser MCP transport did not become ready' >&2
exit 1
