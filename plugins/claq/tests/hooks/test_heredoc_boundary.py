"""3 つの保護フックが heredoc 本文へ与える分類を横断で固定する。

正規化の適用点はフックごとに異なる（`has_bypass_flag` / `find_protected_write` /
`evaluate` のループ）。片側だけ変更されたときのドリフトを 1 ファイルで検出する。

実測の出発点: `cat > note.md <<'EOF'` の本文に保護対象の語を書いただけで
3 フックすべてが exit 2 になった（v0.9.44）。一方 `bash <<'EOF'` の本文は
実際に実行されるため exit 2 のままでなければならない。両者を同じ表で固定する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = PLUGIN_ROOT / "src" / "claq" / "launcher.py"

# 本文がデータとして扱われる形。すべて exit 0 になること。
_DATA_HEREDOC_LINES = (
    "cat > note.md <<'EOF'",
    "tee note.md <<'EOF'",
    "python3 - <<'EOF'",
    "jq -f - <<'EOF'",
)

# 本文が実行されうる形。exit 2 のまま維持されること。
_EXECUTED_HEREDOC_LINES = (
    "bash <<'EOF'",
    "cat <<'EOF' | bash",
    "sh <<'EOF'",
)

# フック → (deny されるべき本文, 陰性対照として通る本文)。
_HOOK_BODIES = {
    "claq.hooks.block_no_verify": "git commit --no-verify -m x",
    "claq.hooks.bash_config_protection": "printf x > ruff.toml",
}

# 「幻の演算子」（C-1）。`<<` がコメント内・クォート内・`\` エスケープ後にあると
# heredoc は開始せず、続く行は**実行されるコマンド**である。それを本文として
# 捨てていた頃は 3 フックが揃って exit 0 になった（実測）。
#
# deny 側だけを並べても意味がない — 「heredoc 本文を一切剥がさない」実装でも
# 全行が緑になるため。剥がすべき形（本物の heredoc）を allow 側の対照として
# 同じ表へ置き、両側で挟む。
_PHANTOM_OPERATOR_CASES = (
    ("コメント内の <<", "# <<EOF\n{body}\nEOF", 2),
    # 行頭以外のコメントは `ls -a` にする。`ls .` だと `.` が
    # `_BODY_EXECUTING_BUILTINS` に当たって修正前でも deny になり、この行が
    # 何も固定しなくなる（実測で確認）。
    ("行頭以外のコメント内の <<", "ls -a # <<EOF\n{body}\nEOF", 2),
    ("ダブルクォート内の <<", 'echo "<<EOF"\n{body}\nEOF', 2),
    ("シングルクォート内の <<", "echo '<<EOF'\n{body}\nEOF", 2),
    ("エスケープされた <<", "echo \\<<EOF\n{body}\nEOF", 2),
    ("行をまたいで開いたクォート内の <<", 'echo "open\n<<EOF\n{body}\nEOF', 2),
    ("本文の # とアポストロフィが状態を汚さない", "cat > note.md <<'EOF'\ndon't # note\nEOF\n{body}", 2),
    # ここから allow 側の対照（本物の heredoc の本文は保護対象ではない）。
    ("本物の heredoc", "cat > note.md <<'EOF'\n{body}\nEOF", 0),
    ("本文に << を含む本物の heredoc", "cat > note.md <<'OUTER'\n{body}\n<<INNER\nOUTER", 0),
    ("行末コメント付きの本物の heredoc", "cat > note.md <<'EOF' # note\n{body}\nEOF", 0),
)

# 幻の演算子の表を流す 3 フックと、それぞれが deny すべき本文。
_PHANTOM_HOOK_BODIES = {
    "claq.hooks.block_no_verify": "git commit --no-verify -m x",
    "claq.hooks.bash_config_protection": "printf x > ruff.toml",
    "claq.hooks.pre_bash_commit_quality": "git add . && git commit -m x",
}


def _run(hook: str, command: str, tmp_home: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """リポジトリ内 launcher で hook を実行する。

    Args:
        hook: dotted module name。
        command: フックへ渡す Bash コマンド文字列。
        tmp_home: 一時 HOME（実 ~/.claq を触らない）。
        cwd: 実行ディレクトリ（リポジトリスコープ判定に使われる）。

    Returns:
        CompletedProcess。
    """
    env = {
        "HOME": str(tmp_home),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
    }
    return subprocess.run(
        [sys.executable, str(LAUNCHER), hook],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=30,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """保護対象ファイル判定の基準になる使い捨て git リポジトリ。"""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True, capture_output=True)
    return root


@pytest.mark.parametrize("hook", sorted(_HOOK_BODIES))
@pytest.mark.parametrize("operator_line", _DATA_HEREDOC_LINES)
def test_data_heredoc_body_is_not_command_text(
    hook: str, operator_line: str, tmp_path: Path, repo: Path
) -> None:
    """データ本文に保護対象の語が現れても通す。"""
    command = f"{operator_line}\n{_HOOK_BODIES[hook]}\nEOF"

    result = _run(hook, command, tmp_path, repo)

    assert result.returncode == 0, f"{hook}: データ heredoc 本文で exit {result.returncode} err={result.stderr[:200]}"


@pytest.mark.parametrize("hook", sorted(_HOOK_BODIES))
@pytest.mark.parametrize("operator_line", _EXECUTED_HEREDOC_LINES)
def test_executed_heredoc_body_stays_blocked(
    hook: str, operator_line: str, tmp_path: Path, repo: Path
) -> None:
    """本文が実行される形は従来どおりブロックし続ける（誤通過を作らない）。"""
    command = f"{operator_line}\n{_HOOK_BODIES[hook]}\nEOF"

    result = _run(hook, command, tmp_path, repo)

    assert result.returncode == 2, f"{hook}: 実行される heredoc 本文が exit {result.returncode} で素通りした"


@pytest.mark.parametrize("operator_line", _DATA_HEREDOC_LINES)
def test_commit_quality_does_not_scan_for_data_heredoc_body(operator_line: str, tmp_path: Path, repo: Path) -> None:
    """本文の `git commit` で staged 検査へ入らない。

    実測 FP は「staged に問題があればブロック」という index 依存の現象なので、
    exit code だけでなく品質スキャンのログが出ないことで判定する。
    """
    command = f"{operator_line}\ngit commit の品質ゲートについて書く\nEOF"

    result = _run("claq.hooks.pre_bash_commit_quality", command, tmp_path, repo)

    assert result.returncode == 0
    assert "Checking" not in result.stderr, f"データ本文で品質スキャンが走った: {result.stderr[:200]}"


@pytest.mark.parametrize("operator_line", _EXECUTED_HEREDOC_LINES)
def test_commit_quality_still_scans_executed_heredoc_body(operator_line: str, tmp_path: Path, repo: Path) -> None:
    """本文が実行される形では従来どおり commit として扱う。"""
    command = f"{operator_line}\ngit add . && git commit -m x\nEOF"

    result = _run("claq.hooks.pre_bash_commit_quality", command, tmp_path, repo)

    assert result.returncode == 2


@pytest.mark.parametrize("hook", sorted(_PHANTOM_HOOK_BODIES))
@pytest.mark.parametrize(("label", "template", "expected"), _PHANTOM_OPERATOR_CASES)
def test_phantom_heredoc_operator(
    hook: str, label: str, template: str, expected: int, tmp_path: Path, repo: Path
) -> None:
    """heredoc を開始しない `<<` で後続行が本文として捨てられないこと。

    deny 側（幻の演算子）と allow 側（本物の heredoc）を同じ表で挟む。片側だけ
    だと「常に剥がす」実装と「常に剥がさない」実装のどちらかが緑で通る。
    """
    command = template.format(body=_PHANTOM_HOOK_BODIES[hook])

    result = _run(hook, command, tmp_path, repo)

    assert result.returncode == expected, (
        f"{hook}: {label} が exit {result.returncode}（期待 {expected}） err={result.stderr[:200]}"
    )
