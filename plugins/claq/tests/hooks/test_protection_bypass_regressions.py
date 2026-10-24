"""保護フックの既知バイパスを block / allow の表で固定する。

`harness_audit` はフックの**存在**を測るだけで実効性を測らない
（`skills/maintain/SKILL.md` のステップ5 が明記）ため、`Security Guardrails` が
満点でも保護フックが素通りしうる。そのため実効性の回帰はこのファイルの
block / allow 一覧が担う。

`allow` 側を同じ表に置くのは、fail-closed へ寄せた判定が通常操作を巻き込んでいない
ことを同時に固定するため。片側だけ書くと「全部ブロックする」実装でも緑になる。

後半は保護ファイル名の全数検査（層1: 集合の全要素で deny を実測、層2: 集合の完全一致）。
語彙集合全般の固定は `test_protection_vocabulary_declarations.py` が担う。
共有する補助と期待値表は `_support.py` に置く。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from claq.hooks import bash_config_protection, config_protection
from claq.hooks.hook_common import normalize_protected_name

from ._support import (
    _EXPECTED_CONDITIONALLY_PROTECTED_FILES,
    _EXPECTED_DIRECTORY_CHANGE_COMMANDS,
    _EXPECTED_LINT_KEYS,
    _EXPECTED_LINT_SECTION_HEADERS,
    _EXPECTED_PROTECTED_FILES,
    _NO_VERIFY,
    _bash,
    _run,
    _write,
    _write_content,
)
from ._support import _repo_scope as _repo_scope

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


_PROTECTED_WRITE = "printf x > ruff.toml"

# (ラベル, フック名, payload) — すべて exit 2 でなければならない。
_BLOCKED_CASES = [
    # `#` の作用域は行末までであり入力末尾までではない。
    ("行コメント後の 2 行目", "block_no_verify", _bash(f"git status #\n{_NO_VERIFY}")),
    ("行コメント後の書き込み", "bash_config_protection", _bash(f"echo ok #\n{_PROTECTED_WRITE}")),
    ("語中の #", "block_no_verify", _bash(f"echo a#b && {_NO_VERIFY}")),
    ("引用符内の #", "block_no_verify", _bash('git commit -m "fix #1" --no-verify')),
    # セグメント内の書き込み先は 1 件ではない。
    ("repo 外の囮が先頭", "bash_config_protection", _bash("rm -f /tmp/ruff.toml ruff.toml")),
    ("リダイレクト 2 連", "bash_config_protection", _bash("printf x > notes.txt > ruff.toml")),
    # `cd` があると cwd 基準の repo スコープ判定が信用できない。
    ("cd 後の相対書き込み", "bash_config_protection", _bash("cd sub && printf x > ../ruff.toml")),
    ("cd の解決先不明", "bash_config_protection", _bash("cd - && printf x > ruff.toml")),
    # `-c` は結合短フラグでも成立する。
    ("bash -lc", "block_no_verify", _bash(f"bash -lc '{_NO_VERIFY}'")),
    ("bash -lc 書き込み", "bash_config_protection", _bash(f"bash -lc '{_PROTECTED_WRITE}'")),
    ("bash -c 書き込み", "bash_config_protection", _bash(f"bash -c '{_PROTECTED_WRITE}'")),
    ("sh -c 書き込み", "bash_config_protection", _bash(f"sh -c '{_PROTECTED_WRITE}'")),
    # `.` / `source` / `eval` は heredoc 本文を実行する。
    (". /dev/stdin", "block_no_verify", _bash(_BYPASS_HEREDOC.format(body=_NO_VERIFY))),
    ("source /dev/stdin", "block_no_verify", _bash(_SOURCE_HEREDOC.format(body=_NO_VERIFY))),
    ("eval $(cat)", "block_no_verify", _bash(_EVAL_HEREDOC.format(body=_NO_VERIFY))),
    (
        ". /dev/stdin 書き込み",
        "bash_config_protection",
        _bash(_BYPASS_HEREDOC.format(body=_PROTECTED_WRITE)),
    ),
    # フィールド値は文字列とは限らない。
    (
        "command が list",
        "block_no_verify",
        {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", _NO_VERIFY]}},
    ),
    # argv 配列は要素ごとにも見るが、結合した 1 コマンドとしても見る。要素単位でしか
    # 見ないと ``--no-verify`` も ``ruff.toml`` も単独では無害に読める。
    (
        "argv 配列に分解された --no-verify",
        "block_no_verify",
        {"tool_name": "shell", "tool_input": {"command": ["git", "commit", "--no-verify", "-m", "x"]}},
    ),
    (
        "argv 配列の sh -c に包まれた --no-verify",
        "block_no_verify",
        {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "git commit --no-verify"]}},
    ),
    (
        "コマンド dict の配列の後続要素の --no-verify",
        "block_no_verify",
        {"tool_name": "shell", "tool_input": [{"command": "ls"}, {"command": "git commit --no-verify"}]},
    ),
    (
        "argv 配列に分解された保護対象の削除",
        "bash_config_protection",
        {"tool_name": "shell", "tool_input": {"command": ["rm", "ruff.toml"]}},
    ),
    # 単独サロゲートはファイルシステム API で ``UnicodeEncodeError`` になる。解決不能な
    # トークンが 1 つ混ざるだけで、後続の保護対象書き込みの検査まで落としてはならない。
    (
        "単独サロゲートの囮トークン",
        "bash_config_protection",
        _bash("touch \ud800x; printf y > ruff.toml"),
    ),
    (
        "単独サロゲートを含む保護 path（Write）",
        "config_protection",
        _write("\ud800/.git/hooks/pre-commit"),
    ),
    ("file_path が list", "config_protection", _write(["ruff.toml"])),
    (
        "command が list（書き込み）",
        "bash_config_protection",
        {"tool_name": "Bash", "tool_input": {"command": ["sh", "-c", _PROTECTED_WRITE]}},
    ),
    # ツール名を特定できない payload を「対象外」と読み替えない。
    (
        "tool_name が非文字列",
        "config_protection",
        {"tool_name": 123, "tool_input": {"file_path": "ruff.toml", "content": "x"}},
    ),
    (
        "tool_name キーが別名",
        "config_protection",
        {"tool": "Write", "tool_input": {"file_path": "ruff.toml", "content": "x"}},
    ),
    (
        "未知のツール名",
        "config_protection",
        {"tool_name": "mcp__fs__write", "tool_input": {"file_path": "ruff.toml", "content": "x"}},
    ),
    (
        "tool_name 欠落（Bash）",
        "bash_config_protection",
        {"tool_input": {"command": _PROTECTED_WRITE}},
    ),
    # 保護対象の照合が大小を区別しない（APFS / NTFS は既定で大小無視なので
    # `Ruff.toml` への書込みは実体 `ruff.toml` に当たる）。
    ("Write 大文字混在", "config_protection", _write("Ruff.toml")),
    ("Write 全大文字", "config_protection", _write("RUFF.TOML")),
    ("rm 大文字混在", "bash_config_protection", _bash("rm Ruff.toml")),
    ("リダイレクト先が大文字", "bash_config_protection", _bash("printf x > RUFF.TOML")),
    ("保護 path の大文字", "config_protection", _write(".Git/Hooks/pre-commit")),
    ("保護 path の大文字（Bash）", "bash_config_protection", _bash("rm .GIT/HOOKS/pre-commit")),
    (
        "条件付き保護の大文字",
        "config_protection",
        _write_content("PyProject.toml", "[tool.ruff]\nignore = ['E501']"),
    ),
    (
        "package.json 専用照合の大文字",
        "config_protection",
        _write_content("Package.json", '{"eslintConfig": {}}'),
    ),
    ("tox.ini 専用照合の大文字", "config_protection", _write_content("TOX.ini", "commands = true")),
    # sed の GNU 長形式（短縮形を含む）と実行 wrapper。
    ("sed --in-place", "bash_config_protection", _bash("sed --in-place s/x/y/ ruff.toml")),
    ("sed --in-place=.bak", "bash_config_protection", _bash("sed --in-place=.bak s/x/y/ ruff.toml")),
    ("sed 長形式の短縮 --i", "bash_config_protection", _bash("sed --i s/x/y/ ruff.toml")),
    ("sed -ni 結合短形式", "bash_config_protection", _bash("sed -ni s/x/y/ ruff.toml")),
    ("timeout wrapper", "bash_config_protection", _bash("timeout 5 rm ruff.toml")),
    ("timeout 単位付き秒数", "bash_config_protection", _bash("timeout 1.5s rm ruff.toml")),
    ("nohup wrapper", "bash_config_protection", _bash("nohup rm ruff.toml")),
    ("nice wrapper", "bash_config_protection", _bash("nice rm ruff.toml")),
    ("nice -n の数値", "bash_config_protection", _bash("nice -n 10 rm ruff.toml")),
    ("stdbuf wrapper", "bash_config_protection", _bash("stdbuf -o0 rm ruff.toml")),
    ("wrapper 多重", "bash_config_protection", _bash("nohup nice timeout 5 rm ruff.toml")),
    # commit 前 mutation ガードの語彙も兄弟と同じ正規化を通す。
    # scan は変更前の作業ツリーを読むため、取りこぼしは未検査コミットになる。
    (
        "大文字の cp",
        "pre_bash_commit_quality",
        _bash("CP /tmp/evil.py app.py && git commit -m 'fix: x'"),
    ),
    ("cp.exe", "pre_bash_commit_quality", _bash("cp.exe /tmp/evil.py app.py && git commit -m 'fix: x'")),
    ("大文字の rm", "pre_bash_commit_quality", _bash("Rm old.py && git commit -m 'fix: x'")),
    ("大文字の tee", "pre_bash_commit_quality", _bash("echo x | TEE app.py && git commit -m 'fix: x'")),
    (
        "sed 長形式の in-place",
        "pre_bash_commit_quality",
        _bash("sed --in-place s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    (
        "perl の結合短形式",
        "pre_bash_commit_quality",
        _bash("perl -0pi -e s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    # フラグ無しでファイルを書き換えるエディタ。`ed` をフラグを要求する
    # `_INPLACE_EDIT_EXECUTABLES` に置くと、``ed app.py && git commit``
    # が mutation-before-commit ガードを素通りする。
    ("ed の commit 前編集", "pre_bash_commit_quality", _bash("ed app.py && git commit -m 'fix: x'")),
    ("ex の commit 前編集", "pre_bash_commit_quality", _bash("ex app.py && git commit -m 'fix: x'")),
    ("red の commit 前編集", "pre_bash_commit_quality", _bash("red app.py && git commit -m 'fix: x'")),
    (
        "sponge の commit 前編集",
        "pre_bash_commit_quality",
        _bash("echo x | sponge app.py && git commit -m 'fix: x'"),
    ),
    ("ED 大文字", "pre_bash_commit_quality", _bash("ED app.py && git commit -m 'fix: x'")),
    # 同じ語彙の穴は保護 config 側にも開きうる（``ed ruff.toml``）。
    ("ed で保護 config", "bash_config_protection", _bash("ed ruff.toml")),
    ("ex で保護 config", "bash_config_protection", _bash("ex .eslintrc")),
    ("sponge で保護 config", "bash_config_protection", _bash("echo x | sponge ruff.toml")),
    # `&>>`（stdout+stderr の追記）と `>&`（`&>` の別綴り）も保護 config への書き込み。
    ("&>> で保護 config", "bash_config_protection", _bash("echo x &>>ruff.toml")),
    ("&>> 空白あり", "bash_config_protection", _bash("echo x &>> ruff.toml")),
    (">& で保護 config", "bash_config_protection", _bash("echo x >&ruff.toml")),
    (">& 空白あり", "bash_config_protection", _bash("echo x >& ruff.toml")),
    ("2>& で保護 config", "bash_config_protection", _bash("echo x 2>&ruff.toml")),
    # commit 前の `&>>` と `>&file` は作業ツリーへの書き込み。`>&` の後ろが fd（数字のみ・`-`）なら
    # 複製・クローズで書き込みではない（allow 側の「対 >&2 の commit 前」）。
    ("&>> の commit 前変更", "pre_bash_commit_quality", _bash("echo x &>>f && git commit -m 'fix: x'")),
    ("&> の commit 前変更", "pre_bash_commit_quality", _bash("echo x &>f && git commit -m 'fix: x'")),
    ("> の commit 前変更", "pre_bash_commit_quality", _bash("echo x > f && git commit -m 'fix: x'")),
    (">&file の commit 前変更", "pre_bash_commit_quality", _bash("echo x >&f && git commit -m 'fix: x'")),
    (">&2x の commit 前変更", "pre_bash_commit_quality", _bash("echo x >&2x && git commit -m 'fix: x'")),
    ("wrapper 越しの ed", "bash_config_protection", _bash("timeout 5 ed ruff.toml")),
    # `touch` は内容を変えないが、保護対象を**空で新規作成**できる。空の
    # `.eslintrc` は ESLint の上位カスケード探索を止めるため実質的な無効化になる。
    ("touch で保護 config", "bash_config_protection", _bash("touch ruff.toml")),
    ("touch の大小混在", "bash_config_protection", _bash("touch .Eslintrc")),
    ("touch の値付きオプション", "bash_config_protection", _bash("touch -t 202601010000 ruff.toml")),
    ("wrapper 越しの touch", "bash_config_protection", _bash("timeout 5 touch ruff.toml")),
    # bash では有効だが `shlex` が解析できない `$'\\''` の直後に、区切り記号が語へ密着した
    # 形。空白分割へ倒れても区切りで割れていないと git 起動・書込み先が 1 語に埋もれる。
    ("解析不能の後の ; 密着 git", "block_no_verify", _bash("echo $'\\'';git commit --no-verify")),
    ("解析不能の後の && 密着 git", "block_no_verify", _bash("echo $'\\''&&git commit --no-verify")),
    ("解析不能の後の || 密着 git", "block_no_verify", _bash("echo $'\\''||git commit --no-verify")),
    ("解析不能の後の | 密着 git", "block_no_verify", _bash("echo $'\\''|git commit --no-verify")),
    ("解析不能の後の & 密着 git", "block_no_verify", _bash("echo $'\\''&git commit --no-verify")),
    ("解析不能の後の括弧 git", "block_no_verify", _bash("echo $'\\'';(git commit --no-verify)")),
    ("解析不能の後の hooksPath", "block_no_verify", _bash("echo $'\\'';git config core.hooksPath /x")),
    # 区切りを割っても、解析不能のまま語に引用符が残る形は構造では読めない。
    # `block_no_verify` だけは、解析不能のときに限り生文字列の `--no-verify` / hooksPath で止める。
    ("解析不能の後の引用付き git", "block_no_verify", _bash("echo $'\\'';\"git\" commit --no-verify")),
    ("解析不能の後の引用付きフラグ", "block_no_verify", _bash("echo $'\\'';git commit \"--no-verify\"")),
    ("解析不能の後の連結引用 git", "block_no_verify", _bash("echo $'\\'';g''it commit --no-verify")),
    ("解析不能の後の引用付き hooksPath", "block_no_verify", _bash("echo $'\\'';git -c \"core.hooksPath=/x\" commit")),
    ("解析不能の後の大文字 hooksPath", "block_no_verify", _bash("echo $'\\'';git -c core.HOOKSPATH=/x commit")),
    ("解析不能の後のコマンド置換", "block_no_verify", _bash("echo $'\\'';`git commit --no-verify`")),
    ("解析不能の後の sh -c", "block_no_verify", _bash("echo $'\\'';sh -c 'git commit --no-verify'")),
    ("解析不能の後の別引用", "block_no_verify", _bash("echo $'\\'';git commit --no-verify -m \"a;b")),
    ("解析不能の後の > 密着", "bash_config_protection", _bash("echo $'\\'' >ruff.toml")),
    ("解析不能の後の >> 密着", "bash_config_protection", _bash("echo $'\\'' >>ruff.toml")),
    ("解析不能の後の >| 密着", "bash_config_protection", _bash("echo $'\\'' >|ruff.toml")),
    ("解析不能の後の &> 密着", "bash_config_protection", _bash("echo $'\\'' &>ruff.toml")),
    ("解析不能の後の 2> 密着", "bash_config_protection", _bash("echo $'\\'' 2>ruff.toml")),
    ("解析不能の後の 2>> 密着", "bash_config_protection", _bash("echo $'\\'' 2>>ruff.toml")),
    ("解析不能の後の &>> 密着", "bash_config_protection", _bash("echo $'\\'' &>>ruff.toml")),
    ("解析不能の後の >& 密着", "bash_config_protection", _bash("echo $'\\'' >&ruff.toml")),
    # fd 複製の `&` で後続の語を別セグメントへ切り離さない。
    ("解析不能の後の 2>&1 越しの rm", "bash_config_protection", _bash("echo $'\\'' ; rm -f 2>&1 ruff.toml")),
    ("解析不能の後の >&2 越しの rm", "bash_config_protection", _bash("echo $'\\'' ; rm -f >&2 ruff.toml")),
    ("解析不能の後の <&3 越しの rm", "bash_config_protection", _bash("echo $'\\'' ; rm -f <&3 ruff.toml")),
    ("解析不能の後の 2>&1 越しの commit", "block_no_verify", _bash("echo $'\\'' ; git commit 2>&1 -n")),
    ("解析不能の後の >&2 越しの commit", "block_no_verify", _bash("echo $'\\'' ; git commit >&2 -n")),
    ("解析不能の後の ; 密着 rm", "bash_config_protection", _bash("echo $'\\'';rm ruff.toml")),
    ("解析不能の後の && 密着 rm", "bash_config_protection", _bash("echo $'\\''&&rm ruff.toml")),
    ("解析不能の後の || 密着 rm", "bash_config_protection", _bash("echo $'\\''||rm ruff.toml")),
    ("解析不能の後の | 密着 tee", "bash_config_protection", _bash("echo $'\\''|tee ruff.toml")),
    # 解析不能の後は引用符がトークンに残るため、引用符付きの保護名・コマンド名が照合を抜けていた。
    ("解析不能の後の二重引用符付き rm", "bash_config_protection", _bash("echo $'\\''; rm \"ruff.toml\"")),
    ("解析不能の後の単一引用符付き rm", "bash_config_protection", _bash("echo $'\\''; rm 'ruff.toml'")),
    ("解析不能の後の引用符付きリダイレクト先", "bash_config_protection", _bash("echo $'\\''; printf x >\"ruff.toml\"")),
    ("解析不能の後の引用符付き sed -i", "bash_config_protection", _bash("echo $'\\''; sed -i 's/a/b/' \"ruff.toml\"")),
    ("解析不能の後の引用符付き tee", "bash_config_protection", _bash("echo $'\\''; echo x | tee \"ruff.toml\"")),
    ("解析不能の後の引用符付き git と -n", "block_no_verify", _bash("echo $'\\''; \"git\" commit -n -m a")),
    ("解析不能の後の連結引用 git と -n", "block_no_verify", _bash("echo $'\\''; g''it commit -n -m a")),
    ("解析不能でない引用符付き git と -n（対照）", "block_no_verify", _bash("\"git\" commit -n")),
    ("解析不能でない引用符付きフラグ（対照）", "block_no_verify", _bash("git commit \"--no-verify\"")),
    ("解析不能でない引用符付き hooksPath（対照）", "block_no_verify", _bash("git -c \"core.hooksPath=/x\" commit -m a")),
    # 受容済みの誤検出: 引用符の対応を取らない空白分割は、引用符内の区切り・リダイレクトも
    # 実行構造として読む。解析不能な入力に限り、引用文中の保護名の書込み様の語や
    # `git commit -n` 様の語を deny する（引用符を整合させれば通る。ADR の該当節）。
    ("受容 解析不能の後の引用文中の ; rm", "bash_config_protection", _bash("echo $'\\''; echo \"a; rm ruff.toml\"")),
    ("受容 解析不能の後の引用文中の >", "bash_config_protection", _bash("echo $'\\''; echo \"x > ruff.toml\"")),
    ("受容 解析不能の後の引用文中の git commit -n", "block_no_verify", _bash("echo $'\\''; echo \"git commit -n\"")),
    ("受容 閉じない引用符の中の git commit -n", "block_no_verify", _bash("grep \"git commit -n README.md")),
    # 陽性対照（fail-closed 化で失われていないこと）。
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
    (
        "argv 配列の通常の commit",
        "block_no_verify",
        {"tool_name": "shell", "tool_input": {"command": ["git", "commit", "-m", "x"]}},
    ),
    (
        "argv 配列の保護対象でない書き込み",
        "bash_config_protection",
        {"tool_name": "shell", "tool_input": {"command": ["rm", "notes.txt"]}},
    ),
    ("単独サロゲートだけで保護対象なし", "bash_config_protection", _bash("touch \ud800x")),
    ("保護対象でない書き込み", "bash_config_protection", _bash("printf x > notes.txt")),
    ("保護対象の読み取り", "bash_config_protection", _bash("cat ruff.toml")),
    ("repo 外への書き込み", "bash_config_protection", _bash("printf x > /tmp/ruff.toml")),
    ("cd はあるが保護対象なし", "bash_config_protection", _bash("cd sub && printf x > notes.txt")),
    ("ラッパー内も保護対象なし", "bash_config_protection", _bash("bash -lc 'printf x > notes.txt'")),
    ("データ heredoc（書き込み）", "bash_config_protection", _bash(_DATA_HEREDOC.format(body=_PROTECTED_WRITE))),
    ("Read は書き込みではない", "config_protection", {"tool_name": "Read", "tool_input": {"file_path": "ruff.toml"}}),
    ("Grep は書き込みではない", "config_protection", {"tool_name": "Grep", "tool_input": {"file_path": "ruff.toml"}}),
    ("保護対象でない Write", "config_protection", _write("notes.txt")),
    # 解析不能（引用符不整合）でも、区切りを割るだけで「危険語が見えたら deny」にはしない。
    # 読み取りコマンドは従来どおり通す。
    ("対 解析不能の後の読み取り", "bash_config_protection", _bash("echo $'\\'' && cat ruff.toml")),
    ("対 解析不能の後の読み取り（block_no_verify）", "block_no_verify", _bash("echo $'\\'' && cat ruff.toml")),
    ("対 解析不能の grep", "bash_config_protection", _bash("grep 'a ruff.toml")),
    ("対 解析不能の grep（block_no_verify）", "block_no_verify", _bash("grep 'a ruff.toml")),
    # 生文字列の deny は「解析不能」かつ「`--no-verify` / hooksPath が見える」ときだけ。
    # 解析できた入力の引数・引用文は従来どおり通す。
    ("対 解析できた入力の --no-verify 引数", "block_no_verify", _bash("grep -- '--no-verify' README.md")),
    ("対 解析できた入力の hooksPath 引数", "block_no_verify", _bash("grep hooksPath README.md")),
    ("対 解析不能で危険語なしの commit", "block_no_verify", _bash("echo $'\\'';git commit -m \"x\"")),
    ("対 解析不能の後の密着 cat", "bash_config_protection", _bash("echo $'\\'';cat ruff.toml")),
    ("対 解析不能の後の通常 commit", "block_no_verify", _bash("echo $'\\'';git commit -m x")),
    ("対 解析不能の後の <  読み取り", "bash_config_protection", _bash("echo $'\\'' <ruff.toml")),
    # --- 上の block 追加と対になる陰性対照 ---
    # 大小無視は「保護対象の綴り違い」だけに効き、無関係なファイルは巻き込まない。
    ("対 大文字混在の無関係 Write", "config_protection", _write("App.py")),
    ("対 大文字混在の無関係 rm", "bash_config_protection", _bash("rm App.py")),
    ("対 名前が似た別ファイル", "config_protection", _write("Ruff.toml.bak")),
    (
        "対 条件付き保護の非 lint 変更",
        "config_protection",
        _write_content("PyProject.toml", '[project]\nversion = "1.2.3"'),
    ),
    # in-place でない sed と、実行位置に無い wrapper 語は allow のまま。
    ("対 sed --expression", "bash_config_protection", _bash("sed --expression s/x/y/ app.py")),
    ("対 保護対象への非 in-place sed", "bash_config_protection", _bash("sed --expression s/x/y/ ruff.toml")),
    # 区切りの `--` は長形式ではないので in-place ではない（名前部分が空）。
    ("対 区切りの --", "bash_config_protection", _bash("sed -- s/x/y/ ruff.toml")),
    # wrapper 語彙を足しても、非実行位置の言及は実行位置にならない。
    ("対 echo tee 言及", "bash_config_protection", _bash("echo tee pyproject.toml")),
    ("対 echo の中の wrapper 列", "bash_config_protection", _bash("echo timeout 5 rm ruff.toml")),
    # wrapper の先が読み取り専用なら書き込み先は現れない。
    ("対 timeout 経由の cat", "bash_config_protection", _bash("timeout 5 cat ruff.toml")),
    # 正規化を足しても、変更しないコマンドは mutation とみなさない。
    ("対 大文字の cat", "pre_bash_commit_quality", _bash("Cat app.py && git commit -m 'fix: x'")),
    ("対 単独 commit", "pre_bash_commit_quality", _bash("git commit -m 'fix: x'")),
    (
        "対 in-place でない sed（--expression）",
        "pre_bash_commit_quality",
        _bash("sed --expression s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    ("対 commit 無しの mutation", "pre_bash_commit_quality", _bash("CP /tmp/evil.py app.py")),
    # 常時 mutation 分類を足しても、非実行位置の言及・保護対象でない引数・
    # フラグを要求する側の語彙（`sed` は `-i` 無しなら標準出力）は allow のまま。
    ("対 commit 無しの ed", "pre_bash_commit_quality", _bash("ed app.py")),
    (
        "対 in-place でない sed",
        "pre_bash_commit_quality",
        _bash("sed s/a/b/ app.py && git commit -m 'fix: x'"),
    ),
    ("対 保護対象でない ed", "bash_config_protection", _bash("ed app.py")),
    ("対 非実行位置の ed", "bash_config_protection", _bash("echo ed ruff.toml")),
    ("対 保護対象の読み取り（ex 名の別語）", "bash_config_protection", _bash("grep -rn ex ruff.toml")),
    # `touch` を書込み verb にしても、保護対象でないファイル・repo 外・
    # 非実行位置は allow のまま。「全部ブロックする」実装をここで落とす。
    # `>&` を書き込み演算子にしても、fd 複製（保護名でない後続）は allow のまま。
    ("対 >&2", "bash_config_protection", _bash("echo x >&2")),
    ("対 2>&1", "bash_config_protection", _bash("ls 2>&1")),
    ("対 >&2 後の git status", "bash_config_protection", _bash("echo hi >&2 && git status")),
    ("対 >&- (fd を閉じる)", "bash_config_protection", _bash("echo x >&-")),
    ("対 保護名でない &>>", "bash_config_protection", _bash("echo x &>>notes.txt")),
    ("対 解析不能の後の >&2", "bash_config_protection", _bash("echo $'\\'' >&2")),
    ("対 &>> の commit 無し", "pre_bash_commit_quality", _bash("echo x &>>f")),
    ("対 >&2 の commit 前", "pre_bash_commit_quality", _bash("echo x >&2 && git commit -m 'fix: x'")),
    ("対 2>&1 の commit 前", "pre_bash_commit_quality", _bash("cmd 2>&1 && git commit -m 'fix: x'")),
    ("対 >&- の commit 前", "pre_bash_commit_quality", _bash("cmd >&- && git commit -m 'fix: x'")),
    ("対 1>&2 の commit 前", "pre_bash_commit_quality", _bash("cmd 1>&2 && git commit -m 'fix: x'")),
    ("対 touch app.py", "bash_config_protection", _bash("touch app.py")),
    ("対 touch README.md", "bash_config_protection", _bash("touch README.md")),
    ("対 repo 外の touch", "bash_config_protection", _bash("touch /tmp/ruff.toml")),
    ("対 非実行位置の touch", "bash_config_protection", _bash("echo touch ruff.toml")),
    # 解析不能な入力でも、引用符付きの読み取りや保護名でない書き込みは巻き込まない。
    ("解析不能の後の引用符付き cat", "bash_config_protection", _bash("echo $'\\''; cat \"ruff.toml\"")),
    ("解析不能の後の保護名でない引用符付き rm", "bash_config_protection", _bash("echo $'\\''; rm \"notes.txt\"")),
    ("解析不能の後の引数位置の保護名", "bash_config_protection", _bash("echo $'\\''; echo \"ruff.toml\"")),
    ("閉じない引用符内の保護名を読む", "bash_config_protection", _bash("grep \"a ruff.toml")),
    ("解析不能の後の引用符付き commit", "block_no_verify", _bash("echo $'\\''; git commit -m \"x\"")),
]


# ハーネスごとの payload 形状（`lib/harness.py` の `_TOOL_NAME_MAP` /
# `INPUT_CONTAINER_KEYS` に対応）。ツール名を特定できない payload を検査側へ倒す
# 判定が、Claude Code 以外のハーネスの通常操作を巻き込んでいないことを
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


# 期待 exit code を付けて 1 本の表にまとめる。ids はラベル（3 表を通して一意）。
_VERDICT_CASES = [
    *((label, hook, payload, 2) for label, hook, payload in _BLOCKED_CASES),
    *((label, hook, payload, 0) for label, hook, payload in _ALLOWED_CASES),
    *_HARNESS_CASES,
]


@pytest.mark.parametrize(
    ("label", "hook", "payload", "expected"), _VERDICT_CASES, ids=[case[0] for case in _VERDICT_CASES]
)
def test_hook_verdict(
    label: str, hook: str, payload: dict[str, Any], expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既知バイパスは exit 2・通常操作は exit 0 になること（他ハーネスの payload 形状を含む）。"""
    assert _run(hook, payload, monkeypatch) == expected, label


# --------------------------------------------------------------------------
# 保護語彙の全数検査
#
# 上の表は語彙の**サンプル**しか流さない。保護集合は複数行にまたがっても 1 statement
# なので、要素を 1 個消しても実行行は変わらず `fail_under=100` では検出できない。
# 検査は 2 層に分け、どちらも必須:
#
#   層1（列挙）: 期待値の全要素を parametrize の入力にし、deny を実測する。
#       「集合には載っているが実際には効いていない」要素を検出する。
#   層2（固定）: 集合の内容を完全一致で表明する。層1 だけだと、要素を削除した
#       瞬間にその parametrize ケース自体が消えるため**緑のまま**通ってしまう。
#
# 保護語彙は外部に突き合わせ先を持たないため、層2 は意図的に定義をテストへ複製する。
# --------------------------------------------------------------------------

# 保護語彙に**似ているが載っていない**名前（拡張子だけを変えてある）。層1 の陰性対照。
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
# `package.json` は TOML/INI ではないため専用キー（`eslintConfig` / `prettier`）を signal に使う
# —— 一律 `[tool.ruff]` を流すと「テストの取り違え」で赤くなる。
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


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (config_protection.PROTECTED_FILES, _EXPECTED_PROTECTED_FILES),
        (config_protection.CONDITIONALLY_PROTECTED_FILES, _EXPECTED_CONDITIONALLY_PROTECTED_FILES),
        (bash_config_protection._DIRECTORY_CHANGE_COMMANDS, _EXPECTED_DIRECTORY_CHANGE_COMMANDS),
        (config_protection._LINT_SECTION_HEADERS, _EXPECTED_LINT_SECTION_HEADERS),
        (config_protection._LINT_KEYS, _EXPECTED_LINT_KEYS),
    ],
    ids=[
        "PROTECTED_FILES",
        "CONDITIONALLY_PROTECTED_FILES",
        "_DIRECTORY_CHANGE_COMMANDS",
        "_LINT_SECTION_HEADERS",
        "_LINT_KEYS",
    ],
)
def test_protected_vocabulary_is_exactly_as_declared(actual: Any, expected: Any) -> None:
    """層1 が入力にする保護語彙の内容が完全一致で固定されていること（層2）。"""
    assert actual == expected


def test_folded_protected_sets_cover_every_declared_name() -> None:
    """判定に使う畳み済み集合が、宣言側の全要素を漏れなく・余計なく含むこと。

    判定は `*_FOLDED` 側でのみ行われる。期待値は実装の集合ではなく `_EXPECTED_*` から組み立てる
    （実装から同じ式で作ると、宣言側と畳み込み側が揃って壊れた場合に緑のまま通る）。
    """
    for expected, folded in (
        (_EXPECTED_PROTECTED_FILES, config_protection.PROTECTED_FILES_FOLDED),
        (_EXPECTED_CONDITIONALLY_PROTECTED_FILES, config_protection.CONDITIONALLY_PROTECTED_FILES_FOLDED),
    ):
        assert folded == frozenset(normalize_protected_name(name) for name in expected)
        assert len(folded) == len(expected)


@pytest.mark.parametrize("name", sorted(_EXPECTED_PROTECTED_FILES))
def test_every_protected_file_is_denied_on_write(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PROTECTED_FILES` の全要素が Write 経路で、元の綴りでも大文字綴りでも deny されること（層1）。

    APFS / NTFS では `RUFF.TOML` が `ruff.toml` そのものを指すため、大小無視も全要素で確認する。
    """
    assert _run("config_protection", _write(name), monkeypatch) == 2
    assert _run("config_protection", _write(name.upper()), monkeypatch) == 2


@pytest.mark.parametrize("name", sorted(_EXPECTED_PROTECTED_FILES))
def test_every_protected_file_is_denied_on_bash_write(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PROTECTED_FILES` の全要素が Bash 経路（リダイレクト・削除・作成）で deny されること（層1）。

    Write だけでは `bash_config_protection` の取りこぼしに気付けない。`touch` を含めるのは、
    空ファイルの新規作成も linter の上位カスケード探索を止めて上書きと同じ弱体化になるため。
    """
    for command in (f"printf x > {name}", f"rm {name}", f"touch {name}"):
        assert _run("bash_config_protection", _bash(command), monkeypatch) == 2, command


@pytest.mark.parametrize("name", _NON_MEMBER_CONFIG_NAMES)
def test_non_member_config_name_is_allowed(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """保護語彙に載っていない類似名は allow のままであること（「全部 deny する」実装を落とす陰性対照）。"""
    assert name not in _EXPECTED_PROTECTED_FILES
    assert _run("config_protection", _write(name), monkeypatch) == 0


@pytest.mark.parametrize("name", sorted(_EXPECTED_CONDITIONALLY_PROTECTED_FILES))
def test_every_conditionally_protected_file_denies_only_lint_signals(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """条件付き保護の全要素が、lint 兆候ありで deny・なしで allow になること（層1）。"""
    with_signal, without_signal = _CONDITIONAL_SIGNALS[name]
    assert _run("config_protection", _write_content(name, with_signal), monkeypatch) == 2
    assert _run("config_protection", _write_content(name, without_signal), monkeypatch) == 0


@pytest.mark.parametrize("command", sorted(_EXPECTED_DIRECTORY_CHANGE_COMMANDS))
def test_every_directory_change_command_forces_unconditional_deny(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_DIRECTORY_CHANGE_COMMANDS` の全要素が repo スコープ判定を放棄させること（層1）。

    cwd を動かすと相対パス解決が実行時の位置とずれるため、保護対象 basename のヒットをそのまま deny する。
    repo 内のパスでは「repo 内だから deny」と区別できないので、cd 系が無ければ allow になる
    **repo ルート外**の書き込み先を対照として先に固定する。
    """
    outside_repo = "printf x > ../ruff.toml"
    assert _run("bash_config_protection", _bash(outside_repo), monkeypatch) == 0
    assert _run("bash_config_protection", _bash(f"{command} sub && {outside_repo}"), monkeypatch) == 2


@pytest.mark.parametrize("header", _EXPECTED_LINT_SECTION_HEADERS)
def test_every_lint_section_header_is_detected(header: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_LINT_SECTION_HEADERS` の全要素が条件付き保護を発火させること（層1・前方一致なので `[tool.ruff` 等は `.sub]` を足す）。"""
    line = header if header.endswith("]") else f"{header}.sub]"
    assert _run("config_protection", _write_content("setup.cfg", f"{line}\nx = 1\n"), monkeypatch) == 2


@pytest.mark.parametrize("key", _EXPECTED_LINT_KEYS)
def test_every_lint_key_is_detected(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_LINT_KEYS` の全要素が見出し無しの値行編集でも条件付き保護を発火させること（層1）。"""
    assert _run("config_protection", _write_content("pyproject.toml", f"{key} = 1\n"), monkeypatch) == 2


@pytest.mark.parametrize("word", ["description", "requires-python", "name", "[project]", "[metadata]"])
def test_non_lint_section_or_key_is_allowed(word: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """lint 語彙でない見出し・キーは条件付き保護を発火させないこと（層1 の陰性対照）。"""
    body = f"{word}\nx = 1\n" if word.startswith("[") else f"{word} = 1\n"
    assert _run("config_protection", _write_content("pyproject.toml", body), monkeypatch) == 0
