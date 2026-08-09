<#
Puts an "Obsidion Wallet HUD" icon on the desktop, so the wallet can be opened
without ever touching a terminal.

Run once, from the repository folder:

    powershell -ExecutionPolicy Bypass -File deploy\install-hud-shortcut.ps1

Double-clicking the icon afterwards starts the node, asks for the wallet
password, and opens the HUD in your browser. Safe to re-run; it overwrites its
own shortcut and nothing else.

No administrator rights are needed: this writes one .lnk file to your own
desktop and changes nothing else on the machine.

KEEP THIS FILE PURE ASCII. Windows PowerShell 5.1 parses a BOM-less .ps1 as
ANSI, so a UTF-8 em dash arrives as three CP1252 characters ending in 0x94 - a
right smart quote, which PowerShell honours as a real string delimiter. One
dash in a comment silently inverts quote parity for the rest of the file and
the script fails to parse on someone else's machine, far from whoever edited
it.
#>

[CmdletBinding()]
param(
    # mainnet, testnet, or regtest. regtest is a private practice chain whose
    # coins are worthless on purpose - a good place to learn the wallet.
    [ValidateSet("mainnet", "testnet", "regtest")]
    [string]$Network = "mainnet",

    [string]$ShortcutName = "Obsidion Wallet HUD"
)

$ErrorActionPreference = "Stop"

function Write-Ok($message) { Write-Host "   ok   $message" -ForegroundColor Green }

Write-Host "== Obsidion desktop shortcut ==" -ForegroundColor Cyan

# deploy\ -> repository root
$repo = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repo "deploy\desktop_launcher.py"
if (-not (Test-Path $launcher)) {
    throw "Cannot find $launcher. Run this from inside the Obsidion repository."
}

# Prefer the project's virtual environment: it is the interpreter that actually
# has flask and ecdsa installed. Fall back to whatever python is on PATH, and
# say so, because a bare python without the dependencies fails at a confusing
# moment - after the icon is double-clicked, not now.
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if (-not $onPath) {
        throw "No .venv found and no python on PATH. Create the virtual environment first: python -m venv .venv"
    }
    $python = $onPath.Source
    Write-Host "  warn  no .venv found; using $python" -ForegroundColor Yellow
    Write-Host "  warn  make sure 'ecdsa' and 'flask' are installed for it" -ForegroundColor Yellow
}

# python.exe, not pythonw.exe, on purpose: the console window is where the
# wallet password is typed and where the node reports what it is doing.
$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "$ShortcutName.lnk"

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $python
$link.Arguments = '"' + $launcher + '" ' + $Network
$link.WorkingDirectory = $repo
$link.Description = "Start the Obsidion node and open the wallet HUD"

# The Obsidion gem if it is there, Python's own logo if it is not. A missing
# icon file must never cost somebody their shortcut, so this degrades rather
# than throwing - the launcher works identically either way.
$icon = Join-Path $repo "assets\obsidion.ico"
if (Test-Path $icon) {
    $link.IconLocation = "$icon,0"
} else {
    $link.IconLocation = "$python,0"
    Write-Host "  warn  assets\obsidion.ico not found; using the Python icon" -ForegroundColor Yellow
    Write-Host "  warn  regenerate it with: python deploy\make_icon.py" -ForegroundColor Yellow
}
$link.Save()

Write-Ok "created $linkPath"
Write-Host ""
Write-Host "Double-click '$ShortcutName' on your desktop to start."
Write-Host "A black window will open and ask for your wallet password."
Write-Host "Leave that window open - it is the node. Closing it stops everything."
