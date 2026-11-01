"""上限を外した状態で、疑わしい形の時間を入力サイズを変えて測る（プロセス内、起動時間は含まない）。"""
import sys, time
import claq.hooks.shell_tokens as s
s.MAX_COMMAND_TOKENS = 10**9
from claq.hooks import bash_config_protection as bcp, pre_bash_commit_quality as pbcq
LIMIT = 64 * 1024 - 64
def fill(unit, tail="", limit=LIMIT): return unit * ((limit - len(tail)) // len(unit)) + tail
def t_bcp(cmd):
    st = time.monotonic()
    try: r = bcp.find_protected_write(cmd)
    except bcp.ScanBudgetExceededError: r = "BUDGET"
    return time.monotonic() - st, r
def t_pb(cmd):
    st = time.monotonic(); r = pbcq._detect_group_commits([cmd]); return time.monotonic() - st, (None if r is None else len(r))
forms = {
 "bcp > x ;": (t_bcp, lambda L: fill("> x ;", limit=L)),
 "bcp > x ; +bs": (t_bcp, lambda L: fill("> x ;", "a\\b", limit=L)),
 "bcp echo >x (1seg)": (t_bcp, lambda L: "echo" + fill(" >x", limit=L - 4)),
 "bcp >x;": (t_bcp, lambda L: fill(">x;", limit=L)),
 "bcp >x; +bs": (t_bcp, lambda L: fill(">x;", "a\\b", limit=L)),
 "bcp sh -c '>x;>x'": (t_bcp, lambda L: fill("sh -c '>x;>x;>x;>x';", limit=L)),
 "bcp sh -c bundle big +bs": (t_bcp, lambda L: " ; ".join(["sh -c '" + fill(">x;", "a\\b", limit=L // 3 - 20) + "'"] * 3)),
 "pb git": (t_pb, lambda L: fill("git ", limit=L)),
 "pb git +bs": (t_pb, lambda L: fill("git ", "a\\b", limit=L)),
 "pb git -c a=b": (t_pb, lambda L: fill("git -c a=b ", limit=L)),
 "pb git;": (t_pb, lambda L: fill("git ;", limit=L)),
 "pb sudo git": (t_pb, lambda L: fill("sudo git ", "a\\b", limit=L)),
}
for name, (fn, mk) in forms.items():
    out = []
    for L in (LIMIT // 4, LIMIT // 2, LIMIT):
        sec, r = fn(mk(L)); out.append(f"{L//1024:2}KiB {sec:6.2f}s r={r}")
    print(f"{name:28} " + " | ".join(out), flush=True)
