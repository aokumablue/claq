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

from ple4.hooks import (
    bash_config_protection,
    block_no_verify,
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
# ないので剥がしてはならない（ADR-0017 の除去しない条件 1 と同じ理由）。
_BYPASS_HEREDOC = ". /dev/stdin <<'EOF'\n{body}\nEOF"
_SOURCE_HEREDOC = "source /dev/stdin <<'EOF'\n{body}\nEOF"
# なお、クォートで包んだコマンド置換（``eval "$(cat <<'EOF' ...)"``）は全体が
# 1 トークンになるため検出できない。ADR-0002 が「コマンド置換・変数展開」を
# 非目標として明記している範囲であり、下の block 一覧には載せない。
_EVAL_HEREDOC = "eval $(cat <<'EOF'\n{body}\nEOF\n)"

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
    from ple4.hooks.hook_common import normalize_protected_name

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

    ADR-0018: cwd を動かすコマンドがあると相対パス解決が実行時の位置とずれるため、
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
