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

# Record one knowledge card in the mem database (`mem learn`).
#
# Usage:
#   bluecore_mem_learn --kind <kind> --title "<one line>" \
#                      [--body "<why/how>"] [--key <slug>] \
#                      [--scope repo|global] [--domain <tag>] \
#                      [--confidence 0.0-1.0] [--status active|pending] \
#                      [--source-ref <path-or-url>]
#
# --kind and --title are required; everything else has a default.
#   kind    : convention | decision | pitfall | howto | fact | preference
#   scope   : repo (default) | global
#   status  : active (default, injected at SessionStart) | pending (needs
#             a human to run `/instinct promote` before it is injected)
#   key     : defaults to a slug derived from the title, so re-recording the
#             same title updates that card instead of piling up duplicates.
#
# Values are marshalled into JSON by python3, so quotes, newlines and
# non-ASCII text in --title / --body need no shell escaping.
bluecore_mem_learn() {
  # zsh marks `status` (and friends) read-only, so every local is prefixed.
  local card_key="" card_kind="" card_scope="" card_title="" card_body=""
  local card_domain="" card_confidence="" card_status="" card_source_ref=""

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
      --status) card_status="$2" ;;
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
  BLUECORE_LEARN_STATUS="$card_status" \
  BLUECORE_LEARN_SOURCE_REF="$card_source_ref" \
  python3 -c 'import json, os, sys
fields = ("key", "kind", "scope", "title", "body", "domain", "confidence", "status", "source_ref")
payload = {f: os.environ["BLUECORE_LEARN_" + f.upper()] for f in fields}
json.dump({k: v for k, v in payload.items() if v}, sys.stdout, ensure_ascii=False)
' | bluecore_run bluecore.mem.cli learn
}

# Append one loop-dev iteration telemetry record to the JSONL raw log.
#
# Telemetry is a raw log, NOT knowledge: it never goes into the `knowledge`
# table. `bluecore_mem_learn` stays knowledge-only — use it for the lessons
# learned during a run, and this function for the run's own metrics. The
# records land in ~/.bluecore/repos/<repo-id>/loop-dev.jsonl next to the learn
# observation log, and rotate/purge on the same 10MB / 30-day policy.
#
# Usage:
#   bluecore_loop_telemetry --task "<one line>" \
#                           --result converged|not-converged|circuit-break|stopped \
#                           --iterations <n> [--max-iterations <n>] \
#                           [--blockers <n>] [--flakes <n>] \
#                           [--commit <hash>]... [--note "<text>"]
#
# --task, --result and --iterations are required; the rest default to 0 / 2.
# --commit may be repeated once per commit produced by the run.
#
# The record path is resolved through the `repos` ledger (SQLite), so the CLI
# arms a hard timeout (5s) on itself and drops the record rather than stalling
# the caller. On success it prints the JSONL path it wrote to.
bluecore_loop_telemetry() {
  bluecore_run bluecore.skills.loop_dev.telemetry record "$@"
}

# Read loop-dev iteration telemetry back as JSONL (archives included).
#
# Usage:
#   bluecore_loop_telemetry_list [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--limit <n>]
#
# Prints every matching record, oldest first, one JSON object per line. There
# is no relevance cutoff, so callers get an exact count rather than a sample.
bluecore_loop_telemetry_list() {
  bluecore_run bluecore.skills.loop_dev.telemetry list "$@"
}

# Collect the repeated inputs used by /skill-gen.
collect_skill_create_inputs() {
  local commits="${1:-200}"

  printf '%s\n' "# 最近のコミットとファイル変更"
  git log --oneline -n "${commits}" --name-only --pretty=format:"%H|%s|%ad" --date=short

  printf '\n%s\n' "# ファイルごとのコミット頻度"
  git log --oneline -n 200 --name-only | grep -v "^$" | grep -v "^[a-f0-9]" | sort | uniq -c | sort -rn | head -20
}
