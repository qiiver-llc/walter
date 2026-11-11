# Walter desktop face for Windows (UNTESTED on real Windows: written on Linux; see docs/DESKTOP.md).
# Run in PowerShell from the unpacked folder:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Puts the app in %LOCALAPPDATA%\walter-desktop\app with its own venv, a walter-desktop.cmd
# next to it, and a Startup-folder script so it starts at login.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = if (Test-Path "$here\desktop") { $here } else { (Resolve-Path "$here\..\..\..").Path }
$app = Join-Path $env:LOCALAPPDATA "walter-desktop\app"
$py = $null
foreach ($c in @("py -3", "python")) {
  try {
    $v = & cmd /c "$c -c ""import sys; print(sys.version_info >= (3, 10))""" 2>$null
    if ($v -eq "True") { $py = $c; break }
  } catch {}
}
if (-not $py) {
  Write-Host "Python 3.10+ is needed: install it from https://www.python.org/downloads/windows/ (tick 'Add to PATH'), then run this again."
  exit 1
}
New-Item -ItemType Directory -Force -Path $app | Out-Null
foreach ($d in @("desktop", "walter", "bin", "docs")) {
  if (Test-Path "$app\$d") { Remove-Item -Recurse -Force "$app\$d" }
  Copy-Item -Recurse "$root\$d" "$app\$d"
}
$req = if (Test-Path "$root\requirements-desktop.txt") { "$root\requirements-desktop.txt" } else { "$root\scripts\desktop\requirements-desktop.txt" }
Copy-Item $req "$app\requirements-desktop.txt"
if (-not (Test-Path "$app\.venv\Scripts\python.exe")) { & cmd /c "$py -m venv ""$app\.venv""" }
& "$app\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$app\.venv\Scripts\python.exe" -m pip install -q -r "$app\requirements-desktop.txt"
if ($LASTEXITCODE -ne 0) { Write-Host "some extras didn't install; the face still works" }
$cmd = Join-Path $app "walter-desktop.cmd"
Set-Content -Encoding ASCII -Path $cmd -Value "@echo off`r`nset PYTHONPATH=$app`r`n""$app\.venv\Scripts\python.exe"" -m desktop %*"
& $cmd autostart enable
Write-Host ""
Write-Host "Installed: $cmd"
Write-Host "Windows will ask for microphone access the first time (Settings > Privacy > Microphone)."
Write-Host "Next: on the wall say 'add a face called Office', then:"
Write-Host "  & '$cmd' pair http://<wall-ip>:8765 <code> --name Office"
Write-Host "  & '$cmd' run"
