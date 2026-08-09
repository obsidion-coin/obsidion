<#
One-shot installer for an Obsidion seed node on a Windows machine.

Run in an **Administrator** PowerShell on the machine that will serve:

    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\seed-setup.ps1 -DuckDnsDomain yourname -DuckDnsToken <token>

It installs the code, opens the P2P port, registers the node as a scheduled
task that starts at boot, and keeps a DuckDNS hostname pointed at this
connection. Safe to re-run; it is idempotent.

**This node carries no wallet.** A seed relays blocks and transactions; it
never mines and never spends, so there are no keys on it and nothing worth
stealing. Mine separately, on a different machine, where the wallet lives.

Prefer a machine that holds nothing you care about. A seed accepts inbound
connections from strangers, which is exactly what you do not want pointed at
your main computer.

KEEP THIS FILE PURE ASCII. Windows PowerShell 5.1 parses a BOM-less .ps1 as
ANSI, so a UTF-8 em dash arrives as three CP1252 characters ending in 0x94 -
a right smart quote, which PowerShell honours as a real string delimiter. One
dash in a comment silently inverts quote parity for the rest of the file and
the script fails to parse on the target machine, far from whoever edited it.
A BOM would also fix it, but only if the BOM survives being downloaded; ASCII
survives everything.
#>

[CmdletBinding()]
param(
    # The DuckDNS name WITHOUT the .duckdns.org suffix, e.g. "obsidion".
    # Omit both DuckDNS parameters to skip dynamic DNS entirely.
    [string]$DuckDnsDomain,
    [string]$DuckDnsToken,
    [string]$InstallDir = "C:\Obsidion",
    [int]$Port = 9444,
    [string]$RepoUrl = "https://github.com/obsidion-coin/obsidion.git"
)

$ErrorActionPreference = "Stop"

function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "   ok   $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "  warn  $message" -ForegroundColor Yellow }

Write-Host "== Obsidion seed setup (Windows) ==" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an Administrator PowerShell. It needs to open a firewall port and register a startup task."
}

if (-not (Get-Command "python" -ErrorAction SilentlyContinue)) {
    throw "'python' is not installed or not on PATH. Install it first:  winget install Python.Python.3.12"
}

# git is preferred but not required. An old machine often has Python and no
# git, and a seed node is too useful to block on a version-control tool.
$hasGit = [bool](Get-Command "git" -ErrorAction SilentlyContinue)
if ($hasGit) {
    Write-Ok "administrator, python and git present"
} else {
    Write-Ok "administrator and python present"
    Write-Warn "git not found; falling back to a zip download (updates will re-download)"
}

# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------
Write-Step "Fetching the published code"
$repoDir = Join-Path $InstallDir "obsidion"
if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir | Out-Null }

if ($hasGit) {
    if (Test-Path (Join-Path $repoDir ".git")) {
        git -C $repoDir pull --ff-only
    } else {
        git clone $RepoUrl $repoDir
    }
} else {
    # GitHub serves every branch as a zip. Expand-Archive unpacks it into a
    # "<repo>-<branch>" folder, so move the contents into place afterwards.
    $zipUrl = $RepoUrl -replace "\.git$", ""
    $zipUrl = "$zipUrl/archive/refs/heads/master.zip"
    $zipPath = Join-Path $env:TEMP "obsidion-master.zip"
    $staging = Join-Path $env:TEMP "obsidion-extract"

    # TLS 1.2 is not the default on older Windows builds, and GitHub refuses
    # anything less - the download fails with an unhelpful connection error.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing

    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
    Expand-Archive -Path $zipPath -DestinationPath $staging -Force
    $extracted = Get-ChildItem $staging -Directory | Select-Object -First 1

    # Keep .venv and data if this is a re-run; replace only the source.
    foreach ($item in Get-ChildItem $extracted.FullName -Force) {
        $target = Join-Path $repoDir $item.Name
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        Move-Item $item.FullName $target
    }
    Remove-Item $staging -Recurse -Force
    Remove-Item $zipPath -Force
}

$python = Join-Path $repoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { python -m venv (Join-Path $repoDir ".venv") }
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet ecdsa flask
Write-Ok "code and dependencies installed at $repoDir"

# Prove this machine derives the expected chain before it serves anyone.
# A seed on the wrong parameters is worse than no seed: it answers, and then
# nobody who reaches it can sync.
Write-Step "Verifying the genesis block derives correctly here"
Push-Location $repoDir
try {
    & $python -c "from obsidion.genesis import genesis_hash; from obsidion.params import MAINNET; print('genesis', genesis_hash(MAINNET)[::-1].hex())"
    if ($LASTEXITCODE -ne 0) { throw "genesis derivation failed" }
} finally {
    Pop-Location
}
Write-Ok "compare the hash above against the one in README.md"

# ---------------------------------------------------------------------------
# Firewall - the P2P port only. The wallet RPC stays on loopback, always.
# ---------------------------------------------------------------------------
Write-Step "Opening TCP $Port inbound"
$ruleName = "Obsidion P2P $Port"
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Write-Ok "firewall rule already present"
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
        -LocalPort $Port -Action Allow -Profile Any | Out-Null
    Write-Ok "firewall rule created"
}
Write-Warn "Windows Firewall is only the first door. Your ROUTER must also forward"
Write-Warn "TCP $Port to this machine, or the outside world still cannot reach it."

# ---------------------------------------------------------------------------
# The node itself, as a boot-start scheduled task
# ---------------------------------------------------------------------------
Write-Step "Registering the node to start at boot"
$dataDir = Join-Path $InstallDir "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }

# No --wallet and no --mine: this node relays, and nothing else.
$nodeArgs = "-m obsidion.node --network mainnet --datadir `"$dataDir`" --host 0.0.0.0 --port $Port"
$action = New-ScheduledTaskAction -Execute $python -Argument $nodeArgs -WorkingDirectory $repoDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principalTask = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0

Register-ScheduledTask -TaskName "ObsidionSeed" -Action $action -Trigger $trigger `
    -Principal $principalTask -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "ObsidionSeed"
Write-Ok "task 'ObsidionSeed' registered and started"

# ---------------------------------------------------------------------------
# DuckDNS - so the published hostname follows this connection's IP
# ---------------------------------------------------------------------------
if ($DuckDnsDomain -and $DuckDnsToken) {
    Write-Step "Pointing $DuckDnsDomain.duckdns.org at this connection"

    # Empty ip= tells DuckDNS to use the request's source address, which is
    # this connection's public IP as seen from outside - exactly what a peer
    # would resolve.
    $updateUrl = "https://www.duckdns.org/update?domains=$DuckDnsDomain&token=$DuckDnsToken&ip="
    $response = (Invoke-WebRequest -Uri $updateUrl -UseBasicParsing).Content.Trim()
    if ($response -ne "OK") {
        throw "DuckDNS rejected the update (returned '$response'). Check the domain and token."
    }
    Write-Ok "DuckDNS updated"

    # A home IP changes without warning; a stale record is a seed nobody can
    # reach. Re-assert it every five minutes.
    $updateScript = Join-Path $InstallDir "duckdns-update.ps1"
    Set-Content -Path $updateScript -Encoding utf8 -Value @"
# Keeps $DuckDnsDomain.duckdns.org pointed at this connection. Written by seed-setup.ps1.
try {
    Invoke-WebRequest -Uri "$updateUrl" -UseBasicParsing | Out-Null
} catch {
    # A failed update is not worth crashing over; the next run retries.
}
"@
    # The token is a credential. Keep the file to administrators only.
    icacls $updateScript /inheritance:r /grant:r "Administrators:(F)" "SYSTEM:(F)" | Out-Null

    $ddnsAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$updateScript`""
    $ddnsTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName "ObsidionDuckDNS" -Action $ddnsAction -Trigger $ddnsTrigger `
        -Principal $principalTask -Force | Out-Null
    Write-Ok "task 'ObsidionDuckDNS' will refresh the record every 5 minutes"
} else {
    Write-Warn "No DuckDNS parameters given; skipping dynamic DNS."
}

Write-Host "`n== Obsidion seed is up ==" -ForegroundColor Cyan
Write-Host "Verify it from OUTSIDE this network - a phone hotspot, not this LAN:"
Write-Host "    python deploy\preflight.py <hostname>:$Port" -ForegroundColor White
Write-Host "A machine on your own network can always reach itself, so testing"
Write-Host "from here proves nothing about the router's port forward."
