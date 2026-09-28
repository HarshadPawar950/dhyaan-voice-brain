# ==========================================================================
# DHYAAN VOICE BRAIN - DESKTOP APP LAUNCHER (engine)
# One double-click (via the "Dhyaan VB" desktop icon) does BOTH:
#   1. Ensures the services are up (Catcher + Dashboard + Tunnel) by calling
#      the existing start_voicebrain.ps1 - but ONLY if the dashboard isn't
#      already answering, so re-opening the app window is instant.
#   2. Opens the dashboard in a STANDALONE app window (Edge/Chrome --app mode):
#      no tabs, no address bar - looks like a native desktop app.
#
# Uses a dedicated app profile (LOCALAPPDATA\DhyaanVB\app-profile) so the
# window is clean and isolated from your normal browsing (and always loads a
# fresh copy of the page - no stale cache).
#
# voicebrain.* ONLY. Never touches the Command Center / realestate_db.private.
# ==========================================================================
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }

# --- resolve the dashboard port from .env (default 5006) -------------------
$dashPort = "5006"
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    foreach ($raw in Get-Content $envFile) {
        if ($raw.Trim() -match '^\s*VB_DASHBOARD_PORT\s*=\s*(.+?)\s*$') { $dashPort = $matches[1].Trim() }
    }
}
$dashUrl = "http://localhost:$dashPort/"

function Test-Dash {
    try {
        $r = Invoke-WebRequest -Uri $dashUrl -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
    } catch { return $false }
}

Say ""
Say "  DHYAAN VOICE BRAIN - opening app ..." "Cyan"

# --- Step 1: ensure services are up ----------------------------------------
if (Test-Dash) {
    Say "  services already running - opening the window." "Green"
} else {
    Say "  services down - starting them (a status window will open) ..." "White"
    # Launch the existing full startup engine in its OWN window (-NoExit) so you
    # still see the webhook URL / DB status / Bolna balance it prints.
    Start-Process -FilePath "powershell" `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$root\start_voicebrain.ps1`"" `
        -WorkingDirectory $root | Out-Null
    Say "  waiting for the dashboard to come up ..." "DarkGray"
    $deadline = (Get-Date).AddSeconds(75)
    while ((Get-Date) -lt $deadline -and -not (Test-Dash)) { Start-Sleep -Milliseconds 900 }
}

if (-not (Test-Dash)) {
    Say "  WARN dashboard still not answering - check the status window / logs." "Yellow"
    Say "       Opening the window anyway; it will connect once services are up." "Yellow"
}

# --- Step 2: open the dashboard as a standalone app window -----------------
$browser = $null
$candidates = @(
    (Join-Path $env:ProgramFiles          "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path ${env:ProgramFiles(x86)}   "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles          "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)}   "Google\Chrome\Application\chrome.exe")
)
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $browser = $c; break } }

if (-not $browser) {
    Say "  no Edge/Chrome found - opening in your default browser instead." "Yellow"
    Start-Process $dashUrl
} else {
    $profileDir = Join-Path $env:LOCALAPPDATA "DhyaanVB\app-profile"
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    $bArgs = @(
        "--app=$dashUrl",
        "--user-data-dir=`"$profileDir`"",
        "--window-size=1440,920",
        "--no-first-run",
        "--no-default-browser-check"
    )
    Start-Process -FilePath $browser -ArgumentList $bArgs | Out-Null
    Say "  app window opened -> $dashUrl  (using $(Split-Path $browser -Leaf))" "Green"
}
Say ""
