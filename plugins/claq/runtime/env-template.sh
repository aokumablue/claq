#!/usr/bin/env sh
# claq plugin root resolver — installed to $HOME/.claq/env.sh verbatim by
# claq.lib.env_pointer.write_env_pointer(). Contains no vendor-specific paths;
# do not hardcode host names here (see docs/adr/03-plugin-root-resolution.md for why).
#
# Usage (in agents/commands/skills):
#   . "$HOME/.claq/env.sh" || exit 127
#   claq_run claq.mem.cli learn <<'JSON'
#   ...
#
# roots/<pid> files are DATA, never shell code (R-01/R-03): each
# holds two lines — the absolute plugin root path, then the pid's own process
# start time (`ps -o lstart=`) as an opaque identity token. This resolver never
# `.`/`source`s them — it only `read`s the two lines — so no content placed in
# a pointer file can execute, regardless of what characters it contains.
#
# Resolution: walk $PPID and its parent (`ps -o ppid=`) — 2 pids total. Both
# are exclusive to this host instance (the bash tool's own shell, and the
# host binary itself), never shared with another host, so the writer
# (env_pointer.py) only ever records here — no further ancestors. For each
# candidate pid, if roots/<pid> exists, has both lines, and the recorded
# start-time line matches that pid's *current* `ps -o lstart=` output
# byte-for-byte, use its recorded root. A mismatch means the pid was reused
# by an unrelated process since the pointer was written — skip it. An empty
# or malformed file is treated the same way (defensive only — the writer no
# longer produces empty files by design; see docs/adr/03-plugin-root-resolution.md revision 3).
#
# There is no second-tier fallback. If neither candidate yields a verified
# match, this exits 127 rather than guessing (H-02: an
# earlier design fell back to "the root every valid pointer agrees on",
# which cannot prove the shell asking is actually that host's descendant).

_claq_env_root=""

_claq_resolve_ancestor_pointer() {
  # $1: candidate pid. On success, prints the recorded root on stdout and
  # returns 0. Prints nothing and returns 1 if the pointer is missing,
  # empty or malformed (defensive only — the writer no longer produces
  # these), or its recorded start-time does not match the pid's current
  # start-time (pid reuse).
  _claq_walk_candidate_pid="$1"
  _claq_walk_pointer="$HOME/.claq/roots/$_claq_walk_candidate_pid"
  if [ -L "$_claq_walk_pointer" ] || [ ! -f "$_claq_walk_pointer" ]; then
    return 1
  fi

  _claq_walk_root=""
  _claq_walk_recorded_lstart=""
  {
    IFS= read -r _claq_walk_root
    IFS= read -r _claq_walk_recorded_lstart
  } < "$_claq_walk_pointer" 2>/dev/null
  if [ -z "$_claq_walk_root" ] || [ -z "$_claq_walk_recorded_lstart" ]; then
    return 1
  fi

  # LC_ALL=C: `ps -o lstart=` の出力はロケール依存（LANG=ja_JP.UTF-8 では
  # "木  8/20 ..."、C ロケールでは "Thu Aug 20 ..."。実機で確認済み）。
  # writer（env_pointer.py も同じく LC_ALL=C で ps を呼ぶ）と ambient
  # locale が異なる環境で一致判定させないため、両側で固定する。
  _claq_walk_actual_lstart="$(LC_ALL=C ps -o lstart= -p "$_claq_walk_candidate_pid" 2>/dev/null)"
  if [ -z "$_claq_walk_actual_lstart" ] || [ "$_claq_walk_actual_lstart" != "$_claq_walk_recorded_lstart" ]; then
    return 1
  fi

  printf '%s\n' "$_claq_walk_root"
  return 0
}

_claq_walk_pid="$PPID"
_claq_walk_depth=0
while [ "$_claq_walk_depth" -lt 2 ]; do
  # `set -e` 下で 1 候補目のミス（正常経路）が silent exit にならないよう守る。
  # `runtime/claq-helpers.sh` が同一パターンで既に採っている形。
  _claq_env_root="$(_claq_resolve_ancestor_pointer "$_claq_walk_pid")" || _claq_env_root=""
  [ -n "$_claq_env_root" ] && break
  _claq_walk_parent="$(ps -o ppid= -p "$_claq_walk_pid" 2>/dev/null | tr -d ' ')"
  case "$_claq_walk_parent" in
    '' | 0 | 1) break ;;
  esac
  _claq_walk_pid="$_claq_walk_parent"
  _claq_walk_depth=$((_claq_walk_depth + 1))
done
unset _claq_walk_pid _claq_walk_depth _claq_walk_parent \
  _claq_walk_candidate_pid _claq_walk_pointer _claq_walk_root \
  _claq_walk_recorded_lstart _claq_walk_actual_lstart

if [ -z "$_claq_env_root" ] || [ ! -f "$_claq_env_root/runtime/claq-helpers.sh" ]; then
  printf '%s\n' "claq: could not resolve the plugin root (no verified ~/.claq/roots ancestor pointer). Run any claq-backed command once through this host first so it can record its root." >&2
  unset _claq_env_root
  return 127 2>/dev/null || exit 127
fi

# Hand the verified root to the helpers explicitly. Without this they would have
# to self-locate, which POSIX sh cannot do for a sourced file (see the header of
# claq-helpers.sh).
_CLAQ_SOURCED_ROOT="$_claq_env_root"
export _CLAQ_SOURCED_ROOT
. "$_claq_env_root/runtime/claq-helpers.sh"
unset _claq_env_root
