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
# holds two lines — the absolute plugin root path, then the pid's own process
# start time (`ps -o lstart=`) as an opaque identity token. This resolver never
# `.`/`source`s them — it only `read`s the two lines — so no content placed in
# a pointer file can execute, regardless of what characters it contains.
#
# Resolution: walk $PPID and up to 3 more process ancestors (`ps -o ppid=`).
# For each ancestor pid, if roots/<pid> exists, has both lines, and the
# recorded start-time line matches that pid's *current* `ps -o lstart=`
# output byte-for-byte, use its recorded root. A mismatch means the pid was
# reused by an unrelated process since the pointer was written — skip it.
# An empty file means the writer found a conflicting root at that ancestor
# (two different hosts share it, e.g. the same terminal) and poisoned it —
# skip it too.
#
# There is no second-tier fallback. If no ancestor yields a verified match,
# this exits 127 rather than guessing (docs/reports/
# PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md H-02: an
# earlier design fell back to "the root every valid pointer agrees on",
# which cannot prove the shell asking is actually that host's descendant).

_bluecore_env_root=""

_bluecore_resolve_ancestor_pointer() {
  # $1: candidate pid. On success, prints the recorded root on stdout and
  # returns 0. Prints nothing and returns 1 if the pointer is missing,
  # empty (poisoned), malformed, or its recorded start-time does not match
  # the pid's current start-time (pid reuse).
  _bluecore_walk_candidate_pid="$1"
  _bluecore_walk_pointer="$HOME/.bluecore/roots/$_bluecore_walk_candidate_pid"
  if [ -L "$_bluecore_walk_pointer" ] || [ ! -f "$_bluecore_walk_pointer" ]; then
    return 1
  fi

  _bluecore_walk_root=""
  _bluecore_walk_recorded_lstart=""
  {
    IFS= read -r _bluecore_walk_root
    IFS= read -r _bluecore_walk_recorded_lstart
  } < "$_bluecore_walk_pointer" 2>/dev/null
  if [ -z "$_bluecore_walk_root" ] || [ -z "$_bluecore_walk_recorded_lstart" ]; then
    return 1
  fi

  # LC_ALL=C: `ps -o lstart=` の出力はロケール依存（LANG=ja_JP.UTF-8 では
  # "木  8/20 ..."、C ロケールでは "Thu Aug 20 ..."。実機で確認済み）。
  # writer（env_pointer.py も同じく LC_ALL=C で ps を呼ぶ）と ambient
  # locale が異なる環境で一致判定させないため、両側で固定する。
  _bluecore_walk_actual_lstart="$(LC_ALL=C ps -o lstart= -p "$_bluecore_walk_candidate_pid" 2>/dev/null)"
  if [ -z "$_bluecore_walk_actual_lstart" ] || [ "$_bluecore_walk_actual_lstart" != "$_bluecore_walk_recorded_lstart" ]; then
    return 1
  fi

  printf '%s\n' "$_bluecore_walk_root"
  return 0
}

_bluecore_walk_pid="$PPID"
_bluecore_walk_depth=0
while [ "$_bluecore_walk_depth" -lt 4 ]; do
  _bluecore_env_root="$(_bluecore_resolve_ancestor_pointer "$_bluecore_walk_pid")"
  [ -n "$_bluecore_env_root" ] && break
  _bluecore_walk_parent="$(ps -o ppid= -p "$_bluecore_walk_pid" 2>/dev/null | tr -d ' ')"
  case "$_bluecore_walk_parent" in
    '' | 0 | 1) break ;;
  esac
  _bluecore_walk_pid="$_bluecore_walk_parent"
  _bluecore_walk_depth=$((_bluecore_walk_depth + 1))
done
unset _bluecore_walk_pid _bluecore_walk_depth _bluecore_walk_parent \
  _bluecore_walk_candidate_pid _bluecore_walk_pointer _bluecore_walk_root \
  _bluecore_walk_recorded_lstart _bluecore_walk_actual_lstart

if [ -z "$_bluecore_env_root" ] || [ ! -f "$_bluecore_env_root/runtime/bluecore-helpers.sh" ]; then
  printf '%s\n' "bluecore: could not resolve the plugin root (no verified ~/.bluecore/roots ancestor pointer). Run any bluecore-backed command once through this host first so it can record its root." >&2
  unset _bluecore_env_root
  return 127 2>/dev/null || exit 127
fi

. "$_bluecore_env_root/runtime/bluecore-helpers.sh"
unset _bluecore_env_root
