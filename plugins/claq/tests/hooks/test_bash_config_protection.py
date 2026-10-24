"""`bash_config_protection` フックのテスト（Bash 経由の設定ファイル直接書き換え保護）。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from claq.hooks import bash_config_protection

from ._support import _bash
from ._support import _home_outside_repo as _home_outside_repo
from ._support import _repo_scope as _repo_scope

_DENIED = [
    # リダイレクト（数字プレフィックス付き等を含む）・tee・sed -i・heredoc・複合コマンド。
    "printf x > pyproject.toml",
    "printf x >> pyproject.toml",
    "echo '[testenv]' > tox.ini",
    "cat file.txt > .eslintrc.json",
    "tee pyproject.toml <<< 'x'",
    "tee -a pyproject.toml <<< 'x'",
    "echo x | tee package.json",
    "tee notes.txt pyproject.toml <<< 'x'",
    "sed -i 's/a/b/' pyproject.toml",
    "sed -i '' pyproject.toml",
    "sed -i.bak 's/a/b/' setup.cfg",
    "cat <<'EOF' > pyproject.toml\nx\nEOF",
    "true && printf x > pyproject.toml",
    "echo x &> pyproject.toml",
    "echo x >| pyproject.toml",
    "echo x 1> pyproject.toml",
    "echo x 2>> pyproject.toml",
    "echo x &>> pyproject.toml",
    "echo x >& pyproject.toml",
    # 書き込み先が引数位置に明示されるコマンド群。-f 無しの新規 symlink も、以後その名前を
    # 読む処理をリンク先へ差し替えるため実質的な書き込み。
    "cp src.py pyproject.toml",
    "mv src.py pyproject.toml",
    "install -m 644 src.py pyproject.toml",
    "ln -f /dev/null pyproject.toml",
    "ln -s weak.toml pyproject.toml",
    "ln --force /dev/null pyproject.toml",
    "dd if=/dev/zero of=pyproject.toml",
    "perl -i -pe s/a/b/ pyproject.toml",
    "perl -0pi -e 1 pyproject.toml",
    # 削除・切り詰め・移動元。リンタ設定を消せばルールごと無効化できるため上書きと同じ強さの弱体化。
    "rm pyproject.toml",
    "rm -f pyproject.toml",
    "rm -rf pyproject.toml",
    "unlink pyproject.toml",
    "shred pyproject.toml",
    "truncate -s 0 pyproject.toml",
    "mv pyproject.toml /tmp/backup",
    "sudo rm pyproject.toml",
    "env rm pyproject.toml",
    # wrapper（sudo/env/command）を挟んでも実行位置を特定する。cp/mv/install と ln -f も
    # `_command_index` を通す（素の `segment[0]` を見ると `sudo cp ... pyproject.toml` が素通りする）。
    "sudo cp /tmp/x pyproject.toml",
    "sudo install /tmp/x pyproject.toml",
    "sudo ln -f /dev/null pyproject.toml",
    "env cp /tmp/x pyproject.toml",
    "env X=1 tee pyproject.toml",
    "sudo tee pyproject.toml",
    "command tee pyproject.toml",
    "sudo -u root tee pyproject.toml",
    "env -u FOO tee pyproject.toml",
    "env sed -i 's/a/b/' pyproject.toml",
    "sudo dd if=/dev/zero of=pyproject.toml",
    # 未知の wrapper オプションは 1 トークンだけ読み飛ばして実行対象の探索を続ける。
    "sudo -n tee pyproject.toml",
    # 単独サロゲートで解決が ValueError になっても、保護名に一致する以上は deny する（fail-closed。
    # docs/adr/hook-failure-direction.md）。allow にすると単独サロゲートを混ぜるだけで保護を外せる。
    "rm \ud800/.git/hooks/pre-commit",
    "printf x > \ud800/.git/hooks/pre-commit",
    "printf x > \ud800/ruff.toml",
    "rm \ud800/pyproject.toml",
    # `config_protection` の path ベース保護（`.git/hooks/`）も Bash 経路へ伝播する。取りこぼすと
    # 同じ書込みが Write では exit 2、Bash では exit 0 という非対称になる。
    "echo x > .git/hooks/pre-commit",
    "printf x >> .git/hooks/pre-push",
    "sed -i 's/a/b/' .git/hooks/pre-commit",
    "echo x | tee .git/hooks/commit-msg",
    "rm .git/hooks/pre-commit",
    # 保護ディレクトリ自身を消す・退避する操作。配下ファイルが deny でディレクトリ操作が allow だと、
    # 同じ結果（フックが走らない状態）に対する非対称が残る。
    "rm -rf .git/hooks",
    "mv .git/hooks .git/hooks.off",
    "ln -s /dev/null .git/hooks",
    # 権限変更。中身を書き換えなくても実行ビットや読み取り権限を落とせば検査は無効化できる。
    "chmod -x .git/hooks/pre-commit",
    "chmod 000 .eslintrc.json",
    "chmod a-r pyproject.toml",
    "chown nobody .git/hooks/pre-commit",
    "chgrp nogroup .eslintrc.json",
    "chflags uchg pyproject.toml",
    "sudo chmod -x .git/hooks/pre-commit",
    "chmod -R 000 .git/hooks",
]

_ALLOWED = [
    # 書き込みではない・保護対象でない。fd 複製の `>&` は保護名が続かない限り書き込みではない。
    "printf x > notes.txt",
    "echo x >&2",
    "ls 2>&1",
    "cat pyproject.toml",
    "echo pyproject.toml",
    "grep -n commands tox.ini",
    "sed -n '1,5p' pyproject.toml",
    "tee",
    "sed -i",
    "",
    "ln -s a.txt b.txt",
    # 弱体化でない削除系（非保護対象・複製は元が残る・ファイル指定が無い）。
    "rm notes.txt",
    "cp pyproject.toml /tmp/backup",
    "rm",
    "truncate -s 0",
    # リポジトリ外の同名ファイル。`~` は展開してから判定する（展開しないと cwd 相対の
    # `<repo_root>/~/outside/...` と読まれ false positive になる）。
    "printf x > /definitely-outside-the-repo/pyproject.toml",
    "printf x > ~/outside/pyproject.toml",
    # 拡張検出（perl/cp/mv/ln/dd）の non-match（in-place 無し・引数無し・非保護対象）。
    "perl -pe s/a/b/ pyproject.toml",
    "perl -i -pe s/a/b/ notes.txt",
    "cp -v",
    "mv --",
    "ln -f",
    "dd if=/dev/zero of=notes.txt",
    # tee/sed/perl/dd/of= が実行位置ではなくただの引数文字列として現れるだけ。
    "echo tee pyproject.toml",
    "echo of=pyproject.toml",
    "echo sed -i pyproject.toml",
    "echo perl -i pyproject.toml",
    "printf 'tee pyproject.toml\\n'",
    "grep -l 'dd of=pyproject.toml' notes.txt",
    # `_command_index` が実行対象を特定できない（代入/wrapper/オプションで終わる）。
    "env",
    "FOO=bar",
    "sudo -u",
    # 保護名に一致しない解決不能トークン。
    "rm \ud800/notes.txt",
    "printf x > \ud800/src/main.py",
    # `.git/hooks/` 配下でない同名・類似パス。
    "echo x > src/hooks/pre-commit",
    "echo x > .git/info/exclude",
    "echo x > README.md",
    "echo x > hooks/pre-commit",
    # 保護対象でないパスへの権限変更。
    "chmod +x scripts/deploy.sh",
    "chmod 644 README.md",
    "chmod -x src/hooks/pre-commit",
    "chown me:me .git/info/exclude",
    "echo chmod -x .git/hooks/pre-commit",
    # `~/.claq/logs/` 以外の home 配下・類似パス。
    "echo x > logs/bg-1.log",
    "echo x > ~/.claq/mem.db",
    "echo x > ~/.claq/session-data/x.md",
    "echo x > ~/.config/claq/logs/x.log",
]


@pytest.mark.parametrize("command", _DENIED)
def test_protected_write_is_denied(command: str) -> None:
    """保護対象への書き込み・削除・権限変更を検出する。"""
    assert bash_config_protection.find_protected_write(command) is not None


@pytest.mark.parametrize("command", _ALLOWED)
def test_non_protected_command_is_allowed(command: str) -> None:
    """保護対象への書き込みでないコマンドは通す。"""
    assert bash_config_protection.find_protected_write(command) is None


@pytest.mark.parametrize(
    ("command", "denied"),
    [("printf x > pyproject.toml", False), ("rm \ud800/.git/hooks/pre-commit", True)],
    ids=["resolvable-allows", "unresolvable-denies"],
)
def test_undetermined_repo_root_denies_only_unresolvable_token(
    command: str, denied: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ルート不明なら allow に倒すが、解決不能な保護名トークンはルートの有無で結論を変えない。"""
    monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: None)

    assert (bash_config_protection.find_protected_write(command) is not None) is denied


def test_resolve_repo_root_not_called_without_write_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """保護対象 basename が出現しない大多数の呼び出しでは resolve_repo_root を呼ばない。"""
    called = {"n": 0}

    def _tracking_resolve() -> Path:
        """呼び出し回数を数える。"""
        called["n"] += 1
        return Path("/repo")

    monkeypatch.setattr(bash_config_protection, "resolve_repo_root", _tracking_resolve)

    assert bash_config_protection.find_protected_write("ls -la && echo hello") is None
    assert called["n"] == 0


def test_denies_protected_name_when_resolution_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """symlink ループで `Path.resolve` が RuntimeError になる実装（Python 3.12 の strict=False）でも deny する。"""

    def _looping_resolve(self: Path, strict: bool = False) -> Path:  # noqa: ARG001
        """ループ検出の RuntimeError を投げる。"""
        raise RuntimeError("Symlink loop")

    monkeypatch.setattr(bash_config_protection.Path, "resolve", _looping_resolve)

    assert bash_config_protection.find_protected_write("printf x > ruff.toml") is not None


def test_denies_protected_write_through_symlink_loop(tmp_path: Path) -> None:
    """実際の symlink ループ越しの保護パスへの書き込みを deny する。"""
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")

    assert bash_config_protection.find_protected_write("rm a/.git/hooks/pre-commit") is not None
    assert bash_config_protection.find_protected_write("printf x > a/ruff.toml") is not None


def test_allows_resolvable_protected_name_outside_repo_root(tmp_path_factory: pytest.TempPathFactory) -> None:
    """解決できてリポジトリ外のパスは、保護名に一致しても allow のまま。"""
    outside = tmp_path_factory.mktemp("outside")

    assert bash_config_protection.find_protected_write(f"printf x > {outside}/ruff.toml") is None
    assert bash_config_protection.find_protected_write(f"rm {outside}/.git/hooks/pre-commit") is None


def test_within_repo_root_returns_false_when_repo_root_resolve_fails(tmp_path: Path) -> None:
    """書き込み先の解決は成功していても repo_root.resolve() が失敗すれば False を返す。"""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    class _BoomPath(Path):
        """resolve が必ず失敗するパス。"""

        def resolve(self, strict: bool = False) -> Path:  # noqa: ARG002
            """repo ルートの解決失敗を模す。"""
            raise OSError("repo root unavailable")

    assert bash_config_protection._within_repo_root(tmp_path / "pyproject.toml", _BoomPath(tmp_path)) is False


@pytest.mark.parametrize(
    ("raw_input", "denied"),
    [
        # トークン化経路の書き込み先抽出（`_EXECUTED_COMMAND_TARGET_RULES`）と同じ verb 集合を見る。
        # JSON の壊れ方だけで同じ入力の結論が変わるのは境界として説明できない。
        ("{broken rm ruff.toml", True),
        ("{broken unlink pyproject.toml", True),
        ("{broken shred tox.ini", True),
        ("{broken ln -s x ruff.toml", True),
        ("{broken cat ruff.toml", False),
    ],
)
def test_malformed_fallback_covers_destructive_verbs(raw_input: str, denied: bool) -> None:
    """malformed JSON fallback は削除・リンク系の verb を拾い、読むだけの verb は拾わない。"""
    assert (bash_config_protection._raw_text_write_risk(raw_input) is not None) is denied


_BLOCKED_PYPROJECT = "BLOCKED: Modifying pyproject.toml is not allowed."


@pytest.mark.parametrize(
    ("raw", "truncated", "code", "fragments"),
    [
        pytest.param(json.dumps(_bash("printf x > pyproject.toml")), False, 2, [_BLOCKED_PYPROJECT], id="redirect"),
        # 先勝ちで 1 キーだけ見ると、無害な `tool_input` の陰の `toolArgs` が素通りする。
        pytest.param(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo safe"},
                    "toolArgs": {"command": "printf x > pyproject.toml"},
                }
            ),
            False,
            2,
            [_BLOCKED_PYPROJECT],
            id="every-container-key",
        ),
        pytest.param(json.dumps(_bash("echo x > .git/hooks/pre-commit")), False, 2, [".git/hooks/"], id="git-hooks"),
        pytest.param(json.dumps(_bash("chmod -x .git/hooks/pre-commit")), False, 2, [".git/hooks/"], id="chmod-hook"),
        pytest.param(
            json.dumps(_bash("echo x >> ~/.claq/logs/bg-2026-09-12.log")),
            False,
            2,
            ["~/.claq/logs", "prompt-injection"],
            id="home-log",
        ),
        pytest.param(json.dumps(_bash("printf x > notes.txt")), False, 0, [], id="non-protected-write"),
        pytest.param(json.dumps(_bash("cat pyproject.toml")), False, 0, [], id="read-only"),
        pytest.param(
            json.dumps({"tool_name": "Write", "tool_input": {"file_path": "pyproject.toml", "content": "x"}}),
            False,
            0,
            [],
            id="non-bash-tool",
        ),
        pytest.param("", False, 0, [], id="empty"),
        pytest.param("{not-even-json", True, 2, ["exceeded"], id="truncated-fail-closed"),
        # JSON が壊れていても生テキストに保護対象 + 書き込み指示が見えれば deny する。
        pytest.param("not-json but printf x > pyproject.toml appears here", False, 2, [], id="malformed-risk"),
        pytest.param("not-json at all", False, 0, [], id="malformed-no-risk"),
        pytest.param("not-json but > notes.txt appears here", False, 0, [], id="malformed-no-protected-name"),
    ],
)
def test_main(
    raw: str,
    truncated: bool,
    code: int,
    fragments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stdin の形ごとの終了コードと、ブロック時に対象を名指しする文言。"""
    monkeypatch.setattr(bash_config_protection, "read_raw_stdin_with_truncation", lambda: (raw, truncated))

    assert bash_config_protection.main() == code
    err = capsys.readouterr().err
    for fragment in fragments:
        assert fragment in err


def test_module_entrypoint_runs_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m` 起動で main の終了コードが SystemExit になる。"""
    # runpy が bash_config_protection を fresh に再実行するため、
    # stdin_io（sys.modules にキャッシュされ再利用される側）を patch する。
    monkeypatch.setattr("claq.hooks.stdin_io.read_raw_stdin_with_truncation", lambda: ("", False))
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.hooks.bash_config_protection", run_name="__main__")
    assert excinfo.value.code == 0


class TestHomeLogDirectoryProtection:
    """``~/.claq/logs/`` への Bash 経由の書込みを保護する。

    `recent_bg_failure_notice` が当日・前日のログ末尾行を無条件で次回
    SessionStart の `<claq-memory>` へ注入するため、このディレクトリへの
    自由な追記はプロンプト注入の永続化経路になる。home は repo ルートと兄弟の
    ディレクトリに固定する（`_home_outside_repo`）。
    """

    @pytest.mark.parametrize(
        "command",
        [
            "echo x >> ~/.claq/logs/bg-2026-09-12.log",
            "printf x > ~/.claq/logs/bg-2026-09-12.log",
            "tee -a ~/.claq/logs/bg-2026-09-12.log <<< x",
            "sed -i 's/a/b/' ~/.claq/logs/bg-2026-09-12.log",
            "rm ~/.claq/logs/bg-2026-09-12.log",
            "rm -rf ~/.claq/logs",
            "mv ~/.claq/logs ~/.claq/logs.bak",
        ],
        ids=[
            "tilde-redirect",
            "tilde-truncate",
            "tilde-tee",
            "tilde-sed-i",
            "tilde-rm",
            "tilde-rmrf-dir",
            "tilde-mv-dir",
        ],
    )
    def test_tilde_form_is_blocked(self, command: str) -> None:
        """``~`` 形の書込み先を deny する。"""
        assert bash_config_protection.find_protected_write(command) is not None

    def test_absolute_form_is_blocked(self, _home_outside_repo: Path) -> None:
        """絶対パス直書き（`~` を介さない）でも deny する。

        `~` を含まないため tilde 展開は無関係。構造一致（`_find_home_log_directory_match`）
        が repo スコープ判定より前で効いていることを、この形で確認する。
        """
        command = f"echo x >> {_home_outside_repo}/.claq/logs/bg-2026-09-12.log"
        assert bash_config_protection.find_protected_write(command) is not None

    def test_relative_after_cd_is_blocked(self) -> None:
        """`cd` を挟んだ相対パスも deny する。

        `_find_home_log_directory_match` は cwd 基準で解決した path への構造的
        一致であり、`scope_certain`（`cd` 等での repo スコープ判定の可否）より
        前で無条件に効くため、`cd` を挟んでいても捕まる。
        """
        assert (
            bash_config_protection.find_protected_write("cd ~ && echo x >> .claq/logs/bg-2026-09-12.log")
            is not None
        )

    @pytest.mark.parametrize(
        ("token", "expected"),
        [("app.py", None), ("{home}/.claq/logs/bg-1.log", "~/.claq/logs")],
        ids=["plain-relative", "absolute"],
    )
    def test_find_home_log_directory_match(self, token: str, expected: str | None, _home_outside_repo: Path) -> None:
        """home ログ配下の絶対パスだけを表示名へ対応付ける。"""
        token = token.format(home=_home_outside_repo)
        assert bash_config_protection._find_home_log_directory_match(token) == expected

    def test_find_home_log_directory_match_falls_back_to_raw_token_when_unresolvable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`resolve_effective_target` が None を返す場合も生トークンで判定できること。"""
        monkeypatch.setattr(bash_config_protection, "resolve_effective_target", lambda _t: None)

        assert bash_config_protection._find_home_log_directory_match(".claq/logs/bg-1.log") == "~/.claq/logs"
