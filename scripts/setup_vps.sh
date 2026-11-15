#!/usr/bin/env bash
# SmartFetch VPS Browser Worker Daemon Bootstrap & Provisioning Script
# Target Platforms: Debian 12 (Bookworm) / Ubuntu 22.04+ / 24.04 LTS
# Usage: sudo bash scripts/setup_vps.sh [vps_hostname_or_ip]
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "[-] ERROR: This script must be run as root (sudo)." >&2
  exit 1
fi

VPS_HOST="${1:-worker.smartfetch.internal}"
INSTALL_DIR="${INSTALL_DIR:-/opt/smartfetch}"

echo "========================================================"
echo " SmartFetch VPS Browser Isolation Provisioner"
echo " Target Host/IP:   ${VPS_HOST}"
echo " Install Location: ${INSTALL_DIR}"
echo "========================================================"

# 1. Update OS Packages & Install Prerequisites
echo "[1/8] Installing host prerequisites..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates \
  curl \
  gnupg \
  openssl \
  ufw \
  util-linux

# 2. Configure Kernel for Chromium Unprivileged User Namespaces
echo "[2/8] Configuring kernel unprivileged user namespaces..."
# Debian/Ubuntu allow unprivileged user namespaces; Debian kernels often require sysctl toggle
if sysctl kernel.unprivileged_userns_clone >/dev/null 2>&1; then
  sysctl -w kernel.unprivileged_userns_clone=1
  cat <<'EOF' > /etc/sysctl.d/99-smartfetch-userns.conf
kernel.unprivileged_userns_clone=1
EOF
fi

# Ensure max user namespaces are sufficient
cat <<'EOF' > /etc/sysctl.d/99-smartfetch-limits.conf
user.max_user_namespaces=28633
fs.file-max=2097152
EOF
sysctl --system >/dev/null 2>&1 || true

# 3. Install Docker Engine and Compose Plugin (if not already installed)
echo "[3/8] Checking Docker installation..."
if ! command -v docker &>/dev/null; then
  echo "--> Installing official Docker Engine..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  echo "--> Docker is already installed: $(docker --version)"
fi

# Detect host docker group GID
DOCKER_GID=$(getent group docker | cut -d: -f3 || echo "999")
echo "--> Detected host Docker group GID: ${DOCKER_GID}"

# 4. Create Host User 10001 (smartfetch)
echo "[4/8] Creating unprivileged service user (UID 10001)..."
if ! id -u 10001 >/dev/null 2>&1; then
  groupadd --gid 10001 smartfetch 2>/dev/null || true
  useradd --uid 10001 --gid 10001 --create-home --home-dir /home/smartfetch --shell /bin/false smartfetch 2>/dev/null || true
fi

# 5. Configure Firewall (UFW)
echo "[5/8] Configuring UFW firewall rules..."
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp comment "SSH administration" >/dev/null || true
ufw allow 8443/tcp comment "SmartFetch Browser Daemon mTLS" >/dev/null
ufw --force enable >/dev/null || true

# 6. Initialize IPC Volume & Application Tree
echo "[6/8] Preparing volume and directory structure..."
mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

# Ensure named volume exists with correct UID 10001 ownership
docker volume create smartfetch_vps_ipc >/dev/null
docker run --rm -v smartfetch_vps_ipc:/ipc debian:stable-slim chown -R 10001:10001 /ipc || true

mkdir -p "${INSTALL_DIR}/deploy/vps/certs"

# 7. Generate mTLS Certificates & Environment Configuration
echo "[7/8] Generating mTLS certificates and credentials..."
if [[ ! -f "${INSTALL_DIR}/deploy/vps/certs/server.crt" ]]; then
  if [[ -f "${INSTALL_DIR}/scripts/generate_mtls_certs.sh" ]]; then
    bash "${INSTALL_DIR}/scripts/generate_mtls_certs.sh" "${INSTALL_DIR}/deploy/vps/certs" "${VPS_HOST}"
  else
    echo "[-] WARNING: generate_mtls_certs.sh not found at ${INSTALL_DIR}/scripts/generate_mtls_certs.sh"
  fi
else
  echo "--> Existing certificates found in ${INSTALL_DIR}/deploy/vps/certs"
fi

# Create .env file if missing
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  HMAC_KEY="$(openssl rand -hex 32)"
  cat <<EOF > "${INSTALL_DIR}/.env"
# SmartFetch VPS Production Environment
BROWSER_WORKER_HMAC_KEY=${HMAC_KEY}
BROWSER_WORKER_HOST=0.0.0.0
BROWSER_WORKER_PORT=8443
BROWSER_WORKER_MAX_CONCURRENT=4
BROWSER_WORKER_MODE=docker
BROWSER_WORKER_DOCKER_IMAGE=smartfetch-browser-worker:prod
BROWSER_WORKER_SECCOMP=/etc/smartfetch/seccomp-chrome.json
BROWSER_WORKER_IPC_DIR=/ipc
BROWSER_WORKER_IPC_VOLUME=smartfetch_vps_ipc
BROWSER_WORKER_SSL_CERT=/etc/smartfetch/certs/server.crt
BROWSER_WORKER_SSL_KEY=/etc/smartfetch/certs/server.key
BROWSER_WORKER_SSL_CA=/etc/smartfetch/certs/ca.crt
DOCKER_GID=${DOCKER_GID}
EOF
  chmod 0600 "${INSTALL_DIR}/.env"
  echo "--> Generated fresh production .env"
fi

# Ensure correct permissions for UID 10001
chown -R 10001:10001 "${INSTALL_DIR}/deploy/vps/certs" 2>/dev/null || true
chmod 0644 "${INSTALL_DIR}/deploy/vps/certs"/*.crt 2>/dev/null || true
chmod 0644 "${INSTALL_DIR}/deploy/vps/certs"/*.key 2>/dev/null || true

# 8. Build Containers & Install Systemd Unit
echo "[8/8] Building containers and enabling systemd unit..."
if [[ -f "${INSTALL_DIR}/deploy/vps/docker-compose.vps.yml" ]]; then
  docker compose -f "${INSTALL_DIR}/deploy/vps/docker-compose.vps.yml" build
fi

if [[ -f "${INSTALL_DIR}/deploy/vps/systemd/smartfetch-daemon.service" ]]; then
  cp "${INSTALL_DIR}/deploy/vps/systemd/smartfetch-daemon.service" /etc/systemd/system/smartfetch-daemon.service
  systemctl daemon-reload
  systemctl enable smartfetch-daemon.service
  systemctl restart smartfetch-daemon.service
  echo "--> Started smartfetch-daemon.service"
fi

echo ""
echo "========================================================"
echo " SmartFetch VPS Setup Complete!"
echo "========================================================"
echo "Service Status: $(systemctl is-active smartfetch-daemon.service || echo 'inactive')"
echo ""
echo "Health check verification:"
echo "  curl -k https://127.0.0.1:8443/health"
echo ""
echo "Export these environment variables into Railway:"
echo "  BROWSER_WORKER_URL=https://${VPS_HOST}:8443"
if [[ -f "${INSTALL_DIR}/.env" ]]; then
  grep "BROWSER_WORKER_HMAC_KEY=" "${INSTALL_DIR}/.env"
fi
echo "  BROWSER_WORKER_CA_CERT=<contents of ${INSTALL_DIR}/deploy/vps/certs/ca.crt>"
echo "  BROWSER_WORKER_CLIENT_CERT=<contents of ${INSTALL_DIR}/deploy/vps/certs/client.crt>"
echo "  BROWSER_WORKER_CLIENT_KEY=<contents of ${INSTALL_DIR}/deploy/vps/certs/client.key>"
echo "========================================================"
