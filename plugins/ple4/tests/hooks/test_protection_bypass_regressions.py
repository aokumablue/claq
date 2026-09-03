"""保護フックの既知バイパスを block / allow の表で固定する。

release-verify v0.9.48 の実起動で、`Security Guardrails` が満点のまま
`block_no_verify` / `bash_config_protection` / `config_protection` の 7 経路が
素通りしていた。`harness_audit` はフックの**存在**を測るだけで実効性を測らない
（`skills/maintain/SKILL.md` のステップ6 が明記）ため、実効性の回帰はこのファイルの
block / allow 一覧が担う。

`allow` 側を同じ表に置くのは、fail-closed へ寄せた判定が通常操作を巻き込んでいない
ことを同時に固定するため。片側だけ書くと「全部ブロックする」実装でも緑になる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ple4.hooks import bash_config_protection, block_no_verify, config_protection

# 検査対象フック名 → main() を持つモジュール。
_HOOKS = {
    "block_no_verify": block_no_verify,
    "bash_config_protection": bash_config_protection,
    "config_protection": config_protection,
}


def _bash(command: str) -> dict[str, Any]:
    """Bash ツール呼び出しの payload を組み立てる。

    Args:
        command: フックへ渡すシェルコマンド文字列。

    Returns:
        フック stdin へ渡す payload dict。
    """
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _write(file_path: Any) -> dict[str, Any]:
    """Write ツール呼び出しの payload を組み立てる。

    Args:
        file_path: `file_path` フィールドに載せる値（文字列でなくてもよい）。

    Returns:
        フック stdin へ渡す payload dict。
    """
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": "x"}}


# heredoc 本文を実行するシンク。演算子行にこれらが現れる場合、本文はデータでは
# ないので剥がしてはならない（ADR-0017 の除去しない条件 1 と同じ理由）。
_BYPASS_HEREDOC = ". /dev/stdin <<'EOF'\n{body}\nEOF"
_SOURCE_HEREDOC = "source /dev/stdin <<'EOF'\n{body}\nEOF"
_EVAL_HEREDOC = "eval $(cat <<'EOF'\n{body}\nEOF\n)"
# クォートで包んだコマンド置換（``eval "$(cat <<'EOF' ...)"``）は 1 トークンに
# なるため検出できない。ADR-0002 が「コマンド置換・変数展開」を非目標として
# 明記している範囲であり、本ファイルの block 一覧には載せない。
# 本文がデータのまま終わる形。ADR-0017 のとおり allow でなければならない。
_DATA_HEREDOC = "cat > note.md <<'EOF'\n{body}\nEOF"

_NO_VERIFY = "git commit --no-verify -m x"
_PROTECTED_WRITE = "printf x > ruff.toml"

# (ラベル, フック名, payload) — すべて exit 2 でなければならない。
_BLOCKED_CASES = [
    # S-1: `#` の作用域は行末までであり入力末尾までではない。
    ("S-1 行コメント後の 2 行目", "block_no_verify", _bash(f"git status #\n{_NO_VERIFY}")),
    ("S-1 行コメント後の書き込み", "bash_config_protection", _bash(f"echo ok #\n{_PROTECTED_WRITE}")),
    ("S-1 語中の #", "block_no_verify", _bash(f"echo a#b && {_NO_VERIFY}")),
    ("S-1 引用符内の #", "block_no_verify", _bash('git commit -m "fix #1" --no-verify')),
    # S-3: セグメント内の書き込み先は 1 件ではない。
    ("S-3 repo 外の囮が先頭", "bash_config_protection", _bash("rm -f /tmp/ruff.toml ruff.toml")),
    ("S-3 リダイレクト 2 連", "bash_config_protection", _bash("printf x > notes.txt > ruff.toml")),
    # S-4: `cd` があると cwd 基準の repo スコープ判定が信用できない。
    ("S-4 cd 後の相対書き込み", "bash_config_protection", _bash("cd sub && printf x > ../ruff.toml")),
    ("S-4 cd の解決先不明", "bash_config_protection", _bash("cd - && printf x > ruff.toml")),
    # S-5: `-c` は結合短フラグでも成立する。
    ("S-5 bash -lc", "block_no_verify", _bash(f"bash -lc '{_NO_VERIFY}'")),
    ("S-5 bash -lc 書き込み", "bash_config_protection", _bash(f"bash -lc '{_PROTECTED_WRITE}'")),
    ("S-5b bash -c 書き込み", "bash_config_protection", _bash(f"bash -c '{_PROTECTED_WRITE}'")),
    ("S-5b sh -c 書き込み", "bash_config_protection", _bash(f"sh -c '{_PROTECTED_WRITE}'")),
    # S-6: `.` / `source` / `eval` は heredoc 本文を実行する。
    ("S-6 . /dev/stdin", "block_no_verify", _bash(_BYPASS_HEREDOC.format(body=_NO_VERIFY))),
    ("S-6 source /dev/stdin", "block_no_verify", _bash(_SOURCE_HEREDOC.format(body=_NO_VERIFY))),
    ("S-6 eval $(cat)", "block_no_verify", _bash(_EVAL_HEREDOC.format(body=_NO_VERIFY))),
    (
        "S-6 . /dev/stdin 書き込み",
        "bash_config_protection",
        _bash(_BYPASS_HEREDOC.format(body=_PROTECTED_WRITE)),
    ),
    # S-7: フィールド値は文字列とは限らない。
    (
        "S-7 command が list",
        "block_no_verify",
        {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", _NO_VERIFY]}},
    ),
    ("S-7 file_path が list", "config_protection", _write(["ruff.toml"])),
    (
        "S-7 command が list（書き込み）",
        "bash_config_protection",
        {"tool_name": "Bash", "tool_input": {"command": ["sh", "-c", _PROTECTED_WRITE]}},
    ),
    # S-8: ツール名を特定できない payload を「対象外」と読み替えない。
    (
        "S-8 tool_name が非文字列",
        "config_protection",
        {"tool_name": 123, "tool_input": {"file_path": "ruff.toml", "content": "x"}},
    ),
    (
        "S-8 tool_name キーが別名",
        "config_protection",
        {"tool": "Write", "tool_input": {"file_path": "ruff.toml", "content": "x"}},
    ),
    (
        "S-8 未知のツール名",
        "config_protection",
        {"tool_name": "mcp__fs__write", "tool_input": {"file_path": "ruff.toml", "content": "x"}},
    ),
    (
        "S-8 tool_name 欠落（Bash）",
        "bash_config_protection",
        {"tool_input": {"command": _PROTECTED_WRITE}},
    ),
    # 陽性対照（修正前から exit 2。fail-closed 化で失われていないこと）。
    ("対照 コメント無しの 2 行目", "block_no_verify", _bash(f"git status\n{_NO_VERIFY}")),
    ("対照 bash -c", "block_no_verify", _bash(f"bash -c '{_NO_VERIFY}'")),
    ("対照 素の書き込み", "bash_config_protection", _bash(_PROTECTED_WRITE)),
    ("対照 Write で保護対象", "config_protection", _write("ruff.toml")),
]

# (ラベル, フック名, payload) — すべて exit 0 でなければならない。
# fail-closed へ寄せた判定が通常操作を巻き込んでいないことの担保。
_ALLOWED_CASES = [
    ("通常の commit", "block_no_verify", _bash("git commit -m x")),
    ("コメントは剥がされる", "block_no_verify", _bash(f"git status # {_NO_VERIFY}")),
    ("引用された散文", "block_no_verify", _bash(f"echo '# {_NO_VERIFY}'")),
    ("語中の # は語の一部", "block_no_verify", _bash('echo "a#b"')),
    ("データ heredoc", "block_no_verify", _bash(_DATA_HEREDOC.format(body=_NO_VERIFY))),
    ("保護対象でない書き込み", "bash_config_protection", _bash("printf x > notes.txt")),
    ("保護対象の読み取り", "bash_config_protection", _bash("cat ruff.toml")),
    ("repo 外への書き込み", "bash_config_protection", _bash("printf x > /tmp/ruff.toml")),
    ("cd はあるが保護対象なし", "bash_config_protection", _bash("cd sub && printf x > notes.txt")),
    ("ラッパー内も保護対象なし", "bash_config_protection", _bash("bash -lc 'printf x > notes.txt'")),
    ("データ heredoc（書き込み）", "bash_config_protection", _bash(_DATA_HEREDOC.format(body=_PROTECTED_WRITE))),
    ("Read は書き込みではない", "config_protection", {"tool_name": "Read", "tool_input": {"file_path": "ruff.toml"}}),
    ("Grep は書き込みではない", "config_protection", {"tool_name": "Grep", "tool_input": {"file_path": "ruff.toml"}}),
    ("保護対象でない Write", "config_protection", _write("notes.txt")),
]


@pytest.fixture(autouse=True)
def _repo_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """cwd と repo ルートを使い捨てディレクトリへ固定する。

    `bash_config_protection` の repo スコープ判定（A-06）が実行環境の git
    リポジトリに依存しないようにする。`cd sub` の到達先も用意する。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。
        tmp_path: pytest の一時ディレクトリ。

    Returns:
        repo ルートとして固定した一時ディレクトリ。
    """
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)
    return tmp_path


def _run(hook: str, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> int:
    """payload を stdin として与えてフックの main() を実行する。

    Args:
        hook: `_HOOKS` のキー。
        payload: フック stdin へ渡す payload。
        monkeypatch: pytest の monkeypatch フィクスチャ。

    Returns:
        main() の終了コード。
    """
    module = _HOOKS[hook]
    monkeypatch.setattr(module, "read_raw_stdin_with_truncation", lambda: (json.dumps(payload), False))
    return module.main()


@pytest.mark.parametrize(("label", "hook", "payload"), _BLOCKED_CASES, ids=[case[0] for case in _BLOCKED_CASES])
def test_known_bypass_is_blocked(
    label: str, hook: str, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """既知バイパス経路が exit 2 でブロックされること。

    Args:
        label: ケースの識別ラベル（失敗時の可読性のため）。
        hook: 対象フック名。
        payload: フックへ渡す payload。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run(hook, payload, monkeypatch) == 2, label


@pytest.mark.parametrize(("label", "hook", "payload"), _ALLOWED_CASES, ids=[case[0] for case in _ALLOWED_CASES])
def test_ordinary_command_is_allowed(
    label: str, hook: str, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """通常操作が fail-closed 化に巻き込まれず exit 0 のままであること。

    Args:
        label: ケースの識別ラベル（失敗時の可読性のため）。
        hook: 対象フック名。
        payload: フックへ渡す payload。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run(hook, payload, monkeypatch) == 0, label
