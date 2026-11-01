"""保護フックを実プロセスで起動し、上限ぎりぎりの入力での壁時計時間を測る。"""
import json, os, subprocess, sys, time
from pathlib import Path
REPO = Path(__file__).resolve().parents[4]
ROOT = REPO / "plugins/claq"
LAUNCHER = ROOT / "src/claq/launcher.py"
def run(module, payload):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "CLAQ_HOME": sys.argv[1]}
    data = json.dumps(payload).encode()
    t = time.monotonic()
    r = subprocess.run([sys.executable, str(LAUNCHER), module], input=data, capture_output=True, env=env, timeout=120)
    return time.monotonic() - t, r.returncode, len(data)
def bash(cmd): return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(REPO)}
cases = {
 "5000 tokens 'a'": bash(" ".join(["a"] * 4999)),
 "5000 tokens 'git'": bash(" ".join(["git"] * 4999)),
 "5000 tokens git commit chain": bash(" && ".join(["git commit -m x"] * 1250)[: 10**9]),
 "5000 tokens quoted": bash(" ".join(['"a b"'] * 4999)),
 "64KiB single token": bash("x" * (64 * 1024 - 10)),
 "64KiB quoted token": bash('"' + "x" * (64 * 1024 - 12) + '"'),
}
for module in ["claq.hooks.block_no_verify", "claq.hooks.bash_config_protection", "claq.hooks.pre_bash_commit_quality"]:
    for name, payload in cases.items():
        cmd = payload["tool_input"]["command"]
        sec, rc, n = run(module, payload)
        print(f"{module.rsplit('.',1)[1]:28} {name:32} tokens~{len(cmd.split()):5} bytes={len(cmd.encode()):6} rc={rc} {sec:6.2f}s")
