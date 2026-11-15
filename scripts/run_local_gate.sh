#!/usr/bin/env bash
set -euo pipefail

# Orchestrator for Phase 1 local Docker browser isolation gate.
# Runs broker and worker as coordinated batch containers using a shared named volume.

MODE="${1:-full}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/local/docker-compose.prototype.yml"

echo "=== SmartFetch Phase 1 Browser Isolation Gate ==="
echo "Mode: ${MODE}"
echo "Compose file: ${COMPOSE_FILE}"

# 1. Clean up lingering containers from previous runs
docker rm -f sf-prototype-broker sf-prototype-worker 2>/dev/null || true

# 2. Ensure named volume exists, clean stale sockets/files, and fix UID 10001 permissions
echo "--> Initializing and cleaning /ipc named volume..."
docker volume create smartfetch_prototype_ipc >/dev/null
docker run --rm -v smartfetch_prototype_ipc:/ipc busybox sh -c \
  "rm -rf /ipc/* /ipc/.* 2>/dev/null || true; mkdir -p /ipc && chown -R 10001:10001 /ipc && chmod 0700 /ipc"

# 3. Build container images
echo "--> Building prototype container images..."
docker compose -f "${COMPOSE_FILE}" build

# 4. Start the single-shot broker container in the background
echo "--> Starting prototype broker..."
BROKER_ID=$(docker compose -f "${COMPOSE_FILE}" run -d --name sf-prototype-broker prototype-broker python tests/browser_prototype_linux_gate.py broker)

cleanup() {
  echo "--> Tearing down lingering containers..."
  docker rm -f sf-prototype-broker sf-prototype-worker 2>/dev/null || true
}
trap cleanup EXIT

# 5. Wait for broker to write /ipc/ready.json
echo "--> Waiting for broker readiness signal (/ipc/ready.json)..."
READY=0
for _ in $(seq 1 75); do
  if docker exec sf-prototype-broker /usr/bin/test -s /ipc/ready.json 2>/dev/null; then
    READY=1
    break
  fi
  if ! docker ps -q --filter "name=sf-prototype-broker" --filter "status=running" | grep -q .; then
    echo "ERROR: Broker container exited before ready signal. Logs:"
    docker logs sf-prototype-broker
    exit 1
  fi
  sleep 0.2
done

if [ "${READY}" -ne 1 ]; then
  echo "ERROR: Timed out waiting for /ipc/ready.json"
  docker logs sf-prototype-broker
  exit 1
fi
echo "--> Broker is ready."

# 6. Execute worker container with strict isolation
echo "--> Launching isolated worker container (network_mode: none)..."
WORKER_EXIT_CODE=0
if [ "${MODE}" = "relay-only" ]; then
  docker compose -f "${COMPOSE_FILE}" run --rm --name sf-prototype-worker prototype-worker python tests/browser_prototype_linux_gate.py relay-only || WORKER_EXIT_CODE=$?
else
  docker compose -f "${COMPOSE_FILE}" run --rm --name sf-prototype-worker prototype-worker python tests/browser_prototype_linux_gate.py || WORKER_EXIT_CODE=$?
fi

# 7. Collect broker exit code and results
echo "--> Waiting for broker completion..."
BROKER_EXIT_CODE=0
docker wait sf-prototype-broker >/dev/null || BROKER_EXIT_CODE=$?

echo "--> Broker result envelope:"
if docker run --rm -v smartfetch_prototype_ipc:/ipc busybox test -f /ipc/broker-result.json 2>/dev/null; then
  docker run --rm -v smartfetch_prototype_ipc:/ipc busybox cat /ipc/broker-result.json
  echo ""
else
  echo "WARN: /ipc/broker-result.json was not generated."
fi

echo "--> Execution summary: Worker exit=${WORKER_EXIT_CODE}, Broker exit=${BROKER_EXIT_CODE}"
if [ "${WORKER_EXIT_CODE}" -ne 0 ] || [ "${BROKER_EXIT_CODE}" -ne 0 ]; then
  echo "FAIL: Gate failed."
  exit 1
fi

echo "SUCCESS: Phase 1 isolation gate passed cleanly."
exit 0
