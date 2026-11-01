"""1 つの payload に複数のコマンドを載せたときの時間を、上限の有無で測る。"""
import json, os, subprocess, sys, time
from pathlib import Path
REPO = Path(sys.argv[2]); ROOT = REPO / "plugins/claq"
RUNNER = """
import runpy, sys
import claq.hooks.shell_tokens as s
if sys.argv[1] == "nolimit":
    s.MAX_COMMAND_TOKENS = 10**9
sys.argv = [sys.argv[2]]
runpy.run_module(sys.argv[0], run_name="__main__")
"""
LIMIT = 64 * 1024 - 64
git = ("git " * (LIMIT // 4))[: LIMIT - 4] + "a\\b"
def distinct(start):
    out, i, size = [], start, 0
    while size < LIMIT - 20:
        p = f">x{i};"; out.append(p); size += len(p); i += 1
    return "".join(out) + "a\\b"
payloads = {
    "pbcq list x14 git": ("claq.hooks.pre_bash_commit_quality", {"tool_name": "Bash", "tool_input": {"command": [git] * 14}}),
    "pbcq 4 containers git": ("claq.hooks.pre_bash_commit_quality", {"tool_name": "Bash", **{k: {"command": git} for k in ("tool_input", "toolInput", "toolArgs", "tool_args")}}),
    "bcp list x14 distinct": ("claq.hooks.bash_config_protection", {"tool_name": "Bash", "tool_input": {"command": [distinct(k * 20000) for k in range(14)]}}),
    "bnv list x14 git": ("claq.hooks.block_no_verify", {"tool_name": "Bash", "tool_input": {"command": [git] * 14}}),
}
for name, (module, payload) in payloads.items():
    data = json.dumps(payload).encode()
    row = []
    for mode in ("nolimit", "limit"):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "CLAQ_HOME": sys.argv[1]}
        t = time.monotonic()
        r = subprocess.run([sys.executable, "-c", RUNNER, mode, module], input=data, capture_output=True, env=env, timeout=900, cwd=REPO)
        row.append(f"{mode} rc={r.returncode} {time.monotonic() - t:6.2f}s")
    print(f"{name:24} payload={len(data):8} | " + " | ".join(row), flush=True)
