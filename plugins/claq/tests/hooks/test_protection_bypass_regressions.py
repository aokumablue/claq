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

import ast
import importlib
import json
import pkgutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import claq.hooks
from claq.hooks import (
    bash_config_protection,
    block_no_verify,
    commit_quality_scanner,
    config_protection,
    hook_common,
    pre_bash_commit_quality,
)

# 検査対象フック名 → main() を持つモジュール。
_HOOKS = {
    "block_no_verify": block_no_verify,
    "bash_config_protection": bash_config_protection,
    "config_protection": config_protection,
    "pre_bash_commit_quality": pre_bash_commit_quality,
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


def _write_content(file_path: str, content: str) -> dict[str, Any]:
    """内容を指定した Write ツール呼び出しの payload を組み立てる。

    条件付き保護（`CONDITIONALLY_PROTECTED_FILES`）は書き込み内容に lint 兆候が
    無ければ allow なので、`_write` の固定内容では検査軸を動かせない。

    Args:
        file_path: `file_path` フィールドに載せる値。
        content: `content` フィールドに載せる書き込み内容。

    Returns:
        フック stdin へ渡す payload dict。
    """
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}


# heredoc 本文を実行するシンク。演算子行にこれらが現れる場合、本文はデータでは
# ないので剥がしてはならない（docs/adr/shell-analysis-boundary.md の除去しない条件 1 と同じ理由）。
_BYPASS_HEREDOC = ". /dev/stdin <<'EOF'\n{body}\nEOF"
_SOURCE_HEREDOC = "source /dev/stdin <<'EOF'\n{body}\nEOF"
# なお、クォートで包んだコマンド置換（``eval "$(cat <<'EOF' ...)"``）は全体が
# 1 トークンになるため検出できない。docs/adr/shell-analysis-boundary.md が「コマンド置換・変数展開」を
# 非目標として明記している範囲であり、下の block 一覧には載せない。
_EVAL_HEREDOC = "eval $(cat <<'EOF'\n{body}\nEOF\n)"

# 本文がデータのまま終わる形。docs/adr/shell-analysis-boundary.md のとおり allow でなければならない。
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
    # C-2: 保護対象の照合が大小を区別しない（APFS / NTFS は既定で大小無視なので
    # `Ruff.toml` への書込みは実体 `ruff.toml` に当たる）。
    ("C-2 Write 大文字混在", "config_protection", _write("Ruff.toml")),
    ("C-2 Write 全大文字", "config_protection", _write("RUFF.TOML")),
    ("C-2 rm 大文字混在", "bash_config_protection", _bash("rm Ruff.toml")),
    ("C-2 リダイレクト先が大文字", "bash_config_protection", _bash("printf x > RUFF.TOML")),
    ("C-2 保護 path の大文字", "config_protection", _write(".Git/Hooks/pre-commit")),
    ("C-2 保護 path の大文字（Bash）", "bash_config_protection", _bash("rm .GIT/HOOKS/pre-commit")),
    (
        "C-2 条件付き保護の大文字",
        "config_protection",
        _write_content("PyProject.toml", "[tool.ruff]\nignore = ['E501']"),
    ),
    (
        "C-2 package.json 専用照合の大文字",
        "config_protection",
        _write_content("Package.json", '{"eslintConfig": {}}'),
    ),
    ("C-2 tox.ini 専用照合の大文字", "config_protection", _write_content("TOX.ini", "commands = true")),
    # H-5: sed の GNU 長形式（短縮形を含む）と実行 wrapper。
    ("H-5 sed --in-place", "bash_config_protection", _bash("sed --in-place s/x/y/ ruff.toml")),
    ("H-5 sed --in-place=.bak", "bash_config_protection", _bash("sed --in-place=.bak s/x/y/ ruff.toml")),
    ("H-5 sed 長形式の短縮 --i", "bash_config_protection", _bash("sed --i s/x/y/ ruff.toml")),
    ("H-5 sed -ni 結合短形式", "bash_config_protection", _bash("sed -ni s/x/y/ ruff.toml")),
    ("H-5 timeout wrapper", "bash_config_protection", _bash("timeout 5 rm ruff.toml")),
    ("H-5 timeout 単位付き秒数", "bash_config_protection", _bash("timeout 1.5s rm ruff.toml")),
    ("H-5 nohup wrapper", "bash_config_protection", _bash("nohup rm ruff.toml")),
    ("H-5 nice wrapper", "bash_config_protection", _bash("nice rm ruff.toml")),
    ("H-5 nice -n の数値", "bash_config_protection", _bash("nice -n 10 rm ruff.toml")),
    ("H-5 stdbuf wrapper", "bash_config_protection", _bash("stdbuf -o0 rm ruff.toml")),
    ("H-5 wrapper 多重", "bash_config_protection", _bash("nohup nice timeout 5 rm ruff.toml")),
    # H-6: commit 前 mutation ガードの語彙が兄弟の正規化から取り残されていた。
    # scan は変更前の作業ツリーを読むため、取りこぼしは未検査コミットになる。
    (
        "H-6 大文字の cp",
        "pre_bash_commit_quality",
        _bash("CP /tmp/evil.py app.py && git commit -m 'fix: x'"),
    ),
    ("H-6 cp.exe", "pre_bash_commit_quality", _bash("cp.exe /tmp/evil.py app.py && git commit -m 'fix: x'")),
    ("H-6 大文字の rm", "pre_bash_commit_quality", _bash("Rm old.py && git commit -m 'fix: x'")),
    ("H-6 大文字の tee", "pre_bash_commit_quality", _bash("echo x | TEE app.py && git commit -m 'fix: x'")),
    (
        "H-6 sed 長形式の in-place",
        "pre_bash_commit_quality",
        _bash("sed --in-place s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    (
        "H-6 perl の結合短形式",
        "pre_bash_commit_quality",
        _bash("perl -0pi -e s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    # M-8: フラグ無しでファイルを書き換えるエディタ。`ed` はフラグを要求する
    # `_INPLACE_EDIT_EXECUTABLES` に置かれていたため、``ed app.py && git commit``
    # が mutation-before-commit ガードを素通りしていた（実測 exit 0）。
    ("M-8 ed の commit 前編集", "pre_bash_commit_quality", _bash("ed app.py && git commit -m 'fix: x'")),
    ("M-8 ex の commit 前編集", "pre_bash_commit_quality", _bash("ex app.py && git commit -m 'fix: x'")),
    ("M-8 red の commit 前編集", "pre_bash_commit_quality", _bash("red app.py && git commit -m 'fix: x'")),
    (
        "M-8 sponge の commit 前編集",
        "pre_bash_commit_quality",
        _bash("echo x | sponge app.py && git commit -m 'fix: x'"),
    ),
    ("M-8 ED 大文字", "pre_bash_commit_quality", _bash("ED app.py && git commit -m 'fix: x'")),
    # 同じ語彙の穴は保護 config 側にも開いていた（``ed ruff.toml`` が exit 0）。
    ("M-8 ed で保護 config", "bash_config_protection", _bash("ed ruff.toml")),
    ("M-8 ex で保護 config", "bash_config_protection", _bash("ex .eslintrc")),
    ("M-8 sponge で保護 config", "bash_config_protection", _bash("echo x | sponge ruff.toml")),
    ("M-8 wrapper 越しの ed", "bash_config_protection", _bash("timeout 5 ed ruff.toml")),
    # M-9: `touch` は内容を変えないが、保護対象を**空で新規作成**できる。空の
    # `.eslintrc` は ESLint の上位カスケード探索を止めるため実質的な無効化になる。
    # 修正前は保護対象 36 ファイル全てで allow だった（実測 exit 0）。
    ("M-9 touch で保護 config", "bash_config_protection", _bash("touch ruff.toml")),
    ("M-9 touch の大小混在", "bash_config_protection", _bash("touch .Eslintrc")),
    ("M-9 touch の値付きオプション", "bash_config_protection", _bash("touch -t 202601010000 ruff.toml")),
    ("M-9 wrapper 越しの touch", "bash_config_protection", _bash("timeout 5 touch ruff.toml")),
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
    # --- 上の block 追加と対になる陰性対照 ---
    # C-2: 大小無視は「保護対象の綴り違い」だけに効き、無関係なファイルは巻き込まない。
    ("C-2 対 大文字混在の無関係 Write", "config_protection", _write("App.py")),
    ("C-2 対 大文字混在の無関係 rm", "bash_config_protection", _bash("rm App.py")),
    ("C-2 対 名前が似た別ファイル", "config_protection", _write("Ruff.toml.bak")),
    (
        "C-2 対 条件付き保護の非 lint 変更",
        "config_protection",
        _write_content("PyProject.toml", '[project]\nversion = "1.2.3"'),
    ),
    # H-5: in-place でない sed と、実行位置に無い wrapper 語は allow のまま。
    ("H-5 対 sed --expression", "bash_config_protection", _bash("sed --expression s/x/y/ app.py")),
    ("H-5 対 保護対象への非 in-place sed", "bash_config_protection", _bash("sed --expression s/x/y/ ruff.toml")),
    # 区切りの `--` は長形式ではないので in-place ではない（名前部分が空）。
    ("H-5 対 区切りの --", "bash_config_protection", _bash("sed -- s/x/y/ ruff.toml")),
    # M-01: wrapper 語彙を足しても、非実行位置の言及は実行位置にならない。
    ("H-5 対 echo tee 言及", "bash_config_protection", _bash("echo tee pyproject.toml")),
    ("H-5 対 echo の中の wrapper 列", "bash_config_protection", _bash("echo timeout 5 rm ruff.toml")),
    # wrapper の先が読み取り専用なら書き込み先は現れない。
    ("H-5 対 timeout 経由の cat", "bash_config_protection", _bash("timeout 5 cat ruff.toml")),
    # H-6: 正規化を足しても、変更しないコマンドは mutation とみなさない。
    ("H-6 対 大文字の cat", "pre_bash_commit_quality", _bash("Cat app.py && git commit -m 'fix: x'")),
    ("H-6 対 単独 commit", "pre_bash_commit_quality", _bash("git commit -m 'fix: x'")),
    (
        "H-6 対 in-place でない sed",
        "pre_bash_commit_quality",
        _bash("sed --expression s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    ("H-6 対 commit 無しの mutation", "pre_bash_commit_quality", _bash("CP /tmp/evil.py app.py")),
    # M-8: 常時 mutation 分類を足しても、非実行位置の言及・保護対象でない引数・
    # フラグを要求する側の語彙（`sed` は `-i` 無しなら標準出力）は allow のまま。
    ("M-8 対 commit 無しの ed", "pre_bash_commit_quality", _bash("ed app.py")),
    (
        "M-8 対 in-place でない sed",
        "pre_bash_commit_quality",
        _bash("sed s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    ("M-8 対 保護対象でない ed", "bash_config_protection", _bash("ed app.py")),
    ("M-8 対 非実行位置の ed", "bash_config_protection", _bash("echo ed ruff.toml")),
    ("M-8 対 保護対象の読み取り（ex 名の別語）", "bash_config_protection", _bash("grep -rn ex ruff.toml")),
    # M-9: `touch` を書込み verb にしても、保護対象でないファイル・repo 外・
    # 非実行位置は allow のまま。「全部ブロックする」実装をここで落とす。
    ("M-9 対 touch app.py", "bash_config_protection", _bash("touch app.py")),
    ("M-9 対 touch README.md", "bash_config_protection", _bash("touch README.md")),
    ("M-9 対 repo 外の touch", "bash_config_protection", _bash("touch /tmp/ruff.toml")),
    ("M-9 対 非実行位置の touch", "bash_config_protection", _bash("echo touch ruff.toml")),
]


# ハーネスごとの payload 形状（`lib/harness.py` の `_TOOL_NAME_MAP` /
# `INPUT_CONTAINER_KEYS` に対応）。ツール名を特定できない payload を検査側へ倒す
# 変更（S-8）が、Claude Code 以外のハーネスの通常操作を巻き込んでいないことを
# 固定する。ここが緑でないと「Claude Code では動くが Copilot では全部ブロック」
# という片側だけの退行が出荷される。
_HARNESS_CASES = [
    ("copilot bash 危険", "bash_config_protection", {"tool_name": "bash", "toolArgs": json.dumps({"command": _PROTECTED_WRITE})}, 2),
    ("copilot bash 通常", "bash_config_protection", {"tool_name": "bash", "toolArgs": json.dumps({"command": "printf x > notes.txt"})}, 0),
    ("copilot write 危険", "config_protection", {"tool_name": "write", "toolArgs": json.dumps({"file_path": "ruff.toml", "content": "x"})}, 2),
    ("copilot read 通常", "config_protection", {"tool_name": "read", "toolArgs": json.dumps({"file_path": "ruff.toml"})}, 0),
    ("copilot bnv 危険", "block_no_verify", {"tool_name": "bash", "toolArgs": json.dumps({"command": _NO_VERIFY})}, 2),
    ("copilot bnv 通常", "block_no_verify", {"tool_name": "bash", "toolArgs": json.dumps({"command": "git commit -m x"})}, 0),
    ("grok 端末 危険", "bash_config_protection", {"tool_name": "run_terminal_command", "toolInput": {"command": _PROTECTED_WRITE}}, 2),
    ("grok 端末 通常", "bash_config_protection", {"tool_name": "run_terminal_command", "toolInput": {"command": "ls -la"}}, 0),
    ("grok 置換 危険", "config_protection", {"tool_name": "search_replace", "toolInput": {"file_path": "ruff.toml"}}, 2),
    ("grok 読取 通常", "config_protection", {"tool_name": "read_file", "toolInput": {"file_path": "ruff.toml"}}, 0),
    ("grok 一覧 通常", "config_protection", {"tool_name": "list_dir", "toolInput": {"file_path": "ruff.toml"}}, 0),
    ("codex パッチ 危険", "config_protection", {"tool_name": "apply_patch", "tool_input": {"input": "*** Update File: ruff.toml\n@@\n-a\n+b"}}, 2),
    ("codex パッチ 通常", "config_protection", {"tool_name": "apply_patch", "tool_input": {"input": "*** Update File: notes.txt\n@@\n-a\n+b"}}, 0),
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
    # `pre_bash_commit_quality` は staged files を git へ問い合わせる。使い捨て
    # ディレクトリは git リポジトリではないため、素のままだと「staged を特定
    # できない」の fail-closed で全ケースが exit 2 になり、mutation ガードの
    # 判定軸がまったく効いていなくても表が緑になる。空 staged を与えて、
    # `_compound_commit_risk` の結論だけが exit code に出る状態にする。
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [])
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

    def _read() -> tuple[str, bool]:
        """stdin 読み取りの差し替え実装。

        Returns:
            (payload の JSON 文字列, 切り捨て無し) のタプル。
        """
        return json.dumps(payload), False

    module = _HOOKS[hook]
    # `pre_bash_commit_quality.main()` は `hook_common` から関数内 import する
    # ため、モジュール束縛の差し替えだけでは効かない。両方を差し替える。
    monkeypatch.setattr(hook_common, "read_raw_stdin_with_truncation", _read)
    if hasattr(module, "read_raw_stdin_with_truncation"):
        monkeypatch.setattr(module, "read_raw_stdin_with_truncation", _read)
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


@pytest.mark.parametrize(
    ("label", "hook", "payload", "expected"), _HARNESS_CASES, ids=[case[0] for case in _HARNESS_CASES]
)
def test_other_harness_payload_shapes(
    label: str, hook: str, payload: dict[str, Any], expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code 以外のハーネスの payload 形状でも判定が変わらないこと。

    ツール名を特定できない payload を検査側へ倒した（S-8）ため、Copilot CLI の
    lowercase runtime 名・Grok の固有名・Codex の apply_patch が「未知」と扱われて
    通常操作まで塞がれていないかを、危険側と通常側の両方で固定する。

    Args:
        label: ケースの識別ラベル（失敗時の可読性のため）。
        hook: 対象フック名。
        payload: フックへ渡す payload。
        expected: 期待する終了コード。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run(hook, payload, monkeypatch) == expected, label


# --------------------------------------------------------------------------
# 保護語彙の全数検査（H-11）
#
# 上の `_BLOCKED_CASES` / `_ALLOWED_CASES` は語彙の**サンプル**しか流していな
# かった。保護集合は複数行にまたがる 1 個の set リテラル（= 1 statement）なので、
# 要素を 1 個消しても実行行は変わらず `fail_under=100` では原理的に検出できない。
# 実測: `config_protection.py` から ".shellcheckrc" を消すと、スイート全緑のまま
# `Write .shellcheckrc` と `printf x > .shellcheckrc` の両経路が allow へ戻った。
#
# 検査は 2 層に分ける。片方だけでは穴が残るため、どちらも必須:
#
#   層1（列挙）: 集合そのものを parametrize の入力にし、全要素で deny を実測する。
#       「集合には載っているが実際には効いていない」要素を検出する。
#   層2（固定）: 集合の内容を完全一致で表明する。層1 だけだと、要素を削除した
#       瞬間にその parametrize ケース自体が消えるため**緑のまま**通ってしまう。
#       保護語彙の増減を人間の意識的な変更に限定する関門がこちら。
#
# 層2 は実装の定義をテストへ複製する形になるが、これは意図的である。保護語彙は
# 「壊れても誰も困らないから気付かれない」種類のデータで、外部に突き合わせ先を
# 持たない（version-up.sh のように抽出元がある場合はそちらを使う）。ここでは
# 集合の変更そのものをレビュー対象へ引き上げることが目的。
# --------------------------------------------------------------------------

# `config_protection.PROTECTED_FILES` の期待内容（層2）。
_EXPECTED_PROTECTED_FILES = frozenset({
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    "biome.json",
    "biome.jsonc",
    ".ruff.toml",
    "ruff.toml",
    ".shellcheckrc",
    ".stylelintrc",
    ".stylelintrc.json",
    ".stylelintrc.yml",
    ".stylelintrc.yaml",
    ".markdownlint.json",
    ".markdownlint.yaml",
    ".markdownlint.yml",
    ".markdownlintrc",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
})

# `config_protection.CONDITIONALLY_PROTECTED_FILES` の期待内容（層2）。
_EXPECTED_CONDITIONALLY_PROTECTED_FILES = frozenset({
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    # ホスト設定。保護フックを止めるキーだけを条件にする（`env` 全般ではない —
    # `update-config` skill の `DEBUG=true` や `.vscode/settings.json` の
    # `terminal.integrated.env.*` を巻き込むため）。
    "settings.json",
    "settings.local.json",
})

# `bash_config_protection._DIRECTORY_CHANGE_COMMANDS` の期待内容（層2）。
_EXPECTED_DIRECTORY_CHANGE_COMMANDS = frozenset({"cd", "pushd", "popd", "chdir"})

# `config_protection._LINT_SECTION_HEADERS` の期待内容（層2）。順序も含めて固定する
# （前方一致で判定するため、要素の順序は結果に影響しないが定義の差分を見やすくする）。
_EXPECTED_LINT_SECTION_HEADERS = (
    "[tool.ruff",
    "[tool.coverage",
    "[tool.pytest",
    "[flake8]",
    "[mypy]",
    "[pycodestyle]",
    "[testenv",
)

# `config_protection._LINT_KEYS` の期待内容（層2）。
_EXPECTED_LINT_KEYS = ("ignore", "select", "per-file-ignores", "exclude", "fail_under", "addopts")

# 保護語彙に**似ているが載っていない**名前。層1 の deny がファイル名を見ずに
# 一律 deny しているだけではないことを示す陰性対照（拡張子だけを変えてある）。
_NON_MEMBER_CONFIG_NAMES = (
    ".eslintrc.toml",
    "eslint.config.jsonc",
    ".prettierrc.toml",
    "prettier.config.ts",
    "biome.yaml",
    "ruff.yaml",
    ".shellcheckrc.json",
    ".stylelintrc.toml",
    ".markdownlint.toml",
    ".pre-commit-config.toml",
)

# 条件付き保護ファイルごとの (lint 兆候を含む内容, 含まない内容)。
# `package.json` だけは TOML/INI ではないため専用キー（`eslintConfig` / `prettier`）
# を signal に使う —— 同じ表へ一律 `[tool.ruff]` を流すと「実装のバイパス」ではなく
# 「テストの取り違え」で赤くなり、検出件数を汚す。
_CONDITIONAL_SIGNALS = {
    "pyproject.toml": ("[tool.ruff]\nignore = []\n", "[project]\nname = \"x\"\n"),
    "setup.cfg": ("[flake8]\nignore = E501\n", "[metadata]\nname = x\n"),
    "tox.ini": ("[testenv]\ncommands = pytest\n", "[tox]\nenvlist = py312\n"),
    "package.json": ('{"eslintConfig": {"rules": {}}}', '{"name": "x", "version": "1.0.0"}'),
    "settings.json": (
        '{"env": {"CLAQ_PYTHON": "/tmp/evil"}}',
        '{"env": {"DEBUG": "true"}, "permissions": {"allow": ["Bash(npm:*)"]}}',
    ),
    "settings.local.json": (
        '{"disabledPlugins": ["claq"]}',
        '{"terminal.integrated.env.osx": {"FOO": "1"}}',
    ),
}


def test_protected_files_set_is_exactly_as_declared() -> None:
    """`PROTECTED_FILES` の内容が完全一致で固定されていること（層2）。

    層1 の parametrize は集合を入力にするため、要素を削除するとケースごと消えて
    緑のまま通る。保護語彙の増減はここでしか止まらない。
    """
    assert config_protection.PROTECTED_FILES == _EXPECTED_PROTECTED_FILES


def test_conditionally_protected_files_set_is_exactly_as_declared() -> None:
    """`CONDITIONALLY_PROTECTED_FILES` の内容が完全一致で固定されていること（層2）。"""
    assert config_protection.CONDITIONALLY_PROTECTED_FILES == _EXPECTED_CONDITIONALLY_PROTECTED_FILES


def test_directory_change_commands_set_is_exactly_as_declared() -> None:
    """`_DIRECTORY_CHANGE_COMMANDS` の内容が完全一致で固定されていること（層2）。"""
    assert bash_config_protection._DIRECTORY_CHANGE_COMMANDS == _EXPECTED_DIRECTORY_CHANGE_COMMANDS


def test_lint_section_headers_are_exactly_as_declared() -> None:
    """`_LINT_SECTION_HEADERS` の内容が完全一致で固定されていること（層2）。"""
    assert config_protection._LINT_SECTION_HEADERS == _EXPECTED_LINT_SECTION_HEADERS


def test_lint_keys_are_exactly_as_declared() -> None:
    """`_LINT_KEYS` の内容が完全一致で固定されていること（層2）。"""
    assert config_protection._LINT_KEYS == _EXPECTED_LINT_KEYS


def test_folded_protected_sets_cover_every_declared_name() -> None:
    """判定に使う畳み済み集合が、宣言側の全要素を漏れなく含むこと。

    判定は `*_FOLDED` 側でのみ行われるため、畳み込みが一部を落としても宣言側の
    見た目は正しいまま保護だけが消える。件数の一致まで見て、畳み込みが余計な
    名前を増やしていないことも同時に固定する。

    期待値は実装側の集合ではなく層2 の `_EXPECTED_*` から組み立てる。実装の集合
    から実装と同じ式で作ると「畳み込みがまったく別の入力から作られている」場合
    しか捕まえられず、宣言側と畳み込み側が揃って壊れた場合に緑のまま通る。
    """
    from claq.hooks.hook_common import normalize_protected_name

    for expected, folded in (
        (_EXPECTED_PROTECTED_FILES, config_protection.PROTECTED_FILES_FOLDED),
        (
            _EXPECTED_CONDITIONALLY_PROTECTED_FILES,
            config_protection.CONDITIONALLY_PROTECTED_FILES_FOLDED,
        ),
    ):
        assert folded == frozenset(normalize_protected_name(name) for name in expected)
        assert len(folded) == len(expected)


@pytest.mark.parametrize("name", sorted(_EXPECTED_PROTECTED_FILES))
def test_every_protected_file_is_denied_on_write(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PROTECTED_FILES` の全要素が Write 経路で deny されること（層1）。

    Args:
        name: 保護対象のファイル名。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run("config_protection", _write(name), monkeypatch) == 2


@pytest.mark.parametrize("name", sorted(_EXPECTED_PROTECTED_FILES))
def test_every_protected_file_is_denied_on_bash_write(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PROTECTED_FILES` の全要素が Bash 経路（リダイレクト・削除・作成）で deny されること（層1）。

    Write だけを見ると、`config_protection` にしか載っていない名前を
    `bash_config_protection` が取りこぼしていても気付けない。両フックが同じ
    語彙を共有していることを要素ごとに実測する。

    `touch` を含めるのは M-9 の指摘（保護対象 36 ファイル**全て**で allow だった）を
    サンプルではなく全数で固定するため。空ファイルの新規作成は内容を書き換えないが、
    linter の上位カスケード探索を止めるので上書きと同じ弱体化にあたる。

    Args:
        name: 保護対象のファイル名。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run("bash_config_protection", _bash(f"printf x > {name}"), monkeypatch) == 2
    assert _run("bash_config_protection", _bash(f"rm {name}"), monkeypatch) == 2
    assert _run("bash_config_protection", _bash(f"touch {name}"), monkeypatch) == 2


@pytest.mark.parametrize("name", sorted(_EXPECTED_PROTECTED_FILES))
def test_every_protected_file_is_denied_case_insensitively(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PROTECTED_FILES` の全要素が大文字綴りでも deny されること（層1・C-2 の全数版）。

    APFS / NTFS では `RUFF.TOML` が `ruff.toml` そのものを指す。従来は数件の
    サンプルでしか確認していなかった。

    Args:
        name: 保護対象のファイル名。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run("config_protection", _write(name.upper()), monkeypatch) == 2


@pytest.mark.parametrize("name", _NON_MEMBER_CONFIG_NAMES)
def test_non_member_config_name_is_allowed(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """保護語彙に載っていない類似名は allow のままであること（層1 の陰性対照）。

    これが無いと「全部 deny する」実装でも層1 が緑になる。

    Args:
        name: 保護語彙に載っていない類似ファイル名。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert name not in _EXPECTED_PROTECTED_FILES
    assert _run("config_protection", _write(name), monkeypatch) == 0


@pytest.mark.parametrize("name", sorted(_EXPECTED_CONDITIONALLY_PROTECTED_FILES))
def test_every_conditionally_protected_file_denies_only_lint_signals(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """条件付き保護の全要素が、lint 兆候ありで deny・なしで allow になること（層1）。

    Args:
        name: 条件付き保護対象のファイル名。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    with_signal, without_signal = _CONDITIONAL_SIGNALS[name]
    assert _run("config_protection", _write_content(name, with_signal), monkeypatch) == 2
    assert _run("config_protection", _write_content(name, without_signal), monkeypatch) == 0


@pytest.mark.parametrize("command", sorted(_EXPECTED_DIRECTORY_CHANGE_COMMANDS))
def test_every_directory_change_command_forces_unconditional_deny(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_DIRECTORY_CHANGE_COMMANDS` の全要素が repo スコープ判定を放棄させること（層1）。

    docs/adr/shell-analysis-boundary.md: cwd を動かすコマンドがあると相対パス解決が実行時の位置とずれるため、
    repo スコープを信用せず保護対象 basename のヒットをそのまま deny する。

    判別には **repo ルート外**を指す書き込み先を使う。repo 内のパスは
    `_within_repo_root` で当然 deny になるので、それでは「cd 系だから deny した」
    のか「repo 内だから deny した」のか区別できない —— 実測でも
    `pushd sub && printf x > ../ruff.toml` は cd 系の判定を落としても deny のまま
    だった。cd 系が無い同じコマンドが allow であることを対照として先に固定する。

    Args:
        command: ディレクトリ移動コマンド名。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    outside_repo = "printf x > ../ruff.toml"
    assert _run("bash_config_protection", _bash(outside_repo), monkeypatch) == 0
    assert _run("bash_config_protection", _bash(f"{command} sub && {outside_repo}"), monkeypatch) == 2


@pytest.mark.parametrize("header", _EXPECTED_LINT_SECTION_HEADERS)
def test_every_lint_section_header_is_detected(
    header: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_LINT_SECTION_HEADERS` の全要素が条件付き保護を発火させること（層1）。

    前方一致で判定するため、閉じ括弧を持たない要素（`[tool.ruff` / `[testenv`）は
    サブセクション形で流す。

    Args:
        header: セクション見出しの照合文字列。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    line = header if header.endswith("]") else f"{header}.sub]"
    assert _run("config_protection", _write_content("setup.cfg", f"{line}\nx = 1\n"), monkeypatch) == 2


@pytest.mark.parametrize("key", _EXPECTED_LINT_KEYS)
def test_every_lint_key_is_detected(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_LINT_KEYS` の全要素が見出し無しの値行編集でも条件付き保護を発火させること（層1）。

    Args:
        key: lint 設定キー。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run("config_protection", _write_content("pyproject.toml", f"{key} = 1\n"), monkeypatch) == 2


@pytest.mark.parametrize("word", ["description", "requires-python", "name", "[project]", "[metadata]"])
def test_non_lint_section_or_key_is_allowed(word: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """lint 語彙でない見出し・キーは条件付き保護を発火させないこと（層1 の陰性対照）。

    Args:
        word: lint 語彙に載っていない見出しまたはキー。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    body = f"{word}\nx = 1\n" if word.startswith("[") else f"{word} = 1\n"
    assert _run("config_protection", _write_content("pyproject.toml", body), monkeypatch) == 0


# --------------------------------------------------------------------------
# 全語彙集合の固定（M-10）
#
# H-11 は保護**ファイル名**の集合だけを固定した。同じ盲点は保護フックの他の語彙
# 集合すべてに残っている: 集合リテラルは複数行にまたがっても 1 statement なので、
# 要素を 1 個消しても実行行は変わらず `fail_under=100` では原理的に検出できない。
# 代表値をフックへ流す検査（`_BLOCKED_CASES`）はサンプルしか見ないため、消えた
# 要素がサンプル外なら緑のまま通る。
#
# 検査は H-11 と同じ 2 層で行う。
#
#   層2（固定）: チェックイン済みの期待値に対する**完全一致**。語彙の増減を
#       人間の意識的な変更に限定する関門。これが本項目の非交渉部分。
#   層1（列挙）: 期待値の**全要素**をフックへ流し、要素ごとに判定が変わることを
#       実測する。「集合には載っているが実際には効いていない」要素を検出する。
#
# 層1 の入力は必ず期待値（`_EXPECTED_VOCABULARIES`）側から取る。実装の集合を
# parametrize の入力にすると、要素を消したときにテストケースも一緒に消えて
# スイートが緑のまま通る（H-11 で実測した罠）。
#
# さらに「期待値表そのものから行を消す」という 1 段上の盲点を
# `test_vocabulary_inventory_is_complete` が塞ぐ。`claq.hooks` 配下の全モジュールを
# 走査して、モジュール自身が定義した文字列コレクション定数を機械的に数え上げ、
# 期待値表の見出し集合と完全一致することを要求する。新しい語彙集合を実装へ足して
# 表へ足し忘れた場合も、表から行を消した場合も、ここで落ちる。
# --------------------------------------------------------------------------

# `bash_config_protection._RAW_TEXT_RISK_INDICATORS` の構成要素（層2）。
_EXPECTED_REMOVE_COMMANDS = frozenset({
    "clc",
    "clear-content",
    "del",
    "erase",
    "mi",
    "move-item",
    "mv",
    "rd",
    "remove-item",
    "ri",
    "rm",
    "shred",
    "truncate",
    "unlink",
})
_EXPECTED_MODE_COMMANDS = frozenset({"chflags", "chgrp", "chmod", "chown"})
_EXPECTED_TOUCH_COMMANDS = frozenset({"touch"})

# `config_protection._PROTECTED_PATH_SEGMENTS` の期待内容（層2）。
_EXPECTED_PROTECTED_PATH_SEGMENTS = ((".git", "hooks"), ("hooks", "hooks.json"))


def _folded(names: frozenset[str]) -> frozenset[str]:
    """保護名の畳み込みを期待値側でも同じ規則で行う。

    Args:
        names: 畳む前の名前集合。

    Returns:
        大小無視で畳んだ名前集合。
    """
    return frozenset(hook_common.normalize_protected_name(name) for name in names)


# 語彙集合の期待内容（層2）。キーは ``<モジュール名>.<定数名>``。
#
# **実装からコピーせず、変更のたびに人間がここを直す**のが目的の設計である。
# 保護語彙は「壊れても誰も困らないから気付かれない」種類のデータで、外部に
# 突き合わせ先を持たない。ここへ複製することで、語彙の増減がレビュー対象へ上がる。
# 派生値（畳み込み・和集合・連結）だけは、実装と同じ式を**期待値定数の上で**
# 組み立てる。実装側から作ると「派生の入力ごと壊れた」場合に緑のまま通るため。
_EXPECTED_VOCABULARIES: dict[str, Any] = {
    # --- bash_config_protection ---
    "bash_config_protection._BASH_TOOL_NAMES": frozenset({"bash"}),
    "bash_config_protection._REDIRECT_OPERATORS": frozenset(
        {">", ">>", "&>", ">|", "1>", "2>", "1>>", "2>>"}
    ),
    "bash_config_protection._LAST_ARG_WRITE_COMMANDS": frozenset(
        {"copy", "copy-item", "cp", "cpi", "install", "mi", "move", "move-item", "mv"}
    ),
    "bash_config_protection._REMOVE_COMMANDS": _EXPECTED_REMOVE_COMMANDS,
    "bash_config_protection._MODE_COMMANDS": _EXPECTED_MODE_COMMANDS,
    "bash_config_protection._TEE_COMMANDS": frozenset(
        {"ac", "add-content", "new-item", "ni", "out-file", "sc", "set-content", "tee"}
    ),
    "bash_config_protection._INPLACE_EDIT_COMMANDS": frozenset({"perl", "ruby", "sed"}),
    "bash_config_protection._LN_COMMANDS": frozenset({"ln"}),
    "bash_config_protection._DD_COMMANDS": frozenset({"dd"}),
    "bash_config_protection._TOUCH_COMMANDS": _EXPECTED_TOUCH_COMMANDS,
    "bash_config_protection._DIRECTORY_CHANGE_COMMANDS": _EXPECTED_DIRECTORY_CHANGE_COMMANDS,
    "bash_config_protection._COMMAND_POSITION_WRAPPERS": frozenset(
        {
            "chrt",
            "command",
            "doas",
            "env",
            "ionice",
            "nice",
            "nohup",
            "setsid",
            "stdbuf",
            "sudo",
            "taskset",
            "time",
            "timeout",
            "xargs",
        }
    ),
    "bash_config_protection._WRAPPER_VALUE_SHORT_OPTIONS": frozenset({"-u"}),
    # 派生: 保護 basename の和集合。
    "bash_config_protection._ALL_PROTECTED_BASENAMES": (
        _folded(_EXPECTED_PROTECTED_FILES) | _folded(_EXPECTED_CONDITIONALLY_PROTECTED_FILES)
    ),
    # 派生: malformed JSON fallback の指標列（順序も固定する）。
    "bash_config_protection._RAW_TEXT_RISK_INDICATORS": (
        (">", "tee", "-i")
        + tuple(sorted(_EXPECTED_REMOVE_COMMANDS))
        + tuple(sorted(_EXPECTED_MODE_COMMANDS))
        + tuple(sorted(_EXPECTED_TOUCH_COMMANDS))
        + ("ln",)
    ),
    # --- block_no_verify ---
    "block_no_verify._VALUE_LONG_OPTIONS": frozenset(
        {
            "--attr-source",
            "--author",
            "--cleanup",
            "--config-env",
            "--date",
            "--exec-path",
            "--file",
            "--fixup",
            "--git-dir",
            "--message",
            "--namespace",
            "--pathspec-from-file",
            "--reedit-message",
            "--reuse-message",
            "--squash",
            "--super-prefix",
            "--template",
            "--trailer",
            "--work-tree",
        }
    ),
    "block_no_verify._BOOLEAN_GLOBAL_LONG_OPTIONS": frozenset(
        {
            "--bare",
            "--glob-pathspecs",
            "--help",
            "--html-path",
            "--icase-pathspecs",
            "--info-path",
            "--literal-pathspecs",
            "--man-path",
            "--no-advice",
            "--no-lazy-fetch",
            "--no-optional-locks",
            "--no-pager",
            "--no-replace-objects",
            "--noglob-pathspecs",
            "--paginate",
            "--version",
        }
    ),
    "block_no_verify._VALUE_SHORT_OPTIONS": frozenset("CcmFt"),
    "block_no_verify._BOOLEAN_GLOBAL_SHORT_OPTIONS": frozenset("pPvh"),
    "block_no_verify._OPTIONAL_VALUE_SHORT_OPTIONS": frozenset("uS"),
    "block_no_verify._SENSITIVE_CONFIG_KEY_PREFIXES": (
        "core.hookspath",
        "include.path",
        "includeif.",
        "alias.",
    ),
    "block_no_verify._GIT_CONFIG_INJECTION_ENV_NAMES": frozenset(
        {
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_SYSTEM",
        }
    ),
    # `_ENV_BOOLEAN_OPTIONS` と `_EXEC_WRAPPERS` は撤去した。ラッパ名とその
    # オプションを列挙して読み飛ばす設計は、列挙が漏れた瞬間に**素通り側へ倒れる**
    # （実測: `env -v` / `env -C` / `command -p` / `sudo -E` / `nohup env` の 5 形が
    # exit 0 だった）。前置トークンを全部走査する形へ変えたので、この 2 つの語彙は
    # もう存在しない。語彙を持たない実装は語彙の陳腐化で壊れない。
    "block_no_verify._GIT_CONFIG_READ_ONLY_FLAGS": frozenset(
        {"--get", "--get-all", "--get-regexp", "--list", "--show-origin"}
    ),
    "block_no_verify._GIT_CONFIG_WRITE_FLAGS": frozenset(
        {"--add", "--replace-all", "--unset", "--unset-all"}
    ),
    "block_no_verify._GIT_CONFIG_NEW_READ_OPS": frozenset({"get", "list"}),
    "block_no_verify._GIT_CONFIG_NEW_WRITE_OPS": frozenset({"add", "set", "unset"}),
    # --- commit_quality_scanner ---
    "commit_quality_scanner._LINTABLE_SUFFIXES": {".js", ".jsx", ".ts", ".tsx", ".py", ".go", ".rs"},
    "commit_quality_scanner._MINIFIED_SUFFIXES": (".min.js", ".min.css"),
    "commit_quality_scanner._SECRET_SCAN_EXCLUDED_FILENAMES": {
        "Cargo.lock",
        "Pipfile.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    },
    # --- config_protection ---
    "config_protection._WRITE_TOOL_NAMES": frozenset({"edit", "multiedit", "write"}),
    "config_protection.PROTECTED_FILES": _EXPECTED_PROTECTED_FILES,
    "config_protection.CONDITIONALLY_PROTECTED_FILES": _EXPECTED_CONDITIONALLY_PROTECTED_FILES,
    "config_protection.PROTECTED_FILES_FOLDED": _folded(_EXPECTED_PROTECTED_FILES),
    "config_protection.CONDITIONALLY_PROTECTED_FILES_FOLDED": _folded(
        _EXPECTED_CONDITIONALLY_PROTECTED_FILES
    ),
    # ホスト設定で保護フックを止められるキー。`env` 全般を条件にすると
    # `update-config` skill の `DEBUG=true` や `.vscode/settings.json` の
    # `terminal.integrated.env.*` を巻き込むため、`CLAQ_PYTHON` の出現そのものと
    # 保護を外せる 3 キーだけに絞る。
    "config_protection._SETTINGS_GUARD_KEYS": (
        "CLAQ_PYTHON",
        '"hooks"',
        '"enabledPlugins"',
        '"disabledPlugins"',
    ),
    "config_protection._PROTECTED_PATH_SEGMENTS": _EXPECTED_PROTECTED_PATH_SEGMENTS,
    "config_protection._PROTECTED_PATH_SEGMENTS_FOLDED": tuple(
        tuple(hook_common.normalize_protected_name(part) for part in segments)
        for segments in _EXPECTED_PROTECTED_PATH_SEGMENTS
    ),
    "config_protection._LINT_SECTION_HEADERS": _EXPECTED_LINT_SECTION_HEADERS,
    "config_protection._LINT_KEYS": _EXPECTED_LINT_KEYS,
    "config_protection._PACKAGE_JSON_LINT_KEYS": ("eslintConfig", "prettier"),
    "config_protection._TOX_COMMAND_KEYS": ("commands", "commands_pre", "commands_post"),
    # --- hook_common ---
    "hook_common._SHELL_SEPARATORS": frozenset({"&&", "||", ";", "|", "&", "(", ")"}),
    "hook_common.SHELL_WRAPPER_EXECUTABLES": frozenset({"sh", "bash", "zsh", "dash"}),
    "hook_common._BODY_EXECUTING_BUILTINS": frozenset({".", "source", "eval"}),
    "hook_common._COMMENT_PRECEDING_CHARS": frozenset(" \t\n;|&()<>"),
    "hook_common._HEREDOC_CONTINUATION_SUFFIXES": ("\\", "|", "&"),
    "hook_common.ALWAYS_MUTATING_EDIT_EXECUTABLES": frozenset({"ed", "red", "ex", "sponge"}),
    # --- pre_bash_commit_quality ---
    "pre_bash_commit_quality._INDEX_MUTATING_GIT_SUBCOMMANDS": frozenset(
        {
            "add",
            "am",
            "apply",
            "checkout",
            "cherry-pick",
            "merge",
            "mv",
            "pull",
            "rebase",
            "reset",
            "restore",
            "revert",
            "rm",
            "stash",
            "switch",
        }
    ),
    "pre_bash_commit_quality._WORKTREE_MUTATING_EXECUTABLES": frozenset(
        {
            "chmod",
            "chown",
            "cp",
            "dd",
            "install",
            "ln",
            "mkdir",
            "mv",
            "patch",
            "rm",
            "shred",
            "tee",
            "touch",
            "truncate",
            "unlink",
        }
    ),
    "pre_bash_commit_quality._INPLACE_EDIT_EXECUTABLES": frozenset({"perl", "sed"}),
    "pre_bash_commit_quality._MUTATING_REDIRECT_OPERATORS": frozenset(
        {">", ">>", "&>", ">|", "1>", "2>", "1>>", "2>>", ">&"}
    ),
}

# 完全一致で固定**しない**語彙と、その代わりに何が固定しているか。
#
# `_SECRET_PATTERNS` は 14 本の正規表現リテラルで、期待値へ複製すると同じ綴りを
# 2 か所で保守することになる。この集合には外部の突き合わせ先（`claq.mem.redaction`）
# があり、`tests/mem/test_redaction.py::TestVendorPatternSync` が「同じ検体に両者が
# 反応するか」という振る舞いで全ベンダ形式を固定している。H-11 の方針
# （「外部に抽出元がある場合はそちらを使う」）どおり、そちらを単一の関門にする。
_EXTERNALLY_PINNED_VOCABULARIES: dict[str, str] = {
    "commit_quality_scanner._SECRET_PATTERNS": (
        "tests/mem/test_redaction.py::TestVendorPatternSync が redaction 側との"
        "振る舞い一致で固定する（正規表現リテラルの二重保守を避ける）"
    ),
}


def _is_vocabulary(value: Any) -> bool:
    """値が「文字列の語彙集合」かを判定する。

    要素がすべて文字列の集合／タプル（``PROTECTED_FILES`` 等）と、要素が
    すべて文字列タプルの集合／タプル（``_PROTECTED_PATH_SEGMENTS`` 等）を
    語彙として扱う。空の集合は語彙とみなさない（判定材料が無いため）。

    Args:
        value: モジュール属性の値。

    Returns:
        語彙集合なら True。
    """
    if not isinstance(value, (frozenset, set, tuple)) or not value:
        return False
    if all(isinstance(element, str) for element in value):
        return True
    return all(
        isinstance(element, tuple) and all(isinstance(part, str) for part in element)
        for element in value
    )


def _declared_vocabularies(module: ModuleType) -> dict[str, Any]:
    """モジュール**自身が定義した**語彙集合を返す。

    `vars(module)` だけを見ると import した名前（`bash_config_protection` へ
    import された `PROTECTED_FILES_FOLDED` 等）まで数え上げ、同じ集合が定義元と
    参照側の両方で期待値を要求されてしまう。ソースを AST で読み、モジュール
    トップレベルの代入文で束縛された名前だけに絞る。

    Args:
        module: 対象モジュール。

    Returns:
        ``{定数名: 値}``。語彙集合でない属性は含まない。
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    declared: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            declared |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            declared.add(node.target.id)
    return {
        name: getattr(module, name)
        for name in sorted(declared)
        if _is_vocabulary(getattr(module, name, None))
    }


def _hook_modules() -> dict[str, ModuleType]:
    """`claq.hooks` 配下の全モジュールを import して返す。

    モジュール名を表へ書き写さず実際のパッケージから数え上げるため、新しい
    フックモジュールを足しても語彙の数え上げから漏れない。

    Returns:
        ``{モジュール名: モジュール}``。
    """
    return {
        info.name: importlib.import_module(f"claq.hooks.{info.name}")
        for info in pkgutil.iter_modules(claq.hooks.__path__)
    }


def test_vocabulary_inventory_is_complete() -> None:
    """`claq.hooks` 配下の語彙集合が 1 つ残らず期待値表に載っていること（層2 の関門）。

    層2 の完全一致検査は「表に載っている集合」しか守れない。表から行を消せば、
    その集合は無検査に戻る（H-11 が層1 について指摘したのと同じ罠が 1 段上で
    再現する）。実装側を機械的に数え上げて見出し集合と突き合わせることで、
    表の行を消す・新しい語彙を足して表へ書き忘れる、のどちらも赤くする。
    """
    actual = {
        f"{module_name}.{name}"
        for module_name, module in _hook_modules().items()
        for name in _declared_vocabularies(module)
    }
    assert actual == set(_EXPECTED_VOCABULARIES) | set(_EXTERNALLY_PINNED_VOCABULARIES)


@pytest.mark.parametrize("qualified", sorted(_EXTERNALLY_PINNED_VOCABULARIES))
def test_externally_pinned_vocabulary_still_exists(qualified: str) -> None:
    """完全一致検査を免除した語彙が、実装側に今も存在すること。

    免除の一覧は「表に載せない理由」を書く場所であって、消えた集合を隠す場所では
    ない。名前ごと消えたら免除の前提（外部の突き合わせ先がある）も消えている。

    Args:
        qualified: ``<モジュール名>.<定数名>``。
    """
    module_name, _, name = qualified.partition(".")
    assert _is_vocabulary(getattr(_hook_modules()[module_name], name))


@pytest.mark.parametrize("qualified", sorted(_EXPECTED_VOCABULARIES))
def test_vocabulary_is_exactly_as_declared(qualified: str) -> None:
    """語彙集合の内容が完全一致で固定されていること（層2）。

    集合リテラルは複数行でも 1 statement なので、要素を消しても実行行は変わらず
    `fail_under=100` では検出できない。語彙の増減をここでレビュー対象へ上げる。

    Args:
        qualified: ``<モジュール名>.<定数名>``。
    """
    module_name, _, name = qualified.partition(".")
    assert getattr(_hook_modules()[module_name], name) == _EXPECTED_VOCABULARIES[qualified]


def _fill(template: str, item: str) -> str:
    """テンプレート中の ``{item}`` を要素で置換する。

    `str.format` は使わない。payload に JSON（``{"eslintConfig": {}}``）を載せる
    ケースがあり、波括弧のエスケープでテンプレートが読めなくなるため。

    Args:
        template: ``{item}`` を含む文字列。
        item: 埋め込む語彙要素。

    Returns:
        置換後の文字列。
    """
    return template.replace("{item}", item)


# 層1（deny / allow の対）: (語彙の完全修飾名, フック名, deny 書式, allow 書式)。
#
# allow 側は必ず「同じ語を使い、判定軸だけを外した」形にする。そうしないと
# 「全部 deny する」実装でも deny 側だけが緑になり、要素が効いている証拠にならない。
_VOCABULARY_DENY_ALLOW_CASES = (
    # --- 保護 config への書込み verb（bash_config_protection） ---
    ("bash_config_protection._REMOVE_COMMANDS", "bash_config_protection", "{item} ruff.toml", "{item} app.py"),
    ("bash_config_protection._MODE_COMMANDS", "bash_config_protection", "{item} 000 ruff.toml", "{item} 000 app.py"),
    ("bash_config_protection._TEE_COMMANDS", "bash_config_protection", "echo x | {item} ruff.toml", "echo x | {item} app.py"),
    (
        "bash_config_protection._LAST_ARG_WRITE_COMMANDS",
        "bash_config_protection",
        "{item} src.txt ruff.toml",
        "{item} src.txt app.py",
    ),
    ("bash_config_protection._TOUCH_COMMANDS", "bash_config_protection", "{item} ruff.toml", "{item} app.py"),
    ("bash_config_protection._LN_COMMANDS", "bash_config_protection", "{item} -s weak.toml ruff.toml", "{item} -s weak.toml app.py"),
    ("bash_config_protection._DD_COMMANDS", "bash_config_protection", "{item} if=/dev/null of=ruff.toml", "{item} if=/dev/null of=app.py"),
    # in-place 側は「フラグを外すと allow」を対にする（フラグ判定が生きている証拠）。
    (
        "bash_config_protection._INPLACE_EDIT_COMMANDS",
        "bash_config_protection",
        "{item} -i s/x/y/ ruff.toml",
        "{item} s/x/y/ ruff.toml",
    ),
    # wrapper は「実行位置に無ければ allow」を対にする（M-01 の判定軸）。
    ("bash_config_protection._COMMAND_POSITION_WRAPPERS", "bash_config_protection", "{item} rm ruff.toml", "echo {item} rm ruff.toml"),
    (
        "bash_config_protection._WRAPPER_VALUE_SHORT_OPTIONS",
        "bash_config_protection",
        "sudo {item} root rm ruff.toml",
        "echo sudo {item} root rm ruff.toml",
    ),
    # --- hook_common の共有語彙 ---
    ("hook_common.ALWAYS_MUTATING_EDIT_EXECUTABLES", "bash_config_protection", "{item} ruff.toml", "{item} app.py"),
    ("hook_common.SHELL_WRAPPER_EXECUTABLES", "block_no_verify", "{item} -c '" + _NO_VERIFY + "'", "{item} -c 'git commit -m x'"),
    (
        "hook_common._BODY_EXECUTING_BUILTINS",
        "block_no_verify",
        "{item} /dev/stdin <<'EOF'\n" + _NO_VERIFY + "\nEOF",
        "cat /dev/stdin <<'EOF'\n" + _NO_VERIFY + "\nEOF",
    ),
    # 区切りが効かないとセグメントが融合し、後続コマンドが実行位置に来ない。
    ("hook_common._SHELL_SEPARATORS", "bash_config_protection", "cat notes.txt {item} rm ruff.toml", "cat notes.txt {item} rm app.py"),
    # --- block_no_verify の config 注入経路 ---
    ("block_no_verify._SENSITIVE_CONFIG_KEY_PREFIXES", "block_no_verify", "git -c {item}=x commit -m y", "git -c user.name=x commit -m y"),
    ("block_no_verify._GIT_CONFIG_INJECTION_ENV_NAMES", "block_no_verify", "{item}=x git commit -m y", "SOME_VAR=x git commit -m y"),
    ("block_no_verify._GIT_CONFIG_WRITE_FLAGS", "block_no_verify", "git config {item} core.hooksPath x", "git config {item} user.name x"),
    ("block_no_verify._GIT_CONFIG_NEW_WRITE_OPS", "block_no_verify", "git config {item} core.hooksPath x", "git config {item} user.name x"),
    # --- commit 前 mutation ガード ---
    (
        "pre_bash_commit_quality._INDEX_MUTATING_GIT_SUBCOMMANDS",
        "pre_bash_commit_quality",
        "git {item} && git commit -m 'fix: x'",
        "git status && git commit -m 'fix: x'",
    ),
    (
        "pre_bash_commit_quality._WORKTREE_MUTATING_EXECUTABLES",
        "pre_bash_commit_quality",
        "{item} app.py && git commit -m 'fix: x'",
        "cat app.py && git commit -m 'fix: x'",
    ),
    (
        "pre_bash_commit_quality._INPLACE_EDIT_EXECUTABLES",
        "pre_bash_commit_quality",
        "{item} -i s/a/b/ app.py && git commit -m 'fix: x'",
        "{item} s/a/b/ app.py && git commit -m 'fix: x'",
    ),
)

# 層1（allow のみ + 陽性対照）: (語彙の完全修飾名, フック名, allow 書式, 対照 deny コマンド)。
#
# 「値を取るオプション」「値を取らないことが確定しているオプション」の語彙は、
# 要素を消すと**未知オプション**として fail-closed（deny）へ倒れる。したがって
# 判定軸が生きている証拠は allow 側にしか出ない。allow が空虚でないことは、
# 同じフックが対照コマンドを deny することで示す。
_VOCABULARY_ALLOW_ONLY_CASES = (
    ("block_no_verify._VALUE_LONG_OPTIONS", "block_no_verify", "git commit {item} --no-verify", _NO_VERIFY),
    ("block_no_verify._BOOLEAN_GLOBAL_LONG_OPTIONS", "block_no_verify", "git {item} log -n 5", "git --future-global-option v commit -n"),
    ("block_no_verify._VALUE_SHORT_OPTIONS", "block_no_verify", "git commit -{item} --no-verify", _NO_VERIFY),
    ("block_no_verify._BOOLEAN_GLOBAL_SHORT_OPTIONS", "block_no_verify", "git -{item} log -n 5", "git -Z log -n 5"),
    ("block_no_verify._OPTIONAL_VALUE_SHORT_OPTIONS", "block_no_verify", "git -{item} log -n 5", "git -Z log -n 5"),
    ("block_no_verify._GIT_CONFIG_READ_ONLY_FLAGS", "block_no_verify", "git config {item} core.hooksPath", "git config --add core.hooksPath x"),
    ("block_no_verify._GIT_CONFIG_NEW_READ_OPS", "block_no_verify", "git config {item} core.hooksPath", "git config set core.hooksPath x"),
    # 行コメントの開始条件。直前文字が語彙に無ければ `#` は語の一部なので deny。
    ("hook_common._COMMENT_PRECEDING_CHARS", "block_no_verify", "echo ok{item}# " + _NO_VERIFY, "echo okX# " + _NO_VERIFY),
)

# 層1（Write 系 payload）: (語彙の完全修飾名, deny payload の組み立て, allow payload)。
_VOCABULARY_WRITE_CASES = (
    (
        "config_protection._WRITE_TOOL_NAMES",
        lambda item: {"tool_name": item, "tool_input": {"file_path": "ruff.toml", "content": "x"}},
        {"tool_name": "Read", "tool_input": {"file_path": "ruff.toml"}},
    ),
    (
        "config_protection._PACKAGE_JSON_LINT_KEYS",
        lambda item: _write_content("package.json", '{"' + item + '": {}}'),
        _write_content("package.json", '{"name": "x"}'),
    ),
    (
        "config_protection._TOX_COMMAND_KEYS",
        lambda item: _write_content("tox.ini", f"{item} = pytest\n"),
        _write_content("tox.ini", "[tox]\nenvlist = py312\n"),
    ),
    (
        "config_protection._PROTECTED_PATH_SEGMENTS",
        lambda item: _write("/".join(item) + "/pre-commit"),
        _write("docs/hooks/pre-commit"),
    ),
)

# 層1 をこのファイルの別テストが担っている語彙と、その担い手。
#
# 「層1 が無い」ことと「層1 を別の名前で持っている」ことを取り違えないための表。
# ここに載せるには、その語彙の**全要素**を実測しているテストが実在する必要がある。
_VOCABULARY_LAYER1_ELSEWHERE = {
    "bash_config_protection._BASH_TOOL_NAMES": "_HARNESS_CASES の copilot bash 危険 / 通常",
    "bash_config_protection._ALL_PROTECTED_BASENAMES": "test_every_protected_file_is_denied_on_bash_write",
    "bash_config_protection._DIRECTORY_CHANGE_COMMANDS": "test_every_directory_change_command_forces_unconditional_deny",
    "config_protection.PROTECTED_FILES": "test_every_protected_file_is_denied_on_write",
    "config_protection.CONDITIONALLY_PROTECTED_FILES": "test_every_conditionally_protected_file_denies_only_lint_signals",
    "config_protection.PROTECTED_FILES_FOLDED": "test_folded_protected_sets_cover_every_declared_name",
    "config_protection.CONDITIONALLY_PROTECTED_FILES_FOLDED": "test_folded_protected_sets_cover_every_declared_name",
    "config_protection._PROTECTED_PATH_SEGMENTS_FOLDED": "_BLOCKED_CASES の C-2 保護 path の大文字（Bash）",
    "config_protection._LINT_SECTION_HEADERS": "test_every_lint_section_header_is_detected",
    "config_protection._LINT_KEYS": "test_every_lint_key_is_detected",
    "config_protection._SETTINGS_GUARD_KEYS": "test_every_conditionally_protected_file_denies_only_lint_signals の settings.json / settings.local.json",
}

# 層1 を持たない語彙と、その理由。
#
# 「要素を消しても判定が変わらない」ケースを層1 に混ぜると、緑が何も保証しない
# 偽の受領証になる。持てないものは持てないと書き、層2 の完全一致だけで守る。
_VOCABULARY_LAYER1_ABSENT = {
    "bash_config_protection._RAW_TEXT_RISK_INDICATORS": (
        "派生値（`_REMOVE_COMMANDS` 等の連結）であり、構成元の各集合が層1 を持つ。"
        "この tuple 自体は malformed JSON 専用の部分一致指標で、要素単位で判定が"
        "変わることを示す独立した経路が無い"
    ),
}


def _expand(cases: tuple[tuple[Any, ...], ...]) -> list[tuple[Any, ...]]:
    """語彙ごとのケース定義を、期待値の**全要素**へ展開する。

    展開元は必ず `_EXPECTED_VOCABULARIES`（チェックイン済みの期待値）にする。
    実装側の集合を入力にすると、要素を消したときに parametrize ケースも一緒に
    消えてスイートが緑のまま通る（H-11 で実測した罠）。

    Args:
        cases: 先頭要素が語彙の完全修飾名である定義タプルの列。

    Returns:
        ``(要素, *定義の残り)`` へ展開したケース列。

    Raises:
        例外は発生しません。
    """
    return [
        (qualified, item, *rest)
        for qualified, *rest in cases
        for item in sorted(_EXPECTED_VOCABULARIES[qualified], key=repr)
    ]


_DENY_ALLOW_PARAMS = _expand(_VOCABULARY_DENY_ALLOW_CASES)
_ALLOW_ONLY_PARAMS = _expand(_VOCABULARY_ALLOW_ONLY_CASES)
_WRITE_PARAMS = _expand(_VOCABULARY_WRITE_CASES)


@pytest.mark.parametrize(
    ("qualified", "item", "hook", "deny_template", "allow_template"),
    _DENY_ALLOW_PARAMS,
    ids=[f"{case[0]}[{case[1]!r}]" for case in _DENY_ALLOW_PARAMS],
)
def test_every_vocabulary_element_changes_the_verdict(
    qualified: str,
    item: str,
    hook: str,
    deny_template: str,
    allow_template: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """語彙の全要素が、判定軸を外すと allow へ戻る形で deny を生むこと（層1）。

    deny 側だけでは「全部 deny する」実装でも緑になる。同じ語のまま判定軸
    （保護対象かどうか / in-place フラグ / 実行位置）だけを外した allow 側を
    対に置き、要素が**その軸で**効いていることを示す。

    Args:
        qualified: 語彙の完全修飾名（失敗時の可読性のため）。
        item: 語彙要素。
        hook: 対象フック名。
        deny_template: exit 2 になるコマンド書式。
        allow_template: exit 0 になるコマンド書式。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run(hook, _bash(_fill(deny_template, item)), monkeypatch) == 2, qualified
    assert _run(hook, _bash(_fill(allow_template, item)), monkeypatch) == 0, qualified


@pytest.mark.parametrize(
    ("qualified", "item", "hook", "allow_template", "control_deny"),
    _ALLOW_ONLY_PARAMS,
    ids=[f"{case[0]}[{case[1]!r}]" for case in _ALLOW_ONLY_PARAMS],
)
def test_every_parser_vocabulary_element_keeps_a_normal_command_allowed(
    qualified: str,
    item: str,
    hook: str,
    allow_template: str,
    control_deny: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析語彙の全要素が、通常操作を allow に保つこと（層1・allow 側）。

    これらの語彙は要素を消すと「未知のオプション」になり、サブコマンド解決を
    信用できないとして fail-closed（deny）へ倒れる。したがって要素が効いている
    証拠は allow 側にしか現れない。同じフックが対照コマンドを deny することを
    同時に確認し、allow が空虚でないことを示す。

    Args:
        qualified: 語彙の完全修飾名（失敗時の可読性のため）。
        item: 語彙要素。
        hook: 対象フック名。
        allow_template: exit 0 になるコマンド書式。
        control_deny: 同じフックが exit 2 にする対照コマンド。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run(hook, _bash(control_deny), monkeypatch) == 2, qualified
    assert _run(hook, _bash(_fill(allow_template, item)), monkeypatch) == 0, qualified


@pytest.mark.parametrize(
    ("qualified", "item", "build_deny", "allow_payload"),
    _WRITE_PARAMS,
    ids=[f"{case[0]}[{case[1]!r}]" for case in _WRITE_PARAMS],
)
def test_every_write_path_vocabulary_element_changes_the_verdict(
    qualified: str,
    item: Any,
    build_deny: Any,
    allow_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write 経路の語彙の全要素が deny を生み、対照が allow のままであること（層1）。

    Args:
        qualified: 語彙の完全修飾名（失敗時の可読性のため）。
        item: 語彙要素。
        build_deny: 要素から deny payload を組み立てる callable。
        allow_payload: 同じ検査軸を外した allow payload。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run("config_protection", build_deny(item), monkeypatch) == 2, qualified
    assert _run("config_protection", allow_payload, monkeypatch) == 0, qualified


@pytest.mark.parametrize(
    "operator", sorted(_EXPECTED_VOCABULARIES["bash_config_protection._REDIRECT_OPERATORS"])
)
def test_every_redirect_operator_yields_a_write_target(operator: str) -> None:
    """`_REDIRECT_OPERATORS` の全要素がリダイレクト先を返すこと（層1・単体）。

    ここだけ end-to-end ではなく抽出関数を直接呼ぶ。``1>`` / ``2>`` /
    ``1>>`` / ``2>>`` は `tokenize` が数字を別トークンへ割るため ``>`` /
    ``>>`` そのものにも一致し、フックへ流す形では要素を消しても exit code が
    変わらない（＝緑が何も保証しない偽の受領証になる）。抽出関数の戻り値を
    見れば、要素ごとに参照されていることを実測できる。

    Args:
        operator: リダイレクト演算子。
    """
    assert bash_config_protection._redirect_targets(["printf", "x", operator, "ruff.toml"]) == [
        "ruff.toml"
    ]


@pytest.mark.parametrize(
    "operator",
    sorted(_EXPECTED_VOCABULARIES["pre_bash_commit_quality._MUTATING_REDIRECT_OPERATORS"]),
)
def test_every_mutating_redirect_operator_marks_the_segment(operator: str) -> None:
    """`_MUTATING_REDIRECT_OPERATORS` の全要素が mutation 判定を立てること（層1・単体）。

    `_REDIRECT_OPERATORS` と同じ理由で単体呼び出しにする。演算子を含まない
    同形のセグメントが False であることを対照に置き、判定が演算子由来だと示す。

    Args:
        operator: リダイレクト演算子。
    """
    assert pre_bash_commit_quality._segment_mutates_worktree_or_index(
        ["printf", "x", operator, "app.py"]
    )
    assert not pre_bash_commit_quality._segment_mutates_worktree_or_index(["printf", "x", "app.py"])


@pytest.mark.parametrize(
    "suffix", _EXPECTED_VOCABULARIES["hook_common._HEREDOC_CONTINUATION_SUFFIXES"]
)
def test_every_heredoc_continuation_suffix_keeps_the_body(suffix: str) -> None:
    """`_HEREDOC_CONTINUATION_SUFFIXES` の全要素が本文の剥がしを止めること（層1・単体）。

    docs/adr/shell-analysis-boundary.md の「除去しない条件 2」。継続演算子で終わる演算子行は本文の開始位置が
    次行とは限らないため、剥がさず検出側へ倒す。

    フックへ流す形は使えない。``\\`` で終わる行は `shlex` が改行ごとエスケープして
    次行の先頭語と 1 トークンへ融合させるため（実測: ``\\ngit`` という 1 トークンに
    なり git 起動として認識されない）、本文を残しても exit code が変わらない。
    正規化関数の出力を直接見る。

    Args:
        suffix: 継続演算子。
    """
    body = _NO_VERIFY
    kept = f"cat <<'EOF' {suffix}\n{body}\nEOF"
    stripped = f"cat <<'EOF'\n{body}\nEOF"
    assert hook_common.strip_data_heredoc_bodies(kept) == kept
    assert hook_common.strip_data_heredoc_bodies(stripped) != stripped


@pytest.mark.parametrize(
    "suffix", sorted(_EXPECTED_VOCABULARIES["commit_quality_scanner._LINTABLE_SUFFIXES"])
)
def test_every_lintable_suffix_is_checked(suffix: str) -> None:
    """`_LINTABLE_SUFFIXES` の全要素が lint 検査対象になること（層1・単体）。

    Args:
        suffix: 拡張子。
    """
    assert commit_quality_scanner.should_lint_file(f"src/app{suffix}")
    assert not commit_quality_scanner.should_lint_file("src/app.txt")


@pytest.mark.parametrize(
    "name", sorted(_EXPECTED_VOCABULARIES["commit_quality_scanner._SECRET_SCAN_EXCLUDED_FILENAMES"])
)
def test_every_excluded_lockfile_skips_secret_scan(name: str) -> None:
    """`_SECRET_SCAN_EXCLUDED_FILENAMES` の全要素が secret 走査から外れること（層1・単体）。

    Args:
        name: ロックファイル名。
    """
    assert not commit_quality_scanner.should_scan_secrets(f"sub/{name}")
    assert commit_quality_scanner.should_scan_secrets("sub/app.py")


@pytest.mark.parametrize(
    "suffix", _EXPECTED_VOCABULARIES["commit_quality_scanner._MINIFIED_SUFFIXES"]
)
def test_every_minified_suffix_skips_secret_scan(suffix: str) -> None:
    """`_MINIFIED_SUFFIXES` の全要素が secret 走査から外れること（層1・単体）。

    Args:
        suffix: 圧縮生成物の拡張子。
    """
    assert not commit_quality_scanner.should_scan_secrets(f"dist/bundle{suffix}")
    assert commit_quality_scanner.should_scan_secrets("dist/bundle.js")


def test_every_vocabulary_has_a_declared_layer1_status() -> None:
    """層2 で固定した全語彙が、層1 の担い手か不在理由のどちらかを宣言していること。

    層1 は「集合には載っているが実際には効いていない」要素を検出する層で、
    書けるのに書いていない集合を静かに増やさないための宣言を要求する。宣言先は
    次の 4 つのいずれか: この表の deny/allow 対、allow のみ対、Write 経路対、
    単体呼び出し。それ以外は `_VOCABULARY_LAYER1_ELSEWHERE`（別テストが担う）か
    `_VOCABULARY_LAYER1_ABSENT`（理由付きで持たない）へ明示する。
    """
    unit_covered = {
        "bash_config_protection._REDIRECT_OPERATORS",
        "pre_bash_commit_quality._MUTATING_REDIRECT_OPERATORS",
        "hook_common._HEREDOC_CONTINUATION_SUFFIXES",
        "commit_quality_scanner._LINTABLE_SUFFIXES",
        "commit_quality_scanner._SECRET_SCAN_EXCLUDED_FILENAMES",
        "commit_quality_scanner._MINIFIED_SUFFIXES",
    }
    declared = (
        {case[0] for case in _VOCABULARY_DENY_ALLOW_CASES}
        | {case[0] for case in _VOCABULARY_ALLOW_ONLY_CASES}
        | {case[0] for case in _VOCABULARY_WRITE_CASES}
        | unit_covered
        | set(_VOCABULARY_LAYER1_ELSEWHERE)
        | set(_VOCABULARY_LAYER1_ABSENT)
    )
    assert declared == set(_EXPECTED_VOCABULARIES)


@pytest.mark.parametrize(
    "command",
    [
        "GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env -v GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env -C /tmp GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "command -p env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "sudo -E GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "nohup env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "timeout 5 env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env -v GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp git commit -m x",
    ],
)
def test_env_injection_is_denied_through_any_wrapper(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前置にラッパやオプションが何個あっても env 注入を deny すること。

    ラッパ名とそのオプションを列挙して読み飛ばす設計だった頃、列挙に無い形は
    走査が途中で止まって素通りしていた（実測: 下 5 形が exit 0）。列挙を捨てて
    前置トークンを全部走査する形にした回帰防止。
    """
    assert _run("block_no_verify", _bash(command), monkeypatch) == 2


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "a=b"',
        "git commit --author=Foo -m x",
        "env SOME_VAR=x git commit -m y",
    ],
)
def test_benign_assignments_stay_allowed(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """危険でない代入・git のオプションは通り続けること（拾いすぎの対照）。

    前置を全部走査しても、deny するのは危険な変数名に一致したときだけ。
    git のオプション（`--author=Foo`）は識別子で始まらないので代入に一致しない。
    """
    assert _run("block_no_verify", _bash(command), monkeypatch) == 0


def test_huge_single_token_is_blocked_before_tokenizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """巨大な単一トークンがトークン予算を素通りせず BLOCK されること。

    `MAX_COMMAND_TOKENS` はトークン**数**しか縛らないが、走査コストは `shlex` の
    **バイト数**の二次で決まる。1MB の単一トークンはトークン数 2 なので予算を
    素通りし、実測 12.71 秒（timeout 15 秒に対し余裕 15%）かかっていた。host が
    kill した hook は exit code を返さないため silent fail-open になる。
    """
    from claq.hooks.hook_common import MAX_COMMAND_BYTES, command_exceeds_scan_budget

    huge = "echo " + "A" * (MAX_COMMAND_BYTES + 1)

    assert command_exceeds_scan_budget(huge) is True
    assert _run("block_no_verify", _bash(huge), monkeypatch) == 2
    # 上限の内側は従来どおり通る（拾いすぎの対照）
    assert command_exceeds_scan_budget("echo " + "A" * 1000) is False


def test_scan_budget_is_measured_in_bytes_not_characters() -> None:
    """マルチバイト文字でもバイト数で判定すること。

    文字数で数えると、日本語 1 文字 = 3 バイトの入力が上限の 3 倍まで通り、
    走査コストは 9 倍になる。
    """
    from claq.hooks.hook_common import MAX_COMMAND_BYTES, command_exceeds_scan_budget

    multibyte = "あ" * (MAX_COMMAND_BYTES // 3 + 1)

    assert len(multibyte) < MAX_COMMAND_BYTES
    assert command_exceeds_scan_budget(multibyte) is True
