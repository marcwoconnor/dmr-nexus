#!/usr/bin/env bash
set -euo pipefail

# cluster-proxy installer
# Usage: ./install.sh [target_user] [install_dir]
#   target_user: system user to run as (default: cort)
#   install_dir: install path (default: /home/<user>/cluster-proxy)

TARGET_USER="${1:-cort}"
INSTALL_DIR="${2:-/home/${TARGET_USER}/cluster-proxy}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="cluster-proxy"

echo "=== cluster-proxy installer ==="
echo "  User:    ${TARGET_USER}"
echo "  Install: ${INSTALL_DIR}"
echo ""

# Check we're root or can sudo
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
    echo "Not root — will use sudo for service install."
else
    SUDO=""
fi

# Check user exists
if ! id "${TARGET_USER}" &>/dev/null; then
    echo "ERROR: User '${TARGET_USER}' does not exist."
    echo "Create it first: sudo useradd -m -s /bin/bash ${TARGET_USER}"
    exit 1
fi

# Build
echo "--- Building binary..."
cd "${SCRIPT_DIR}"
if ! command -v go &>/dev/null; then
    echo "ERROR: Go not found. Install Go 1.22+ first."
    echo "  curl -fsSL https://go.dev/dl/go1.22.5.linux-amd64.tar.gz | sudo tar -C /usr/local -xz"
    echo "  export PATH=\$PATH:/usr/local/go/bin"
    exit 1
fi

CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o cluster-proxy .
echo "  Built: $(ls -lh cluster-proxy | awk '{print $5}') binary"

# Run tests
echo "--- Running tests..."
go test ./... -count=1 -timeout 30s
echo "  All tests passed."

# Create install directory
echo "--- Installing to ${INSTALL_DIR}..."
$SUDO mkdir -p "${INSTALL_DIR}"
$SUDO cp cluster-proxy "${INSTALL_DIR}/"
$SUDO chmod 755 "${INSTALL_DIR}/cluster-proxy"

# Copy sample config if no config exists
if [[ ! -f "${INSTALL_DIR}/config.json" ]]; then
    $SUDO cp config_sample.json "${INSTALL_DIR}/config.json"
    echo "  Copied sample config — EDIT ${INSTALL_DIR}/config.json before starting!"
    NEEDS_CONFIG=true
else
    echo "  Config already exists, not overwriting."
    NEEDS_CONFIG=false
fi

$SUDO chown -R "${TARGET_USER}:${TARGET_USER}" "${INSTALL_DIR}"

# Install systemd service
echo "--- Installing systemd service..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Generate service file with correct paths
$SUDO tee "${SERVICE_FILE}" > /dev/null <<EOF
[Unit]
Description=DMR Cluster Proxy (HomeBrew to Cluster-Native)
After=network.target
Wants=nexus.service

[Service]
Type=simple
User=${TARGET_USER}
Group=${TARGET_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/cluster-proxy ${INSTALL_DIR}/config.json
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "${SERVICE_NAME}"
echo "  Service installed and enabled."

# Summary
echo ""
echo "=== Installation complete ==="
echo ""
echo "Files:"
echo "  Binary:  ${INSTALL_DIR}/cluster-proxy"
echo "  Config:  ${INSTALL_DIR}/config.json"
echo "  Service: ${SERVICE_FILE}"
echo ""

if [[ "${NEEDS_CONFIG}" == "true" ]]; then
    echo "NEXT STEPS:"
    echo "  1. Edit config:    sudo -u ${TARGET_USER} nano ${INSTALL_DIR}/config.json"
    echo "     - Set local.repeater_id to match your MMDVMHost"
    echo "     - Set local.passphrase to match your MMDVMHost"
    echo "     - Set cluster.servers to your HBlink4 server addresses"
    echo "     - Set cluster.passphrase to match your HBlink4 cluster passphrase"
    echo "     - Set subscription TGs for your network"
    echo "  2. Start service:  sudo systemctl start ${SERVICE_NAME}"
    echo "  3. Check logs:     journalctl -u ${SERVICE_NAME} -f"
else
    echo "NEXT STEPS:"
    echo "  1. Start service:  sudo systemctl start ${SERVICE_NAME}"
    echo "  2. Check logs:     journalctl -u ${SERVICE_NAME} -f"
fi
echo ""
echo "OTHER COMMANDS:"
echo "  Status:   sudo systemctl status ${SERVICE_NAME}"
echo "  Stop:     sudo systemctl stop ${SERVICE_NAME}"
echo "  Restart:  sudo systemctl restart ${SERVICE_NAME}"
echo "  Uninstall: sudo systemctl disable --now ${SERVICE_NAME} && sudo rm ${SERVICE_FILE} && sudo rm -rf ${INSTALL_DIR}"
