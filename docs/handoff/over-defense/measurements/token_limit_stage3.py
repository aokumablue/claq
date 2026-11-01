"""段階 3: 作れる形（単一コマンド・結合文字列が 64KiB の手前の argv 配列）を上限の有無で測る（token_limit_protocol.md）。"""
import json, os, shlex, subprocess, sys, time
from pathlib import Path
REPO = Path(__file__).resolve().parents[4]
ROOT = REPO / "plugins/claq"
RUNNER = """
import runpy, sys
import claq.hooks.shell_tokens as s
if sys.argv[1] == "nolimit":
    s.MAX_COMMAND_TOKENS = 10**9
sys.argv = [sys.argv[2]]
runpy.run_module(sys.argv[0], run_name="__main__")
"""
LIMIT = 64 * 1024 - 64


def distinct(pattern, budget, start=0):
    """pattern の {i} を変えながら budget バイト未満まで並べる。"""
    out, i, size = [], start, 0
    while size + len(pattern) + 8 < budget:
        piece = pattern.format(i=i); out.append(piece); size += len(piece); i += 1
    return "".join(out)


def argv_fit(make_elements):
    """結合文字列が 64KiB の手前に収まる最大の要素数で argv 配列を作る。"""
    lo, hi = 1, 20000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(shlex.join(make_elements(mid)).encode()) <= LIMIT:
            lo = mid
        else:
            hi = mid - 1
    return make_elements(lo)


git_big = ("git " * (LIMIT // 4))[: LIMIT - 8] + " a\\b"
cases = {
    "single git + bs": git_big,
    "single distinct + bs": distinct(">x{i};", LIMIT - 8) + "a\\b",
    "argv [bash,-lc,git big]": argv_fit(lambda n: ["bash", "-lc", " ".join(["git"] * n) + " a\\b"]),
    "argv [bash,-lc,distinct]": argv_fit(lambda n: ["bash", "-lc", "".join(f">x{i};" for i in range(n)) + "a\\b"]),
    "argv many git elements": argv_fit(lambda n: [" ".join(["git"] * 400) + " a\\b"] * max(1, n // 400)),
    "argv many distinct elements": argv_fit(lambda n: ["".join(f">x{k * 1000 + i};" for i in range(150)) + "a\\b" for k in range(max(1, n // 150))]),
}
for module in ["claq.hooks.block_no_verify", "claq.hooks.bash_config_protection", "claq.hooks.pre_bash_commit_quality"]:
    for name, command in cases.items():
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(REPO)}).encode()
        row = []
        for mode in ("nolimit", "limit"):
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "CLAQ_HOME": sys.argv[1]}
            t = time.monotonic()
            r = subprocess.run([sys.executable, "-c", RUNNER, mode, module], input=payload, capture_output=True, env=env, timeout=600, cwd=REPO)
            row.append(f"{mode} rc={r.returncode} {time.monotonic() - t:6.2f}s")
        print(f"{module.rsplit('.', 1)[1]:24} {name:28} payload={len(payload):7} | " + " | ".join(row), flush=True)
