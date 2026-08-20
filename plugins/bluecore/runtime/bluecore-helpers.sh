#!/usr/bin/env bash
# Shared helper functions for command docs.

# Capture this file's own directory at source time, at file top level (not
# inside a function). Under bash, BASH_SOURCE[0] is the sourced file; under
# zsh, a function's $0 is the function name (not the file), but at top level
# of a sourced script $0 is the sourced file itself. Resolving this eagerly
# with `cd ... && pwd` also means the value stays correct even if the caller
# later cd's elsewhere before calling bluecore_plugin_root.
_BLUECORE_HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# Resolve the plugin root from CLAUDE_PLUGIN_ROOT first, then this file's
# location. The helpers are usually sourced from command snippets.
bluecore_plugin_root() {
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
    printf '%s\n' "$CLAUDE_PLUGIN_ROOT"
    return 0
  fi

  printf '%s\n' "$(cd "${_BLUECORE_HELPERS_DIR}/.." && pwd)"
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

# Record one knowledge card in the mem database (`mem learn`).
#
# Usage:
#   bluecore_mem_learn --kind <kind> --title "<one line>" \
#                      [--body "<why/how>"] [--key <slug>] \
#                      [--scope repo|global] [--domain <tag>] \
#                      [--confidence 0.0-1.0] \
#                      [--source-ref <path-or-url>]
#
# --kind and --title are required; everything else has a default.
#   kind    : convention | decision | pitfall | howto | fact | preference
#   scope   : repo (default) | global
#   status  : always pending. This helper has no --status/--source flag
#             (H-01: caller-supplied source/status are not trusted as
#             authority for the generic learn path — `mem.cli learn`
#             itself rejects them). Every card it records has
#             source=agent, status=pending, and needs a human to run
#             `/instinct promote <key>` before it is injected at
#             SessionStart.
#   key     : defaults to a slug derived from the title, so re-recording the
#             same title updates that card instead of piling up duplicates.
#
# Values are marshalled into JSON by python3, so quotes, newlines and
# non-ASCII text in --title / --body need no shell escaping.
bluecore_mem_learn() {
  local card_key="" card_kind="" card_scope="" card_title="" card_body=""
  local card_domain="" card_confidence="" card_source_ref=""

  while [ "$#" -gt 0 ]; do
    if [ "$#" -lt 2 ]; then
      printf 'bluecore_mem_learn: %s needs a value\n' "$1" >&2
      return 2
    fi
    case "$1" in
      --key) card_key="$2" ;;
      --kind) card_kind="$2" ;;
      --scope) card_scope="$2" ;;
      --title) card_title="$2" ;;
      --body) card_body="$2" ;;
      --domain) card_domain="$2" ;;
      --confidence) card_confidence="$2" ;;
      --source-ref) card_source_ref="$2" ;;
      *)
        printf 'bluecore_mem_learn: unknown option: %s\n' "$1" >&2
        return 2
        ;;
    esac
    shift 2
  done

  if [ -z "$card_kind" ] || [ -z "$card_title" ]; then
    printf 'bluecore_mem_learn: --kind and --title are required\n' >&2
    return 2
  fi

  BLUECORE_LEARN_KEY="$card_key" \
  BLUECORE_LEARN_KIND="$card_kind" \
  BLUECORE_LEARN_SCOPE="$card_scope" \
  BLUECORE_LEARN_TITLE="$card_title" \
  BLUECORE_LEARN_BODY="$card_body" \
  BLUECORE_LEARN_DOMAIN="$card_domain" \
  BLUECORE_LEARN_CONFIDENCE="$card_confidence" \
  BLUECORE_LEARN_SOURCE_REF="$card_source_ref" \
  python3 -c 'import json, os, sys
fields = ("key", "kind", "scope", "title", "body", "domain", "confidence", "source_ref")
payload = {f: os.environ["BLUECORE_LEARN_" + f.upper()] for f in fields}
json.dump({k: v for k, v in payload.items() if v}, sys.stdout, ensure_ascii=False)
' | bluecore_run bluecore.mem.cli learn
}

# Collect the repeated inputs used by /skill-gen.
collect_skill_create_inputs() {
  local commits="${1:-200}"

  printf '%s\n' "# 最近のコミットとファイル変更"
  git log --oneline -n "${commits}" --name-only --pretty=format:"%H|%s|%ad" --date=short

  printf '\n%s\n' "# ファイルごとのコミット頻度"
  git log --oneline -n 200 --name-only | grep -v "^$" | grep -v "^[a-f0-9]" | sort | uniq -c | sort -rn | head -20
}
