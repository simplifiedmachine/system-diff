# The pane's launcher on Windows, where there may be no sh: what scripts/run.sh does, in PowerShell. It runs
# `system-diff <args>` from the project's own virtualenv, the plugin's own copy, or PATH; where there is none it installs
# the plugin's own copy in the background with uv, fetching uv (which brings its own Python) into the plugin's data
# folder when the machine has none, and says it is installing (exit 3) meanwhile.
$ErrorActionPreference = 'SilentlyContinue'
$rest = $args
$root = if ($env:CLAUDE_PLUGIN_ROOT) { $env:CLAUDE_PLUGIN_ROOT } else { Split-Path -Parent $PSScriptRoot }
$data = if ($env:CLAUDE_PLUGIN_DATA) { $env:CLAUDE_PLUGIN_DATA } else { Join-Path $HOME '.claude\plugins\data\system-diff' }
$dir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$version = (Get-Content (Join-Path $root '.claude-plugin\plugin.json') -Raw | ConvertFrom-Json).version
$venv = Join-Path $data 'venv'
$own = Join-Path $venv 'Scripts\python.exe'

function Has($py) {
  if (-not (Test-Path $py)) { return $false }
  & $py -c 'import system_diff' 2>$null | Out-Null
  return $LASTEXITCODE -eq 0
}
function Current {
  $v = Join-Path $data 'version'
  return (Test-Path $v) -and ((Get-Content $v -Raw).Trim() -eq $version) -and (Has $own)
}
function Run($exe, $pre) { & $exe @pre @rest; exit $LASTEXITCODE }

$lock = Join-Path $data '.installing'
$stage = Join-Path $data 'stage'
$log = Join-Path $data 'install.log'
function Step($what) { Set-Content -NoNewline $stage $what }
function LastLine { $l = Get-Content $log -Tail 20 | Where-Object { $_.Trim() } | Select-Object -Last 1; if ($l) { "$l".Substring(0, [Math]::Min(160, "$l".Length)) } }

function Stale {   # a lock left by an install that died midway (its process is gone), or ten minutes ago
  if (-not (Test-Path $lock)) { return }
  $old = Get-Content (Join-Path $lock 'pid')
  if (($old -and -not (Get-Process -Id $old)) -or ((Get-Item $lock).LastWriteTime -lt (Get-Date).AddMinutes(-10))) { Remove-Item $lock -Recurse -Force }
}

function Install {   # one at a time; a lock whose process is gone, or ten minutes old, is from one that died midway
  New-Item -ItemType Directory -Force $data | Out-Null
  Stale
  try { New-Item -ItemType Directory $lock -ErrorAction Stop | Out-Null } catch { return }
  $ok = $false
  try {
    Set-Content (Join-Path $lock 'pid') $PID
    Set-Content (Join-Path $lock 'started') ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    Set-Content $log ''
    Step '1/3 Looking for uv'
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { $uv = Join-Path $data 'uv\uv.exe' }
    if (-not (Test-Path $uv)) {   # no uv: fetch it (it brings its own Python) without touching PATH
      Step '1/3 Fetching uv, which brings Python'
      $env:UV_INSTALL_DIR = Join-Path $data 'uv'; $env:UV_NO_MODIFY_PATH = '1'
      Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression *>> $log
    }
    Step '2/3 Setting up Python with uv'
    & $uv venv --python '>=3.11' $venv *>> $log
    Step "3/3 Installing system-diff $version"
    & $uv pip install --python $own "system-diff[mcp]==$version" *>> $log
    if ($LASTEXITCODE -ne 0) { & $uv pip install --python $own 'system-diff[mcp]' *>> $log }
    $ok = Has $own
    if ($ok) { Set-Content -NoNewline (Join-Path $data 'version') $version; Remove-Item $stage -Force }
  } catch { Add-Content $log "$_" }
  finally { if (-not $ok) { Step 'failed' }; Remove-Item $lock -Recurse -Force }
}

if ($rest.Count -gt 0 -and $rest[0] -eq '__install') { Install; exit 0 }
if (Has (Join-Path $dir '.venv\Scripts\python.exe')) { Run (Join-Path $dir '.venv\Scripts\python.exe') @('-m', 'system_diff') }
if (Current) { Run $own @('-m', 'system_diff') }
Stale
$background = { Start-Process powershell -WindowStyle Hidden -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '__install') }
$onPath = Get-Command system-diff -ErrorAction SilentlyContinue
if ($onPath) { if (-not (Test-Path $lock)) { & $background }; Run $onPath.Source @() }
if (Has $own) { if (-not (Test-Path $lock)) { & $background }; Run $own @('-m', 'system_diff') }
# the pane asks again every few seconds: it shows how the install is going, or why it failed (tried again a minute on)
if (-not (Test-Path $lock) -and ((Get-Content $stage) -eq 'failed') -and ((Get-Item $stage).LastWriteTime -gt (Get-Date).AddMinutes(-1))) {
  [Console]::Error.WriteLine("system-diff: install failed: $(LastLine) - log: $log")
  exit 4
}
if (-not (Test-Path $lock)) { & $background; [Console]::Error.WriteLine('system-diff: installing: starting'); exit 3 }
$started = [long](Get-Content (Join-Path $lock 'started'))
$took = if ($started) { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $started } else { 0 }
[Console]::Error.WriteLine("system-diff: installing: $(Get-Content $stage) - ${took}s - $(LastLine)")
exit 3
