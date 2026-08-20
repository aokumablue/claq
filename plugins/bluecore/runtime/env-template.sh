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
# Resolution order:
#   1. PATH: any entry whose parent directory has runtime/bluecore-helpers.sh
#      (the host puts the plugin's bin/ on PATH; this is the most precise match
#      because it names the host that is *currently* running this shell).
#   2. $HOME/.bluecore/roots/$PPID.sh — the plugin root recorded by the bluecore
#      launcher process that is this shell's parent (written on every hook
#      invocation, so it self-heals across plugin updates).
#   3. $HOME/.bluecore/roots/latest.sh — the most recently recorded root, as a
#      last resort when PPID does not match (e.g. the host wraps hook launches
#      in an extra shell layer).
#
# If none resolve, this prints a message to stderr and returns/exits 127 so the
# `|| exit 127` in the usage line above stops the caller instead of running
# `bluecore_run: command not found` a moment later.

_bluecore_env_root=""

_bluecore_env_root="$(
  printf '%s\n' "$PATH" | tr ':' '\n' | while IFS= read -r _bluecore_env_bindir; do
    [ -n "$_bluecore_env_bindir" ] || continue
    _bluecore_env_candidate="$(CDPATH= cd -- "$_bluecore_env_bindir/.." 2>/dev/null && pwd)"
    if [ -n "$_bluecore_env_candidate" ] && [ -f "$_bluecore_env_candidate/runtime/bluecore-helpers.sh" ]; then
      printf '%s\n' "$_bluecore_env_candidate"
      break
    fi
  done
)"

if [ -z "$_bluecore_env_root" ] && [ -f "$HOME/.bluecore/roots/$PPID.sh" ]; then
  . "$HOME/.bluecore/roots/$PPID.sh"
  _bluecore_env_root="$BLUECORE_ROOT"
fi

if [ -z "$_bluecore_env_root" ] && [ -f "$HOME/.bluecore/roots/latest.sh" ]; then
  . "$HOME/.bluecore/roots/latest.sh"
  _bluecore_env_root="$BLUECORE_ROOT"
fi

if [ -z "$_bluecore_env_root" ] || [ ! -f "$_bluecore_env_root/runtime/bluecore-helpers.sh" ]; then
  printf '%s\n' "bluecore: could not resolve the plugin root (no PATH match, no ~/.bluecore/roots pointer). Run any bluecore-backed command once through the host first so it can record its root." >&2
  unset _bluecore_env_root BLUECORE_ROOT
  return 127 2>/dev/null || exit 127
fi

. "$_bluecore_env_root/runtime/bluecore-helpers.sh"
unset _bluecore_env_root BLUECORE_ROOT
