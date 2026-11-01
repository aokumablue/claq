"""MAX_COMMAND_TOKENS を外した状態と付けた状態で、3 つの保護フックの壁時計時間を測る（token_limit_protocol.md）。"""
import json, os, subprocess, sys, time
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


def fill(unit, tail=""):
    """unit を 64KiB 未満まで繰り返し、tail を足す。"""
    return unit * ((LIMIT - len(tail)) // len(unit)) + tail


cases = {
    "commit chain": fill("git commit -m x && ", "true"),
    "git x16k": fill("git "),
    "git x16k + backslash": fill("git ", "a\\b"),
    "a; x32k": fill("a;"),
    "a; x32k + backslash": fill("a;", "a\\b"),
    "git; x16k": fill("git;"),
    "sh -c bundle": fill("sh -c 'git x';"),
    "sh -c bundle + backslash": fill("sh -c 'git a\\b';"),
    "redirect protected": fill("> .claude/settings.json ;"),
    "redirect plain": fill("> x ;"),
    "heredoc": fill("cat <<EOF\nx\nEOF\n"),
    "git -c a=b": fill("git -c a=b "),
    "git config hooksPath": fill("git config core.hooksPath x ;"),
}
for module in ["claq.hooks.block_no_verify", "claq.hooks.bash_config_protection", "claq.hooks.pre_bash_commit_quality"]:
    for name, cmd in cases.items():
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(REPO)}).encode()
        row = []
        for mode in ("nolimit", "limit"):
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "CLAQ_HOME": sys.argv[1]}
            t = time.monotonic()
            r = subprocess.run([sys.executable, "-c", RUNNER, mode, module], input=payload, capture_output=True, env=env, timeout=600, cwd=REPO)
            row.append(f"{mode} rc={r.returncode} {time.monotonic() - t:6.2f}s")
        print(f"{module.rsplit('.', 1)[1]:24} {name:26} bytes={len(cmd.encode()):6} tokens~{len(cmd.split()):6} | " + " | ".join(row))
