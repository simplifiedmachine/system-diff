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

function Install {   # one at a time; a lock older than ten minutes is from one that died midway
  New-Item -ItemType Directory -Force $data | Out-Null
  $lock = Join-Path $data '.installing'
  if ((Test-Path $lock) -and ((Get-Item $lock).LastWriteTime -lt (Get-Date).AddMinutes(-10))) { Remove-Item $lock -Recurse -Force }
  try { New-Item -ItemType Directory $lock -ErrorAction Stop | Out-Null } catch { return }
  try {
    $log = Join-Path $data 'install.log'
    Set-Content $log ''
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { $uv = Join-Path $data 'uv\uv.exe' }
    if (-not (Test-Path $uv)) {   # no uv: fetch it (it brings its own Python) without touching PATH
      $env:UV_INSTALL_DIR = Join-Path $data 'uv'; $env:UV_NO_MODIFY_PATH = '1'
      Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression *>> $log
    }
    & $uv venv --quiet --python '>=3.11' $venv *>> $log
    & $uv pip install --quiet --python $own "system-diff[mcp]==$version" *>> $log
    if ($LASTEXITCODE -ne 0) { & $uv pip install --quiet --python $own 'system-diff[mcp]' *>> $log }
    if (Has $own) { Set-Content -NoNewline (Join-Path $data 'version') $version }
  } finally { Remove-Item $lock -Recurse -Force }
}

if ($rest.Count -gt 0 -and $rest[0] -eq '__install') { Install; exit 0 }
if (Has (Join-Path $dir '.venv\Scripts\python.exe')) { Run (Join-Path $dir '.venv\Scripts\python.exe') @('-m', 'system_diff') }
if (Current) { Run $own @('-m', 'system_diff') }
$background = { Start-Process powershell -WindowStyle Hidden -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '__install') }
$onPath = Get-Command system-diff -ErrorAction SilentlyContinue
if ($onPath) { & $background; Run $onPath.Source @() }
if (Has $own) { & $background; Run $own @('-m', 'system_diff') }
& $background
[Console]::Error.WriteLine('system-diff: installing (first use, about a minute)')
exit 3
