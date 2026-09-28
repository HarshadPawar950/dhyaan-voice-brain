# ==========================================================================
# DHYAAN VOICE BRAIN - DESKTOP ICON INSTALLER
# Run ONCE. Generates a branded "D" icon (navy #0A1628 + gold #D4AF37) and
# drops a "Dhyaan VB" shortcut on your Desktop that:
#   double-click  ->  start_voicebrain_app.ps1  ->  services up + app window.
# The shortcut runs the launcher windowless (hidden) for a clean app feel;
# the startup status window (webhook URL etc.) still appears on a cold start.
#
# Re-run any time to refresh the icon/shortcut. voicebrain.* only.
# ==========================================================================
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- 1) generate a branded .ico -------------------------------------------
Add-Type -AssemblyName System.Drawing
$assets = Join-Path $root "assets"
New-Item -ItemType Directory -Force -Path $assets | Out-Null
$icoPath = Join-Path $assets "dhyaan_vb.ico"

$size = 256
$bmp  = New-Object System.Drawing.Bitmap $size, $size
$g    = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias

# rounded navy tile
$navy = [System.Drawing.Color]::FromArgb(10, 22, 40)
$gold = [System.Drawing.Color]::FromArgb(212, 175, 55)
$g.Clear([System.Drawing.Color]::Transparent)
$rect = New-Object System.Drawing.Rectangle 8, 8, ($size - 16), ($size - 16)
$path = New-Object System.Drawing.Drawing2D.GraphicsPath
$r = 48
$path.AddArc($rect.X, $rect.Y, $r, $r, 180, 90)
$path.AddArc($rect.Right - $r, $rect.Y, $r, $r, 270, 90)
$path.AddArc($rect.Right - $r, $rect.Bottom - $r, $r, $r, 0, 90)
$path.AddArc($rect.X, $rect.Bottom - $r, $r, $r, 90, 90)
$path.CloseFigure()
$g.FillPath((New-Object System.Drawing.SolidBrush $navy), $path)
$g.DrawPath((New-Object System.Drawing.Pen $gold, 6), $path)

# gold serif "D" (Playfair-style)
$font  = New-Object System.Drawing.Font "Georgia", 150, ([System.Drawing.FontStyle]::Bold)
$brush = New-Object System.Drawing.SolidBrush $gold
$sf = New-Object System.Drawing.StringFormat
$sf.Alignment = [System.Drawing.StringAlignment]::Center
$sf.LineAlignment = [System.Drawing.StringAlignment]::Center
$g.DrawString("D", $font, $brush, (New-Object System.Drawing.RectangleF 0, 0, $size, $size), $sf)
$g.Dispose()

$hicon = $bmp.GetHicon()
$icon  = [System.Drawing.Icon]::FromHandle($hicon)
$fs = [System.IO.File]::Create($icoPath)
$icon.Save($fs)
$fs.Close()
$icon.Dispose(); $bmp.Dispose()
Write-Host "  icon written -> $icoPath" -ForegroundColor Green

# --- 2) create the Desktop shortcut ---------------------------------------
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Dhyaan VB.lnk"
$psExe   = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath       = $psExe
$lnk.Arguments        = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\start_voicebrain_app.ps1`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation     = "$icoPath,0"
$lnk.Description       = "Dhyaan Voice Brain - start services + open the app window"
$lnk.WindowStyle      = 7   # 7 = minimized (launcher runs hidden)
$lnk.Save()

Write-Host "  desktop shortcut created -> $lnkPath" -ForegroundColor Green
Write-Host ""
Write-Host "  DONE. Double-click 'Dhyaan VB' on your Desktop to launch the app." -ForegroundColor Cyan
