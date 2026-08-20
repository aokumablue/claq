#!/usr/bin/env sh
# bluecore plugin root resolver — installed to $HOME/.bluecore/env.sh verbatim by
# bluecore.lib.env_pointer.write_env_pointer(). Contains no vendor-specific paths;
# do not hardcode host names here (see docs/adr/0008-*.md for why).
#
# Usage (in agents/commands/skills):
#   . "$HOME/.bluecore/env.sh" || exit 127
#   bluecore_run bluecore.mem.cli learn <<'JSON'
#   ...
#
# roots/<pid> files are DATA, never shell code (see
# docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_VERIFICATION.md R-01/R-03): each
# holds exactly one line, the absolute plugin root path. This resolver never
# `.`/`source`s them — it only `read`s a single line — so no content placed in
# a pointer file can execute, regardless of what characters it contains.
#
# Resolution order:
#   1. $HOME/.bluecore/roots/$PPID, then up to 3 more process ancestors
#      (ps -o ppid=), each checked for its own roots/<pid> pointer. This
#      covers hosts that wrap hook launches in one or more intermediate
#      shells before reaching the shell that runs this bootstrap line.
#   2. If none of the above match: if roots/ contains exactly one valid
#      pointer, use it (single-host machines always resolve this way, and a
#      "wrong host" mismatch cannot happen when there is only one candidate).
#      If roots/ contains zero or more-than-one valid pointer and no ancestor
#      matched, resolution is ambiguous and this exits 127 rather than
#      guessing (R-05: a blind "most recent" fallback can pick another host's
#      install when multiple hosts are running concurrently).
#
# If nothing resolves, this prints a message to stderr and returns/exits 127
# so the `|| exit 127` in the usage line above stops the caller instead of
# running `bluecore_run: command not found` a moment later.

_bluecore_env_root=""

_bluecore_read_root_pointer() {
  # $1: pointer file path. Prints the single line it contains if the file is
  # a regular, non-symlink, non-empty file; prints nothing otherwise. Never
  # sources it — a pointer is data, not code.
  _bluecore_env_pointer="$1"
  if [ -L "$_bluecore_env_pointer" ] || [ ! -f "$_bluecore_env_pointer" ]; then
    return 1
  fi
  IFS= read -r _bluecore_env_line < "$_bluecore_env_pointer" || return 1
  [ -n "$_bluecore_env_line" ] || return 1
  printf '%s\n' "$_bluecore_env_line"
}

# --- tier 1: $PPID and up to 3 process ancestors ---
_bluecore_walk_pid="$PPID"
_bluecore_walk_depth=0
while [ "$_bluecore_walk_depth" -lt 4 ]; do
  _bluecore_env_root="$(_bluecore_read_root_pointer "$HOME/.bluecore/roots/$_bluecore_walk_pid")"
  [ -n "$_bluecore_env_root" ] && break
  _bluecore_walk_parent="$(ps -o ppid= -p "$_bluecore_walk_pid" 2>/dev/null | tr -d ' ')"
  case "$_bluecore_walk_parent" in
    '' | 0 | 1) break ;;
  esac
  _bluecore_walk_pid="$_bluecore_walk_parent"
  _bluecore_walk_depth=$((_bluecore_walk_depth + 1))
done
unset _bluecore_walk_pid _bluecore_walk_depth _bluecore_walk_parent

# --- tier 2: exactly-one-valid-candidate fallback ---
if [ -z "$_bluecore_env_root" ] && [ -d "$HOME/.bluecore/roots" ]; then
  _bluecore_valid_count=0
  _bluecore_only_root=""
  for _bluecore_entry in "$HOME/.bluecore/roots"/*; do
    [ -e "$_bluecore_entry" ] || continue
    case "$(basename "$_bluecore_entry")" in
      '' | *[!0-9]*) continue ;;
    esac
    _bluecore_candidate="$(_bluecore_read_root_pointer "$_bluecore_entry")"
    [ -n "$_bluecore_candidate" ] || continue
    _bluecore_valid_count=$((_bluecore_valid_count + 1))
    _bluecore_only_root="$_bluecore_candidate"
  done
  if [ "$_bluecore_valid_count" -eq 1 ]; then
    _bluecore_env_root="$_bluecore_only_root"
  fi
  unset _bluecore_valid_count _bluecore_only_root _bluecore_entry _bluecore_candidate
fi

if [ -z "$_bluecore_env_root" ] || [ ! -f "$_bluecore_env_root/runtime/bluecore-helpers.sh" ]; then
  printf '%s\n' "bluecore: could not resolve the plugin root (no matching ~/.bluecore/roots pointer, or multiple hosts are recorded and none is this process's ancestor). Run any bluecore-backed command once through this host first so it can record its root." >&2
  unset _bluecore_env_root
  return 127 2>/dev/null || exit 127
fi

. "$_bluecore_env_root/runtime/bluecore-helpers.sh"
unset _bluecore_env_root
