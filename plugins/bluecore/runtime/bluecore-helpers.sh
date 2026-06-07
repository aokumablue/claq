#!/usr/bin/env bash
# Shared helper functions for command docs.

# Resolve the plugin root from CLAUDE_PLUGIN_ROOT first, then this file's
# location. The helpers are usually sourced from command snippets.
bluecore_plugin_root() {
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    printf '%s\n' "$CLAUDE_PLUGIN_ROOT"
    return 0
  fi

  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  printf '%s\n' "$(cd "${script_dir}/.." && pwd)"
}

# Run a bluecore module or script through the repository launcher.
bluecore_run() {
  local plugin_root
  plugin_root="$(bluecore_plugin_root)"
  python3 "${plugin_root}/src/bluecore/launcher.py" "$@"
}

# Pipe JSON input into bluecore.mem subcommands.
bluecore_mem_json() {
  local command="${1:?Usage: bluecore_mem_json <subcommand> [json] }"
  shift || true

  if [ "$#" -gt 0 ]; then
    printf '%s' "$1" | bluecore_run bluecore.mem.cli "$command"
  else
    cat | bluecore_run bluecore.mem.cli "$command"
  fi
}

# Build and execute a repository-scoped mem search payload.
bluecore_mem_search() {
  local query="${1:?Usage: bluecore_mem_search <query> [limit]}"
  local limit="${2:-3}"
  local cwd
  cwd="$(git rev-parse --show-toplevel)"

  bluecore_mem_json search "$(
    python3 - "$cwd" "$query" "$limit" <<'PY'
import json
import sys

cwd, query, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
print(json.dumps({"cwd": cwd, "query": query, "limit": limit}))
PY
  )"
}

# Run a bluecore launcher command in the background and print the PID.
bluecore_run_bg() {
  local plugin_root
  plugin_root="$(bluecore_plugin_root)"

  nohup python3 "${plugin_root}/src/bluecore/launcher.py" "$@" >/dev/null 2>&1 &
  printf '%s\n' "$!"
}

# Collect the repeated inputs used by /skill-gen.
collect_skill_create_inputs() {
  local commits="${1:-200}"

  printf '%s\n' "# 最近のコミットとファイル変更"
  git log --oneline -n "${commits}" --name-only --pretty=format:"%H|%s|%ad" --date=short

  printf '\n%s\n' "# ファイルごとのコミット頻度"
  git log --oneline -n 200 --name-only | grep -v "^$" | grep -v "^[a-f0-9]" | sort | uniq -c | sort -rn | head -20
}
