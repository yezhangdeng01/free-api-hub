# Restart the api-hub tray reliably on this machine (source mode, pythonw desktop.py).
#
# Why a script: after editing .py files the running tray keeps serving OLD code (single-instance
# lock makes "just reopen it" fail silently). This machine has no psutil and no wmic, so match
# the tray by executable path instead: the tray always runs <repo>\.venv\Scripts\pythonw.exe.
#
# NOTE: keep this file ASCII-only. PowerShell 5.1 reads .ps1 as ANSI when there is no BOM, so
# non-ASCII comments corrupt parsing ($PSScriptRoot becomes $null, Join-Path fails on empty arg).
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restart_tray.ps1
$repo = $PSScriptRoot
if (-not $repo) { $repo = (Get-Location).ProviderPath }
$repo = Split-Path $repo -Parent          # scripts/ -> repo root
$exe = Join-Path $repo '.venv\Scripts\pythonw.exe'
Write-Output ("repo=" + $repo)
Write-Output ("exe=" + $exe + " exists=" + (Test-Path $exe))

$mine = @(Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
Write-Output ("matched=" + $mine.Count + " pids=" + (($mine | ForEach-Object { $_.Id }) -join ','))
foreach ($p in $mine) { Stop-Process -Id $p.Id -Force; Write-Output ("  killed " + $p.Id) }
Start-Sleep -Seconds 3

$left = @(Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
Write-Output ("left=" + $left.Count)
$holder = (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue).OwningProcess
Write-Output ("port8787=" + ($holder -join ','))

Start-Process -FilePath $exe -ArgumentList 'desktop.py' -WorkingDirectory $repo
Write-Output "started-new-instance (wait ~15s, then: curl http://127.0.0.1:8787/api/overview)"
