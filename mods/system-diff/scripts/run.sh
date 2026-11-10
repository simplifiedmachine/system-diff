#!/bin/sh
# Runs `system-diff <args>` for the plugin's hooks, MCP server and pane. Where it looks, in order:
#   1. the project's own virtualenv, when it has system-diff (a project that develops or pins it);
#   2. the plugin's own copy, the version the plugin was released with, in its data folder;
#   3. system-diff on PATH (pipx), then any python3 that has it.
# Where none is found it installs the plugin's own copy (uv when there is one, else python3's venv and pip, and with
# neither it fetches uv, which brings its own Python, into the plugin's data folder): in the
# background for a hook, which must not hold up the session and lets this one go, and right away for the MCP server
# and the pane, which need it to start.
root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
data="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/system-diff}"
dir="${CLAUDE_PROJECT_DIR:-$PWD}"
version=$(sed -n 's/.*"version"[^"]*"\([^"]*\)".*/\1/p' "$root/.claude-plugin/plugin.json" | head -n 1)
venv="$data/venv"
own="$venv/bin/python"
[ -x "$own" ] || own="$venv/Scripts/python.exe"

has() { [ -x "$1" ] && "$1" -c "import system_diff" >/dev/null 2>&1; }
current() { [ -f "$data/version" ] && [ "$(cat "$data/version")" = "$version" ] && has "$own"; }

python311() {
  for p in python3 python3.13 python3.12 python3.11 python py; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}

get_uv() {   # uv, which brings its own Python, into the plugin's data folder (nothing on PATH changes)
  mkdir -p "$data/uv"
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      UV_INSTALL_DIR="$data/uv" UV_NO_MODIFY_PATH=1 \
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" ;;
    *)
      if command -v curl >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh
      else wget -qO- https://astral.sh/uv/install.sh; fi | UV_INSTALL_DIR="$data/uv" UV_NO_MODIFY_PATH=1 sh ;;
  esac >>"$data/install.log" 2>&1
}

lock="$data/.installing"
step() { printf '%s' "$1" >"$data/stage"; }   # where the install is, for the pane to show
unlock() { rm -f "$lock/pid" "$lock/started"; rmdir "$lock" 2>/dev/null; }

stale() {   # a lock left by an install that died midway (its process is gone), or ten minutes ago
  [ -d "$lock" ] || return 0
  pid=$(cat "$lock/pid" 2>/dev/null)
  if { [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; } ||
     [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then unlock; fi
}

install() {   # the plugin's own copy of the engine, at the plugin's version; one install at a time
  mkdir -p "$data" || return 1
  stale
  mkdir "$lock" 2>/dev/null || return 1
  trap unlock EXIT
  sh -c 'echo $PPID' >"$lock/pid"; date +%s >"$lock/started" 2>/dev/null
  spec="system-diff[mcp]${version:+==$version}"
  # the plugin's own version; when that is not on PyPI (yet), the latest
  : >"$data/install.log"
  step "1/3 Looking for uv or Python 3.11+"
  uv=$(command -v uv 2>/dev/null) || uv="$data/uv/uv"; [ -x "$uv" ] || [ ! -x "$data/uv/uv.exe" ] || uv="$data/uv/uv.exe"
  if [ ! -x "$uv" ] && ! python311 >/dev/null; then   # no uv and no Python: fetch uv, and Python with it
    step "1/3 Fetching uv, which brings Python"
    get_uv; uv="$data/uv/uv"; [ -x "$uv" ] || uv="$data/uv/uv.exe"
  fi
  if [ -x "$uv" ]; then
    step "2/3 Setting up Python with uv"
    "$uv" venv --python ">=3.11" "$venv" >>"$data/install.log" 2>&1 &&
      { step "3/3 Installing system-diff $version"
        "$uv" pip install --python "$venv" "$spec" >>"$data/install.log" 2>&1 ||
        "$uv" pip install --python "$venv" "system-diff[mcp]" >>"$data/install.log" 2>&1; }
  elif py=$(python311); then
    step "2/3 Setting up Python ($py)"
    vpy="$venv/bin/python"; case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) vpy="$venv/Scripts/python.exe";; esac
    "$py" -m venv "$venv" >>"$data/install.log" 2>&1 &&
      { step "3/3 Installing system-diff $version"
        "$vpy" -m pip install --progress-bar off --disable-pip-version-check "$spec" >>"$data/install.log" 2>&1 ||
        "$vpy" -m pip install --progress-bar off --disable-pip-version-check "system-diff[mcp]" >>"$data/install.log" 2>&1; }
  else
    echo "no uv and no Python 3.11+, and uv could not be fetched" >>"$data/install.log"; false
  fi
  ok=$?
  own="$venv/bin/python"; [ -x "$own" ] || own="$venv/Scripts/python.exe"
  if [ $ok -eq 0 ] && has "$own"; then printf '%s' "$version" >"$data/version"; rm -f "$data/stage"
  else ok=1; step "failed"; fi
  unlock; trap - EXIT
  [ $ok -eq 0 ]
}

# The install in the background, as a process of its own that holds none of the caller's pipes (a shell function sent
# to the background keeps the caller's output open, and the caller would wait for the install, then time it out)
background() { ( sh "$root/scripts/run.sh" __install </dev/null >/dev/null 2>&1 & ); }
if [ "$1" = "__install" ]; then install; exit $?; fi

lastline() { sed -n '/[^[:space:]]/h; ${x;p;}' "$data/install.log" 2>/dev/null | cut -c1-160; }
progress() {   # for the pane: where the install is, how long it has taken, and what it is doing now
  if [ -d "$lock" ]; then
    t=""; now=$(date +%s 2>/dev/null); began=$(cat "$lock/started" 2>/dev/null)
    case "$now$began" in ''|*[!0-9]*) ;; *) t="$((now - began))s - ";; esac
    echo "system-diff: installing: $(cat "$data/stage" 2>/dev/null) - $t$(lastline)" >&2
  else
    echo "system-diff: installing: starting" >&2
  fi
}

for py in "$dir/.venv/bin/python" "$dir/.venv/Scripts/python.exe"; do
  if has "$py"; then exec "$py" -m system_diff "$@"; fi
done
if current; then exec "$own" -m system_diff "$@"; fi

if [ "$1" = "hook" ]; then
  # a hook never waits for an install: start it in the background, and meanwhile use any system-diff there is
  background
  if command -v system-diff >/dev/null 2>&1; then exec system-diff "$@"; fi
  if has "$own"; then exec "$own" -m system_diff "$@"; fi
  cat >/dev/null   # its input, read and let go
  exit 0
fi

# anything else: an older copy or one on PATH will do while the plugin's own is installed
if command -v system-diff >/dev/null 2>&1; then
  background
  exec system-diff "$@"
fi
if has "$own"; then background; exec "$own" -m system_diff "$@"; fi
if [ "$1" = "mcp" ]; then   # the MCP server starts once a session: it waits for the install
  if install || current; then exec "$own" -m system_diff "$@"; fi
  echo "system-diff: could not install the engine (it needs uv or Python 3.11+, or the internet to fetch uv): see $data/install.log" >&2
  exit 1
fi
# the pane asks again every few seconds: it shows how the install is going, or why it failed (tried again a minute on)
stale
if [ ! -d "$lock" ] && [ "$(cat "$data/stage" 2>/dev/null)" = "failed" ] &&
   [ -z "$(find "$data/stage" -mmin +1 2>/dev/null)" ]; then
  echo "system-diff: install failed: $(lastline) - log: $data/install.log" >&2
  exit 4
fi
[ -d "$lock" ] || background
progress
exit 3
