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

install() {   # the plugin's own copy of the engine, at the plugin's version; one install at a time
  mkdir -p "$data" || return 1
  if [ -d "$data/.installing" ] && [ -n "$(find "$data/.installing" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$data/.installing" 2>/dev/null   # one that died midway, ten minutes ago
  fi
  mkdir "$data/.installing" 2>/dev/null || return 1
  trap 'rmdir "$data/.installing" 2>/dev/null' EXIT
  spec="system-diff[mcp]${version:+==$version}"
  # the plugin's own version; when that is not on PyPI (yet), the latest
  : >"$data/install.log"
  uv=$(command -v uv 2>/dev/null) || uv="$data/uv/uv"; [ -x "$uv" ] || [ ! -x "$data/uv/uv.exe" ] || uv="$data/uv/uv.exe"
  if [ ! -x "$uv" ] && ! python311 >/dev/null; then   # no uv and no Python: fetch uv, and Python with it
    get_uv; uv="$data/uv/uv"; [ -x "$uv" ] || uv="$data/uv/uv.exe"
  fi
  if [ -x "$uv" ]; then
    "$uv" venv --quiet --python ">=3.11" "$venv" >>"$data/install.log" 2>&1 &&
      { "$uv" pip install --quiet --python "$venv" "$spec" >>"$data/install.log" 2>&1 ||
        "$uv" pip install --quiet --python "$venv" "system-diff[mcp]" >>"$data/install.log" 2>&1; }
  else
    py=$(python311) || return 1
    vpy="$venv/bin/python"; case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) vpy="$venv/Scripts/python.exe";; esac
    "$py" -m venv "$venv" >>"$data/install.log" 2>&1 &&
      { "$vpy" -m pip install --quiet --disable-pip-version-check "$spec" >>"$data/install.log" 2>&1 ||
        "$vpy" -m pip install --quiet --disable-pip-version-check "system-diff[mcp]" >>"$data/install.log" 2>&1; }
  fi
  ok=$?
  own="$venv/bin/python"; [ -x "$own" ] || own="$venv/Scripts/python.exe"
  [ $ok -eq 0 ] && has "$own" && printf '%s' "$version" >"$data/version"
  rmdir "$data/.installing" 2>/dev/null; trap - EXIT
  [ $ok -eq 0 ]
}

for py in "$dir/.venv/bin/python" "$dir/.venv/Scripts/python.exe"; do
  if has "$py"; then exec "$py" -m system_diff "$@"; fi
done
if current; then exec "$own" -m system_diff "$@"; fi

if [ "$1" = "hook" ]; then
  # a hook never waits for an install: start it in the background, and meanwhile use any system-diff there is
  ( install </dev/null >/dev/null 2>&1 & )
  if command -v system-diff >/dev/null 2>&1; then exec system-diff "$@"; fi
  if has "$own"; then exec "$own" -m system_diff "$@"; fi
  cat >/dev/null   # its input, read and let go
  exit 0
fi

# anything else: an older copy or one on PATH will do while the plugin's own is installed
if command -v system-diff >/dev/null 2>&1; then
  ( install </dev/null >/dev/null 2>&1 & )
  exec system-diff "$@"
fi
if has "$own"; then ( install </dev/null >/dev/null 2>&1 & ); exec "$own" -m system_diff "$@"; fi
if [ "$1" = "mcp" ]; then   # the MCP server starts once a session: it waits for the install
  if install || current; then exec "$own" -m system_diff "$@"; fi
  echo "system-diff: could not install the engine (it needs uv or Python 3.11+, or the internet to fetch uv): see $data/install.log" >&2
  exit 1
fi
( install </dev/null >/dev/null 2>&1 & )   # the pane asks again every few seconds: it shows that this is under way
echo "system-diff: installing (first use, about a minute)" >&2
exit 3
