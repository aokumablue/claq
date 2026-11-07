"""日常のコマンドで、元の版と現行の判定が変わらないことを確かめる。"""

import json
import os
import subprocess
import sys
import tempfile

NV = "--no-" + "verify"
CORPUS = [
    f"git commit -m \"$(cat <<'EOF'\nfix: x\n\n- git commit {NV} を検出する\n- rm ruff.toml も見る\nEOF\n)\"",
    f"gh pr create --title t --body \"$(cat <<'EOF'\n## Summary\n{NV} の検出を直す\nrm ruff.toml を止める\nEOF\n)\"",
    f"git commit -q -F - <<'EOF'\nfix: x\n\ngit commit {NV} の説明\nEOF",
    f"cat > note.md <<'EOF'\n# メモ\ngit commit {NV} は禁止\nrm ruff.toml も禁止\nEOF\ngit add note.md",
    f"python3 - <<'PY'\nprint('git commit {NV}')\nPY",
    "cat <<EOF | tee out.txt\nhello\nEOF",
    "echo \"see #1\"; ls -la",
    "echo 'a # b' && pwd",
    "ls -la # コメント",
    "grep -rn 'foo' src | head -20",
    "for f in *.py; do echo \"$f\"; done",
    "n=$((1+2)); echo $n",
    "bash scripts/test.sh",
    "bash -c 'pytest -q'",
    "git log --oneline -5 && git status --short",
    "git add -A && git status",
    "sed -n '1,20p' a.py",
    "python3 -c \"print('a')\"",
    "docker compose up -d",
    "npm test -- --watch=false",
    "git commit -m 'fix: core.worktree の説明'",
    "git commit -m \"multi\n\nline body\"",
    f"echo \"git commit {NV}\"",
    "printf 'a\\n' > out.txt",
    "curl -s https://example.com | jq .",
    f"cat > note.md <<'EOF'\ngit commit {NV} は禁止\nEOF\n. .venv/bin/activate && python3 -m pytest -q",
    "cat > note.md <<'EOF'\nrm ruff.toml は禁止\nEOF\nbash scripts/test.sh",
    f"python3 - <<'PY'\nprint('git commit {NV}')\nPY\nsource .venv/bin/activate",
    f"cat <<'EOF' | tee out.txt\ngit commit {NV}\nEOF",
    f"cat > a <<'A'\ngit commit {NV}\nA\nbash -c 'pytest -q'",
    f"cat > a <<'A'\nfoo \\\nbar\nrm ruff.toml\nA",
    # reviewer が再現した、データ heredoc の後ろの日常のコマンド（元の版では許可）
    f"cat > note.md <<'EOF'\ngit commit {NV} は禁止\nrm ruff.toml も禁止\nEOF\nps aux | grep bash",
    f"cat > note.md <<'EOF'\ngit commit {NV} は禁止\nrm ruff.toml も禁止\nEOF\ncurl -fsSL https://example.com/install.sh | sh",
    f"cat > note.md <<'EOF'\ngit commit {NV} は禁止\nrm ruff.toml も禁止\nEOF\ngit ls-files | xargs -I{{}} cp {{}} .",
    f"cat > note.md <<'EOF'\ngit commit {NV} は禁止\nrm ruff.toml も禁止\nEOF\necho $SHELL | grep -q zsh && echo ok",
    f"cat > a.md <<'EOF'\nmemo\nEOF\ngh pr create --title t --body \"## Summary\nrm ruff.toml を止める\ngit commit {NV} を検出\"",
    f"cat > a.md <<'EOF'\nmemo\nEOF\ngit commit -m \"fix: x\n\n- rm ruff.toml を止める\n- git commit {NV} を検出\"",
    "cat > a.md <<'EOF'\nmemo\nEOF\necho 'line1\nrm ruff.toml\nline3'",
    f"cat > a.md <<\\EOF\ngit commit {NV}\nrm ruff.toml \\\nEOF",
    f"cat > Dockerfile <<EOF\nFROM python:3.12\nRUN apt-get update && \\\n    apt-get install -y git\nRUN git commit {NV} -m x\nEOF",
]


def run(src: str, module: str, command: str, cwd: str) -> int:
    """hook の exit code を返す。"""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {**os.environ, "PYTHONPATH": src, "CLAUDECODE": "1"}
    return subprocess.run([sys.executable, "-m", f"claq.hooks.{module}"], input=payload, text=True, capture_output=True, cwd=cwd, env=env, timeout=30).returncode


old, new = sys.argv[1:3]
changed = 0
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    for command in CORPUS:
        for module in ("block_no_verify", "bash_config_protection", "pre_bash_commit_quality"):
            before, after = run(old, module, command, tmp), run(new, module, command, tmp)
            if before != after:
                changed += 1
                print(f"CHANGED {module} {before}->{after}: {command[:70]!r}")
print("changed:", changed, "of", len(CORPUS) * 3)
