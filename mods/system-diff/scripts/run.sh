#!/bin/sh
# Runs `system-diff <args>` for the plugin's hooks (and the pane): the project's own virtualenv first (a project that
# develops system-diff, or pins it), then the runner found before (kept in the plugin's data folder), else system-diff
# on PATH, else any Python that has it. Where none is installed it does nothing and succeeds, so a session in a
# project without system-diff is never held up.
#
# Starting a program can cost seconds (endpoint security scanning each launch), so this starts exactly one: the
# runner. Everything else here is a shell builtin ([ -x ], command -v, read, printf, redirections): no $(…), which
# forks (a new process under Git for Windows' sh), and no grep, cat, mv or rm. Only the very first
# call on a machine without `system-diff` on PATH starts Pythons to find one that has it, and keeps what it found in
# "$data/runner"; a runner that then fails to start (126/127, or Python's "No module named system_diff") is forgotten,
# and the next call looks again. A call is never repeated: the hook's input is read once, by the runner.
data="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/system-diff}"
cache="$data/runner"
novenv="$data/no-venv"      # projects whose .venv has no system-diff: one folder a line
dir="${CLAUDE_PROJECT_DIR:-$PWD}"

drain() { while IFS= read -r _line || [ -n "$_line" ]; do :; done; }   # a hook's input, read and let go

listed() {   # is $1 a line of the file $2? (no grep: a builtin loop)
  [ -f "$2" ] || return 1
  while IFS= read -r _l || [ -n "$_l" ]; do [ "$_l" = "$1" ] && return 0; done < "$2"
  return 1
}

keep() {   # remember the runner: kind (exe, py or none) and the command (a path or a name on PATH), a line each
  [ -d "$data" ] || mkdir -p "$data" 2>/dev/null || return 0   # mkdir: once per machine
  printf '%s\n%s\n' "$1" "$2" > "$cache" 2>/dev/null
}

forget() { [ -f "$cache" ] && : > "$cache" 2>/dev/null; }   # emptied: an empty file is no runner

run() {   # run kind path args...; a runner that could not start is reported by its status
  kind=$1 path=$2
  shift 2
  if [ "$kind" = exe ]; then "$path" "$@"; else "$path" -m system_diff "$@"; fi
}

failed_to_start() {   # $1 kind, $2 status: 126/127 (not runnable), or 1 from `python -m` without the module
  [ "$2" = 126 ] || [ "$2" = 127 ] || { [ "$1" = py ] && [ "$2" = 1 ]; }
}

# 1. the project's own virtualenv (file checks only), unless it was found not to have system-diff
for py in "$dir/.venv/bin/python" "$dir/.venv/Scripts/python.exe"; do
  if [ -x "$py" ] && ! listed "$dir" "$novenv"; then
    run py "$py" "$@"
    st=$?
    if [ "$st" = 1 ] && [ "$1" = hook ]; then   # a hook always exits 0: this is "No module named system_diff"
      [ -d "$data" ] || mkdir -p "$data" 2>/dev/null
      printf '%s\n' "$dir" >> "$novenv" 2>/dev/null
    fi
    exit $st
  fi
done

# 2. the runner found before
kind="" path=""
if [ -s "$cache" ]; then
  { IFS= read -r kind; IFS= read -r path; } < "$cache"
  if [ "$kind" = none ]; then
    if command -v system-diff >/dev/null 2>&1; then forget; else   # still none: the Pythons are not asked again
      if [ "$1" = hook ]; then drain; exit 0; fi
      echo 'system-diff: not found by the launcher: pip install "system-diff[mcp]"' >&2
      exit 127
    fi
  elif [ -n "$path" ] && command -v "$path" >/dev/null 2>&1; then
    run "$kind" "$path" "$@"
    st=$?
    if failed_to_start "$kind" "$st"; then forget; fi
    if [ "$1" = hook ]; then exit 0; fi
    exit $st
  else
    forget   # moved or uninstalled: found again below
  fi
fi

# 3. system-diff on PATH (pip, pipx): found without starting anything
if command -v system-diff >/dev/null 2>&1; then
  keep exe system-diff
  run exe system-diff "$@"
  st=$?
  if failed_to_start exe "$st"; then forget; fi
  exit $st
fi

# 4. any Python that has it: the one call that starts Pythons to ask, its answer kept
for name in python3 python py; do
  if command -v "$name" >/dev/null 2>&1 && "$name" -c "import system_diff" >/dev/null 2>&1; then
    keep py "$name"
    run py "$name" "$@"
    exit $?
  fi
done
keep none ""
if [ "$1" = hook ]; then drain; exit 0; fi
# anything else (the pane) is told, and fails, so the pane tries its other ways
echo 'system-diff: not found by the launcher: pip install "system-diff[mcp]"' >&2
exit 127
