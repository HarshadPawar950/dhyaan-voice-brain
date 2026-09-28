# ==========================================================================
# DHYAAN VOICE BRAIN - ONE-CLICK STARTUP (engine)
# Launched by start_voicebrain.bat (double-click that, not this).
# Brings up the WHOLE system in the right order and proves each part healthy:
#   1. Catcher   (port 5005)  - Bolna webhook receiver  -> wait for /health
#   2. Dashboard (port 5006)  - control panel           -> wait for /
#   3. Tunnel    (cloudflared)- public https for Bolna   -> capture the URL
# Then prints the EXACT webhook URL to paste into Bolna, the dashboard link,
# the DB status and the live Bolna balance. PIDs are saved so stop_voicebrain
# can shut everything down cleanly.
#
# voicebrain.* ONLY. Never touches the Command Center / realestate_db.private.
# ==========================================================================
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Line() { Say ("-" * 64) "DarkGray" }

Say ""
Say "==================================================================" "Cyan"
Say "   DHYAAN VOICE BRAIN  -  ONE-CLICK STARTUP" "Cyan"
Say "==================================================================" "Cyan"

# --- read .env into a hashtable (no external deps) -------------------------
$envFile = Join-Path $root ".env"
$cfg = @{}
if (Test-Path $envFile) {
    foreach ($raw in Get-Content $envFile) {
        $l = $raw.Trim()
        if (-not $l -or $l.StartsWith("#") -or ($l -notmatch "=")) { continue }
        $i = $l.IndexOf("=")
        $k = $l.Substring(0, $i).Trim()
        $v = $l.Substring($i + 1).Trim()
        $cfg[$k] = $v
    }
} else {
    Say "  WARNING: .env not found at $envFile - using defaults." "Yellow"
}
function Cfg($key, $default) { if ($cfg.ContainsKey($key) -and $cfg[$key]) { return $cfg[$key] } else { return $default } }

$catcherPort   = Cfg "VB_CATCHER_PORT"   "5005"
$dashboardPort = Cfg "VB_DASHBOARD_PORT" "5006"
$tunnelMode    = (Cfg "VB_TUNNEL_MODE"   "quick").ToLower()
$tunnelName    = Cfg "VB_TUNNEL_NAME"    ""
$publicUrl     = (Cfg "VB_PUBLIC_URL"    "").TrimEnd("/")
$tunnelProto   = (Cfg "VB_TUNNEL_PROTOCOL" "http2").ToLower()
$ngrokDomain   = (Cfg "VB_NGROK_DOMAIN"   "").TrimEnd("/")
$webhookPath   = "/webhook/bolna"

$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$pidFile = Join-Path $logs "vb_pids.txt"

# --- free our own ports if a previous run is still up (clean restart) -------
function Free-Port($port) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            Say "  freeing port $port (stale PID $($c.OwningProcess))" "DarkYellow"
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}
# Kill a stale cloudflared from a previous run so its dead quick-tunnel URL and
# the log-file locks don't linger (we match only OUR tunnel by command line so
# we never touch an unrelated cloudflared the Captain may run).
function Free-StaleTunnel() {
    try {
        $cfs = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue
        foreach ($proc in $cfs) {
            $cl = $proc.CommandLine
            if ($cl -and ($cl -match "localhost:$catcherPort" -or $cl -match "tunnel run")) {
                Say "  stopping stale cloudflared (PID $($proc.ProcessId))" "DarkYellow"
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}
}
Say ""
Say "  Step 0  -  clearing stale listeners on $catcherPort / $dashboardPort + tunnel ..." "White"
Free-Port $catcherPort
Free-Port $dashboardPort
Free-StaleTunnel
Start-Sleep -Milliseconds 800

# --- helper: start a hidden process, capture its PID + redirect logs --------
function Start-Service2($name, $exe, $argList, $outLog, $errLog) {
    $p = Start-Process -FilePath $exe -ArgumentList $argList `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    Say "  started $name (PID $($p.Id))" "DarkGray"
    return $p
}

# --- helper: poll an http endpoint until it answers (or time out) -----------
function Wait-Http($url, $label, $timeoutSec = 40) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) {
                Say "  OK   $label is healthy  ($url)" "Green"
                return $true
            }
        } catch {}
        Start-Sleep -Milliseconds 800
    }
    Say "  FAIL $label did not come up within ${timeoutSec}s ($url)" "Red"
    return $false
}

$pids = @{}

# --- Step 1 - CATCHER -------------------------------------------------------
Line
Say "  Step 1  -  Catcher (Bolna webhook receiver) on :$catcherPort" "White"
$cat = Start-Service2 "catcher" "python" `
    "-m uvicorn catcher.server:app --host 0.0.0.0 --port $catcherPort" `
    (Join-Path $logs "catcher_run.out.log") (Join-Path $logs "catcher_run.err.log")
$pids["catcher"] = $cat.Id
$catcherOk = Wait-Http "http://127.0.0.1:$catcherPort/health" "Catcher" 40

# --- Step 2 - DASHBOARD -----------------------------------------------------
Line
Say "  Step 2  -  Dashboard (control panel) on :$dashboardPort" "White"
$dash = Start-Service2 "dashboard" "python" `
    "-m uvicorn dashboard.app:app --host 0.0.0.0 --port $dashboardPort" `
    (Join-Path $logs "dashboard_run.out.log") (Join-Path $logs "dashboard_run.err.log")
$pids["dashboard"] = $dash.Id
$dashboardOk = Wait-Http "http://127.0.0.1:$dashboardPort/" "Dashboard" 45

# --- Step 3 - TUNNEL --------------------------------------------------------
# QUIC (UDP 7844) is blocked on many office/ISP networks, which leaves a tunnel
# that has a URL but never connects (Cloudflare error 1033 / HTTP 530). So we
# force the protocol from .env (default http2 / TCP) AND we do not declare the
# tunnel UP until cloudflared logs "Registered tunnel connection" - i.e. the
# data plane is actually live, not just the URL minted.
Line
Say "  Step 3  -  Tunnel (public https for Bolna) - mode: $tunnelMode, protocol: $tunnelProto" "White"
$webhookUrl = $null
$tunnelOk = $false
$errLog = Join-Path $logs "tunnel.err.log"
$outLog = Join-Path $logs "tunnel.out.log"
$cf = Get-Command cloudflared -ErrorAction SilentlyContinue

# wait until cloudflared logs an actual edge registration (data plane up)
function Wait-TunnelRegistered($timeoutSec = 45) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 900
        $t = ""
        if (Test-Path $errLog) { $t += (Get-Content $errLog -Raw -ErrorAction SilentlyContinue) }
        if (Test-Path $outLog) { $t += (Get-Content $outLog -Raw -ErrorAction SilentlyContinue) }
        if ($t -match "Registered tunnel connection") { return $true }
    }
    return $false
}

if ($tunnelMode -eq "ngrok") {
    # ngrok static-domain path: a PERMANENT *.ngrok-free.app URL reserved on the
    # ngrok account. No cloudflared, no DNS zone, no per-restart URL churn. The
    # authtoken lives in ngrok's own config (set once via `ngrok config
    # add-authtoken`), so all we need from .env is the reserved domain.
    # Resolve ngrok by full path, not just PATH: when this script is launched by
    # double-clicking the .bat (a plain Windows shell), the user's ~\bin is NOT
    # on PATH, so `Get-Command ngrok` returns nothing even though it's installed.
    # Fall back to the known install location so it works however it's launched.
    $ngExe = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
    if (-not $ngExe) {
        $ngFallback = Join-Path $env:USERPROFILE "bin\ngrok.exe"
        if (Test-Path $ngFallback) { $ngExe = $ngFallback }
    }
    if (-not $ngExe) {
        Say "  FAIL ngrok not found (PATH or $env:USERPROFILE\bin\ngrok.exe) - install it." "Red"
    } elseif (-not $ngrokDomain) {
        Say "  FAIL mode=ngrok but VB_NGROK_DOMAIN not set in .env." "Red"
    } else {
        "" | Set-Content $outLog; "" | Set-Content $errLog
        # --log=stdout disables ngrok's interactive TUI (which needs a real
        # console) and streams structured logs to our redirected stdout instead.
        $tun = Start-Service2 "tunnel" $ngExe `
            "http $catcherPort --url https://$ngrokDomain --log=stdout --log-format=logfmt" $outLog $errLog
        $pids["tunnel"] = $tun.Id
        $webhookUrl = "https://$ngrokDomain$webhookPath"
        Say "  waiting for ngrok to bring up the static endpoint ..." "DarkGray"
        # Authoritative proof: hit the PUBLIC url and require it to reach the
        # catcher end-to-end (edge -> tunnel -> localhost:$catcherPort).
        if (Wait-Http "https://$ngrokDomain/health" "ngrok endpoint" 40) {
            $tunnelOk = $true
            Say "  OK   ngrok static tunnel up -> https://$ngrokDomain (permanent)" "Green"
        } else {
            Say "  WARN ngrok endpoint did not answer in 40s - check authtoken/domain + logs/tunnel.err.log" "Yellow"
        }
    }
} elseif (-not $cf) {
    Say "  FAIL cloudflared not found on PATH - install it or fix PATH." "Red"
} elseif ($tunnelMode -eq "named") {
    if (-not $tunnelName -or -not $publicUrl) {
        Say "  FAIL mode=named but VB_TUNNEL_NAME / VB_PUBLIC_URL not set in .env." "Red"
        Say "       Set them after the one-time named-tunnel setup, or use quick." "Yellow"
    } else {
        "" | Set-Content $outLog; "" | Set-Content $errLog
        $tun = Start-Service2 "tunnel" $cf.Source `
            "tunnel --protocol $tunnelProto run $tunnelName" $outLog $errLog
        $pids["tunnel"] = $tun.Id
        $webhookUrl = "$publicUrl$webhookPath"
        Say "  waiting for cloudflared to register the edge connection ..." "DarkGray"
        if (Wait-TunnelRegistered 45) {
            $tunnelOk = $true
            Say "  OK   named tunnel '$tunnelName' -> $publicUrl (stable, registered)" "Green"
        } else {
            Say "  WARN named tunnel '$tunnelName' did NOT register in 45s - see logs/tunnel.err.log" "Yellow"
            Say "       (URL set, but Bolna may 530 until it connects. Check protocol / firewall.)" "Yellow"
        }
    }
} else {
    # quick mode: random *.trycloudflare.com URL, parsed from cloudflared's log
    "" | Set-Content $outLog; "" | Set-Content $errLog
    $tun = Start-Service2 "tunnel" $cf.Source `
        "tunnel --protocol $tunnelProto --url http://localhost:$catcherPort" $outLog $errLog
    $pids["tunnel"] = $tun.Id
    Say "  waiting for cloudflared to mint the URL + register the edge ..." "DarkGray"
    $deadline = (Get-Date).AddSeconds(50)
    $base = $null
    $registered = $false
    while ((Get-Date) -lt $deadline -and -not ($base -and $registered)) {
        Start-Sleep -Milliseconds 900
        $text = ""
        if (Test-Path $errLog) { $text += (Get-Content $errLog -Raw -ErrorAction SilentlyContinue) }
        if (Test-Path $outLog) { $text += (Get-Content $outLog -Raw -ErrorAction SilentlyContinue) }
        if (-not $base) {
            $m = [regex]::Match($text, "https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            if ($m.Success) { $base = $m.Value; Say "  url minted -> $base" "DarkGray" }
        }
        if (-not $registered -and ($text -match "Registered tunnel connection")) { $registered = $true }
    }
    if ($base) {
        $webhookUrl = "$base$webhookPath"
        if ($registered) {
            $tunnelOk = $true
            Say "  OK   quick tunnel up + registered -> $base" "Green"
        } else {
            Say "  WARN URL minted but edge NOT registered in 50s -> Bolna would 530." "Yellow"
            Say "       Network likely blocks port 7844. Try VB_TUNNEL_PROTOCOL=http2 (or check firewall)." "Yellow"
        }
    } else {
        Say "  FAIL tunnel URL not detected in 50s - see logs/tunnel.err.log" "Red"
    }
}

# --- save PIDs for the stop script -----------------------------------------
$pidLines = @()
foreach ($k in $pids.Keys) { $pidLines += "$k=$($pids[$k])" }
$pidLines | Set-Content $pidFile
if ($webhookUrl) { $webhookUrl | Set-Content (Join-Path $logs "current_webhook_url.txt") }

# --- DB + Bolna balance probes (one dedicated probe script - no inline python) -
Line
Say "  Probes  -  database + Bolna balance" "White"
$dbStatus = "unknown"
$walletLine = "unknown"
try {
    $probeOut = & python (Join-Path $root "tools\vb_probe.py") 2>&1
    foreach ($pl in $probeOut) {
        $s = "$pl"
        if ($s -like "DB=online*")      { $dbStatus = "online" }
        elseif ($s -like "DB=OFFLINE*") { $dbStatus = $s }
        elseif ($s -like "WALLET=*")    { $walletLine = $s }
    }
} catch {
    $dbStatus = "OFFLINE: $_"
}
if ($dbStatus -eq "online") { Say "  OK   Postgres (voicebrain schema) reachable" "Green" }
else { Say "  WARN database not reachable -> $dbStatus" "Yellow" }
Say "  Bolna  $walletLine" "DarkGray"

# --- SUMMARY ----------------------------------------------------------------
Say ""
Say "==================================================================" "Cyan"
Say "   VOICE BRAIN STATUS" "Cyan"
Say "==================================================================" "Cyan"
function Badge($ok) { if ($ok) { return "[ UP ]" } else { return "[DOWN]" } }
function StatColor($ok) { if ($ok) { return "Green" } else { return "Red" } }
$dbOnline = ($dbStatus -eq "online")
Say ("   Catcher    {0}  http://localhost:{1}/health" -f (Badge $catcherOk), $catcherPort) (StatColor $catcherOk)
Say ("   Dashboard  {0}  http://localhost:{1}/" -f (Badge $dashboardOk), $dashboardPort) (StatColor $dashboardOk)
Say ("   Tunnel     {0}  mode={1}" -f (Badge $tunnelOk), $tunnelMode) (StatColor $tunnelOk)
$dbColor = "Yellow"; if ($dbOnline) { $dbColor = "Green" }
Say ("   Database   {0}" -f (Badge $dbOnline)) $dbColor
Say ""
Say "------------------------------------------------------------------" "DarkGray"
if ($webhookUrl) {
    Say "   >>> PASTE THIS INTO BOLNA'S WEBHOOK URL FIELD:" "Yellow"
    Say ""
    Say "       $webhookUrl" "White"
    Say ""
    if ($tunnelMode -eq "quick") {
        Say "   (quick mode: this URL CHANGES every restart. For a permanent" "DarkYellow"
        Say "    URL use VB_TUNNEL_MODE=ngrok or a cloudflared named tunnel.)" "DarkYellow"
    } else {
        Say "   ($tunnelMode mode: this URL is PERMANENT - set it in Bolna once.)" "DarkGray"
    }
} else {
    Say "   WEBHOOK URL UNAVAILABLE - tunnel did not come up. See logs/tunnel.err.log" "Red"
}
Say "------------------------------------------------------------------" "DarkGray"
Say "   Open the dashboard:  http://localhost:$dashboardPort/" "Cyan"
Say "   Stop everything:     double-click stop_voicebrain.bat" "Cyan"
Say "==================================================================" "Cyan"
Say ""
