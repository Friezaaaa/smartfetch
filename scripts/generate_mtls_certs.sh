#!/usr/bin/env bash
# Generate dedicated private CA and mTLS certificates for Railway <-> VPS communication
set -euo pipefail

OUT_DIR="${1:-./deploy/vps/certs}"
VPS_HOST="${2:-worker.smartfetch.internal}"

echo "========================================================"
echo " SmartFetch mTLS Certificate Authority Generator"
echo " Target Output Directory: ${OUT_DIR}"
echo " VPS Host/IP SAN:        ${VPS_HOST}"
echo "========================================================"

mkdir -p "${OUT_DIR}"
cd "${OUT_DIR}"

# 1. Generate Root CA
echo "--> Generating Dedicated SmartFetch Root CA..."
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout ca.key -out ca.crt \
  -subj "/CN=SmartFetch Internal Root CA/O=SmartFetch/OU=Security" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

chmod 0600 ca.key
chmod 0644 ca.crt

# 2. Generate VPS Server Certificate
echo "--> Generating VPS Server Certificate..."
openssl req -newkey rsa:2048 -nodes \
  -keyout server.key -out server.csr \
  -subj "/CN=${VPS_HOST}/O=SmartFetch/OU=VPS Worker"

# Build SAN extension
EXTFILE="$(mktemp)"
trap 'rm -f "${EXTFILE}"' EXIT

cat <<EOF > "${EXTFILE}"
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = ${VPS_HOST}
DNS.2 = localhost
IP.1 = 127.0.0.1
EOF

# If VPS_HOST is an IP address, add it as IP.2
if [[ "${VPS_HOST}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "IP.2 = ${VPS_HOST}" >> "${EXTFILE}"
fi

openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 825 -sha256 -extfile "${EXTFILE}"

chmod 0644 server.crt
# Ensure server.key is readable by UID 10001 inside the container while protected
chmod 0644 server.key
rm -f server.csr

# 3. Generate Railway Client Certificate
echo "--> Generating Railway Client Certificate..."
openssl req -newkey rsa:2048 -nodes \
  -keyout client.key -out client.csr \
  -subj "/CN=railway.smartfetch.client/O=SmartFetch/OU=API Dispatch"

cat <<EOF > "${EXTFILE}"
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt -days 825 -sha256 -extfile "${EXTFILE}"

chmod 0644 client.crt
chmod 0644 client.key
rm -f client.csr

# 4. Generate Random HMAC Secret
HMAC_SECRET=$(openssl rand -hex 32)

echo ""
echo "========================================================"
echo " SUCCESS: Certificates Generated in ${OUT_DIR}"
echo "========================================================"
echo "VPS Deployment Files:"
echo "  - ${OUT_DIR}/ca.crt      (Root CA for client verification)"
echo "  - ${OUT_DIR}/server.crt  (Server certificate)"
echo "  - ${OUT_DIR}/server.key  (Server private key)"
echo ""
echo "Railway Environment Setup:"
echo "  BROWSER_WORKER_URL=https://${VPS_HOST}:8443"
echo "  BROWSER_WORKER_HMAC_KEY=${HMAC_SECRET}"
echo "  BROWSER_WORKER_CA_CERT=/path/to/ca.crt"
echo "  BROWSER_WORKER_CLIENT_CERT=/path/to/client.crt"
echo "  BROWSER_WORKER_CLIENT_KEY=/path/to/client.key"
echo "========================================================"
