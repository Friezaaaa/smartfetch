# PowerShell Orchestrator for Phase 1 local Docker browser isolation gate.
# Native Windows Docker Desktop compatible runner.

param(
    [ValidateSet("full", "relay-only")]
    [string]$Mode = "full"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$ComposeFile = Join-Path $RepoRoot "deploy\local\docker-compose.prototype.yml"

Write-Host "=== SmartFetch Phase 1 Browser Isolation Gate (Windows / Docker Desktop) ===" -ForegroundColor Cyan
Write-Host "Mode: $Mode"
Write-Host "Compose file: $ComposeFile"

# 1. Clean up lingering containers from previous runs
docker rm -f sf-prototype-broker sf-prototype-worker 2>$null | Out-Null

# 2. Ensure named volume exists, clean stale sockets/files, and fix UID 10001 permissions
Write-Host "--> Initializing and cleaning /ipc named volume..." -ForegroundColor Yellow
docker volume create smartfetch_prototype_ipc | Out-Null
docker run --rm -v smartfetch_prototype_ipc:/ipc busybox sh -c "rm -rf /ipc/* /ipc/.* 2>/dev/null || true; mkdir -p /ipc && chown -R 10001:10001 /ipc && chmod 0700 /ipc"

# 3. Build container images
Write-Host "--> Building prototype container images..." -ForegroundColor Yellow
docker compose -f $ComposeFile build

# 4. Start the single-shot broker container in the background
Write-Host "--> Starting prototype broker..." -ForegroundColor Yellow
$BrokerId = (docker compose -f $ComposeFile run -d --name sf-prototype-broker prototype-broker python tests/browser_prototype_linux_gate.py broker).Trim()

try {
    # 5. Wait for broker to write /ipc/ready.json
    Write-Host "--> Waiting for broker readiness signal (/ipc/ready.json)..." -ForegroundColor Yellow
    $Ready = $false
    for ($i = 0; $i -lt 75; $i++) {
        $check = docker run --rm -v smartfetch_prototype_ipc:/ipc busybox sh -c "test -s /ipc/ready.json && echo ready" 2>$null
        if ($check -match "ready") {
            $Ready = $true
            break
        }
        $running = docker ps -q --no-trunc | Select-String $BrokerId
        if (-not $running) {
            Write-Host "ERROR: Broker container exited prematurely. Logs:" -ForegroundColor Red
            docker logs sf-prototype-broker
            exit 1
        }
        Start-Sleep -Milliseconds 200
    }

    if (-not $Ready) {
        Write-Host "ERROR: Timed out waiting for /ipc/ready.json" -ForegroundColor Red
        docker logs sf-prototype-broker
        exit 1
    }
    Write-Host "--> Broker is ready." -ForegroundColor Green

    # 6. Execute worker container with strict isolation
    Write-Host "--> Launching isolated worker container (network_mode: none)..." -ForegroundColor Yellow
    $WorkerArgs = if ($Mode -eq "relay-only") { @("python", "tests/browser_prototype_linux_gate.py", "relay-only") } else { @("python", "tests/browser_prototype_linux_gate.py") }
    
    $WorkerExit = 0
    try {
        & docker compose -f $ComposeFile run --rm --name sf-prototype-worker prototype-worker @WorkerArgs
        $WorkerExit = $LASTEXITCODE
    } catch {
        $WorkerExit = 1
    }

    # 7. Collect broker exit code and results
    Write-Host "--> Waiting for broker completion..." -ForegroundColor Yellow
    docker wait sf-prototype-broker | Out-Null
    $BrokerExit = $LASTEXITCODE

    Write-Host "--> Broker result envelope:" -ForegroundColor Cyan
    $resultExists = docker run --rm -v smartfetch_prototype_ipc:/ipc busybox sh -c "test -f /ipc/broker-result.json && echo exists" 2>$null
    if ($resultExists -match "exists") {
        docker run --rm -v smartfetch_prototype_ipc:/ipc busybox cat /ipc/broker-result.json
        Write-Host ""
    } else {
        Write-Host "WARN: /ipc/broker-result.json was not generated." -ForegroundColor Yellow
    }

    Write-Host "--> Execution summary: Worker exit=$WorkerExit, Broker exit=$BrokerExit"
    if ($WorkerExit -ne 0 -or $BrokerExit -ne 0) {
        Write-Host "FAIL: Gate failed." -ForegroundColor Red
        exit 1
    }

    Write-Host "SUCCESS: Phase 1 isolation gate passed cleanly." -ForegroundColor Green
    exit 0
} finally {
    Write-Host "--> Tearing down lingering containers..." -ForegroundColor Gray
    docker rm -f sf-prototype-broker sf-prototype-worker 2>$null | Out-Null
}
