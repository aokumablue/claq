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
