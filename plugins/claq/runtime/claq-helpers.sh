#!/usr/bin/env sh
# Shared helper functions for command docs. POSIX sh compatible: env-template.sh
# has a `#!/usr/bin/env sh` shebang and sources this file, so anything bash-only
# here breaks the contract on hosts where /bin/sh is dash (Linux).

# Resolve this file's own directory at source time, at file top level (not inside
# a function), so the value stays correct even if the caller later cd's away.
#
# Preferred source: env-template.sh sets _CLAQ_SOURCED_ROOT to the root it
# resolved from the ~/.claq/roots pointer before sourcing us. That is the
# root this file actually came from, so it is the only self-consistent answer.
#
# Fallback: when the helpers are sourced directly (tests, manual use), locate
# this file from the shell's own bookkeeping. POSIX sh has no equivalent of
# BASH_SOURCE and its $0 stays the shell name when sourcing, so this branch is
# gated on bash/zsh. The bash-only expansion is never evaluated under dash
# (dash raises "Bad substitution" at evaluation time, not at parse time).
if [ -n "${_CLAQ_SOURCED_ROOT:-}" ]; then
  _CLAQ_HELPERS_DIR="${_CLAQ_SOURCED_ROOT}/runtime"
elif [ -n "${BASH_VERSION:-}${ZSH_VERSION:-}" ]; then
  _CLAQ_HELPERS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
else
  printf '%s\n' "claq: cannot locate claq-helpers.sh under a POSIX sh. Source \$HOME/.claq/env.sh instead of this file directly." >&2
  return 1 2>/dev/null || exit 1
fi

# Resolve the plugin root from the location this file was actually sourced from.
#
# Deliberately does NOT consult the ambient CLAUDE_PLUGIN_ROOT. The helper body
# running here came from whichever root env.sh selected; reporting a different
# root because an environment variable says so makes the two disagree ("the code
# is A, but it calls itself B"). ADR-0008 claims a verified root, and an ambient
# override would contradict that claim. Using the environment to decide *which*
# root to source is fine; overriding the answer after the fact is not.
#
# The `cd` is checked. It fails when the directory this file was sourced from no
# longer exists -- the normal case is a plugin upgrade replacing the versioned
# cache directory while a shell that already sourced env.sh keeps running. The
# old body printed an empty line and returned 0 there, so `claq_run` went on to
# execute "/runtime/claq-hook" and every later call failed with an error that
# named neither the stale root nor the upgrade. Report the actual cause once and
# return non-zero (127: the resolved command is not there).
claq_plugin_root() {
  local plugin_root
  # `|| plugin_root=""` keeps the failure out of `set -e`'s hands so the message
  # below is what the caller sees, instead of a silent exit at this line.
  plugin_root="$(cd "${_CLAQ_HELPERS_DIR}/.." 2>/dev/null && pwd)" || plugin_root=""
  if [ -z "${plugin_root}" ]; then
    printf '%s\n' "claq: stale plugin root: ${_CLAQ_HELPERS_DIR} is gone (plugin upgraded?). Start a new shell, or re-source \$HOME/.claq/env.sh." >&2
    return 127
  fi
  printf '%s\n' "${plugin_root}"
}

# Run a claq module or script through the hook wrapper.
#
# The wrapper (`runtime/claq-hook`) owns interpreter resolution for the whole
# plugin. Calling `python3` here instead would reintroduce the failure that
# broke every hook on Windows (a Microsoft Store alias owning the name
# `python3`), and would make the helpers disagree with hooks.json about which
# interpreter claq runs on.
# Root 解決の失敗はそのまま伝播させる（`claq_plugin_root` が理由を stderr へ
# 出し済みなので、ここで重ねて出さない）。`local` と代入を分けているのは、
# `local x="$(...)"` にすると `local` の終了ステータスが代入側を覆い隠して
# `||` が発火しなくなるため。
claq_run() {
  local plugin_root
  plugin_root="$(claq_plugin_root)" || return 127
  "${plugin_root}/runtime/claq-hook" "$@"
}

# Record one knowledge card in the mem database (`mem learn`).
#
# Usage:
#   claq_mem_learn --kind <kind> --title "<one line>" \
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
claq_mem_learn() {
  local card_key="" card_kind="" card_scope="" card_title="" card_body=""
  local card_domain="" card_confidence="" card_source_ref=""

  while [ "$#" -gt 0 ]; do
    if [ "$#" -lt 2 ]; then
      printf 'claq_mem_learn: %s needs a value\n' "$1" >&2
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
        printf 'claq_mem_learn: unknown option: %s\n' "$1" >&2
        return 2
        ;;
    esac
    shift 2
  done

  if [ -z "$card_kind" ] || [ -z "$card_title" ]; then
    printf 'claq_mem_learn: --kind and --title are required\n' >&2
    return 2
  fi

  CLAQ_LEARN_KEY="$card_key" \
  CLAQ_LEARN_KIND="$card_kind" \
  CLAQ_LEARN_SCOPE="$card_scope" \
  CLAQ_LEARN_TITLE="$card_title" \
  CLAQ_LEARN_BODY="$card_body" \
  CLAQ_LEARN_DOMAIN="$card_domain" \
  CLAQ_LEARN_CONFIDENCE="$card_confidence" \
  CLAQ_LEARN_SOURCE_REF="$card_source_ref" \
  claq_run claq.mem.learn_payload | claq_run claq.mem.cli learn
}

# Collect the repeated inputs used by /skill-gen.
# Argument 1 is the number of commits to look back over (default 200); it applies
# to both git log calls. Hardcoding the second one made the argument silently
# half-effective, so the two sections described different ranges.
collect_skill_create_inputs() {
  local commits="${1:-200}"

  case "${commits}" in
    ''|*[!0-9]*)
      printf '%s\n' "collect_skill_create_inputs: commits must be a positive integer: ${commits}" >&2
      return 2
      ;;
  esac
  if [ "${commits}" -lt 1 ]; then
    printf '%s\n' "collect_skill_create_inputs: commits must be a positive integer: ${commits}" >&2
    return 2
  fi

  printf '%s\n' "# 最近のコミットとファイル変更"
  git log --oneline -n "${commits}" --name-only --pretty=format:"%H|%s|%ad" --date=short

  printf '\n%s\n' "# ファイルごとのコミット頻度"
  # 空の `--pretty=format:` でコミット見出しを最初から出力しない。
  # 以前は `--oneline` で見出しを出してから `grep -v "^[a-f0-9]"` で短縮 SHA を
  # 落としていたが、これは 16 進数字で始まる**実在パスも巻き添えにする**。
  # 実測で agents/ commands/ docs/ conftest.py が消え、plugins/ scripts/ は
  # 残るため欠落に気づけず、/skill-gen が「agents/commands はほとんど触られて
  # いない」という誤った頻度表を受け取っていた。出力しないものは選り分ける
  # 必要も無いので、残すフィルタは空行除去だけでよい。
  git log -n "${commits}" --name-only --pretty=format: | grep -v "^$" | sort | uniq -c | sort -rn | head -20
}
