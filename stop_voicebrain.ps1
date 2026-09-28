# ==========================================================================
# DHYAAN VOICE BRAIN - STOP (engine)
# Launched by stop_voicebrain.bat. Shuts down the Catcher, Dashboard and the
# cloudflared tunnel that start_voicebrain brought up.
# Primary: kill the exact PIDs saved in logs/vb_pids.txt.
# Fallback: free ports 5005/5006 and kill any cloudflared tunnel we launched.
# voicebrain.* only - it never touches the portal (port 3000) or any other app.
# ==========================================================================
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

Say ""
Say "==================================================================" "Cyan"
Say "   DHYAAN VOICE BRAIN  -  STOP" "Cyan"
Say "==================================================================" "Cyan"

$logs = Join-Path $root "logs"
$pidFile = Join-Path $logs "vb_pids.txt"

# read ports from .env (so fallback matches the start script)
$cfg = @{}
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    foreach ($raw in Get-Content $envFile) {
        $l = $raw.Trim()
        if (-not $l -or $l.StartsWith("#") -or ($l -notmatch "=")) { continue }
        $i = $l.IndexOf("="); $cfg[$l.Substring(0,$i).Trim()] = $l.Substring($i+1).Trim()
    }
}
function Cfg($k, $d) { if ($cfg.ContainsKey($k) -and $cfg[$k]) { return $cfg[$k] } else { return $d } }
$catcherPort   = Cfg "VB_CATCHER_PORT"   "5005"
$dashboardPort = Cfg "VB_DASHBOARD_PORT" "5006"

$killed = 0

# --- primary: kill saved PIDs ----------------------------------------------
if (Test-Path $pidFile) {
    foreach ($line in Get-Content $pidFile) {
        if ($line -notmatch "=") { continue }
        $name, $procId = $line.Split("=", 2)
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p) {
            Say "  stopping $name (PID $procId)" "White"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            $killed++
        } else {
            Say "  $name (PID $procId) already gone" "DarkGray"
        }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
} else {
    Say "  no logs/vb_pids.txt - falling back to port + process match" "DarkYellow"
}

# --- fallback: free our two ports ------------------------------------------
function Free-Port($port, $label) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            Say "  freeing $label port $port (PID $($c.OwningProcess))" "White"
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
            $script:killed++
        }
    } catch {}
}
Free-Port $catcherPort "Catcher"
Free-Port $dashboardPort "Dashboard"

# --- fallback: kill cloudflared tunnels we started (match our command line) -
try {
    $cfs = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue
    foreach ($proc in $cfs) {
        $cl = $proc.CommandLine
        if ($cl -and ($cl -match "localhost:$catcherPort" -or $cl -match "tunnel run")) {
            Say "  stopping cloudflared tunnel (PID $($proc.ProcessId))" "White"
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            $killed++
        }
    }
} catch {}

Say ""
if ($killed -gt 0) { Say "  Stopped $killed process(es). Voice Brain is down." "Green" }
else { Say "  Nothing was running." "DarkGray" }
Say "==================================================================" "Cyan"
Say ""
