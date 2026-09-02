#!/usr/bin/env sh
# ple4 plugin root resolver — installed to $HOME/.ple4/env.sh verbatim by
# ple4.lib.env_pointer.write_env_pointer(). Contains no vendor-specific paths;
# do not hardcode host names here (see docs/adr/0008-*.md for why).
#
# Usage (in agents/commands/skills):
#   . "$HOME/.ple4/env.sh" || exit 127
#   ple4_run ple4.mem.cli learn <<'JSON'
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
# longer produces empty files by design; see docs/adr/0008-*.md revision 3).
#
# There is no second-tier fallback. If neither candidate yields a verified
# match, this exits 127 rather than guessing (H-02: an
# earlier design fell back to "the root every valid pointer agrees on",
# which cannot prove the shell asking is actually that host's descendant).

_ple4_env_root=""

_ple4_resolve_ancestor_pointer() {
  # $1: candidate pid. On success, prints the recorded root on stdout and
  # returns 0. Prints nothing and returns 1 if the pointer is missing,
  # empty or malformed (defensive only — the writer no longer produces
  # these), or its recorded start-time does not match the pid's current
  # start-time (pid reuse).
  _ple4_walk_candidate_pid="$1"
  _ple4_walk_pointer="$HOME/.ple4/roots/$_ple4_walk_candidate_pid"
  if [ -L "$_ple4_walk_pointer" ] || [ ! -f "$_ple4_walk_pointer" ]; then
    return 1
  fi

  _ple4_walk_root=""
  _ple4_walk_recorded_lstart=""
  {
    IFS= read -r _ple4_walk_root
    IFS= read -r _ple4_walk_recorded_lstart
  } < "$_ple4_walk_pointer" 2>/dev/null
  if [ -z "$_ple4_walk_root" ] || [ -z "$_ple4_walk_recorded_lstart" ]; then
    return 1
  fi

  # LC_ALL=C: `ps -o lstart=` の出力はロケール依存（LANG=ja_JP.UTF-8 では
  # "木  8/20 ..."、C ロケールでは "Thu Aug 20 ..."。実機で確認済み）。
  # writer（env_pointer.py も同じく LC_ALL=C で ps を呼ぶ）と ambient
  # locale が異なる環境で一致判定させないため、両側で固定する。
  _ple4_walk_actual_lstart="$(LC_ALL=C ps -o lstart= -p "$_ple4_walk_candidate_pid" 2>/dev/null)"
  if [ -z "$_ple4_walk_actual_lstart" ] || [ "$_ple4_walk_actual_lstart" != "$_ple4_walk_recorded_lstart" ]; then
    return 1
  fi

  printf '%s\n' "$_ple4_walk_root"
  return 0
}

_ple4_walk_pid="$PPID"
_ple4_walk_depth=0
while [ "$_ple4_walk_depth" -lt 2 ]; do
  _ple4_env_root="$(_ple4_resolve_ancestor_pointer "$_ple4_walk_pid")"
  [ -n "$_ple4_env_root" ] && break
  _ple4_walk_parent="$(ps -o ppid= -p "$_ple4_walk_pid" 2>/dev/null | tr -d ' ')"
  case "$_ple4_walk_parent" in
    '' | 0 | 1) break ;;
  esac
  _ple4_walk_pid="$_ple4_walk_parent"
  _ple4_walk_depth=$((_ple4_walk_depth + 1))
done
unset _ple4_walk_pid _ple4_walk_depth _ple4_walk_parent \
  _ple4_walk_candidate_pid _ple4_walk_pointer _ple4_walk_root \
  _ple4_walk_recorded_lstart _ple4_walk_actual_lstart

if [ -z "$_ple4_env_root" ] || [ ! -f "$_ple4_env_root/runtime/ple4-helpers.sh" ]; then
  printf '%s\n' "ple4: could not resolve the plugin root (no verified ~/.ple4/roots ancestor pointer). Run any ple4-backed command once through this host first so it can record its root." >&2
  unset _ple4_env_root
  return 127 2>/dev/null || exit 127
fi

# Hand the verified root to the helpers explicitly. Without this they would have
# to self-locate, which POSIX sh cannot do for a sourced file (see the header of
# ple4-helpers.sh).
_PLE4_SOURCED_ROOT="$_ple4_env_root"
export _PLE4_SOURCED_ROOT
. "$_ple4_env_root/runtime/ple4-helpers.sh"
unset _ple4_env_root
