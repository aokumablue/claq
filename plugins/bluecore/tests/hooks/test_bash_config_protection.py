"""`bash_config_protection` フックのテスト（A-06: Bash 経由の設定ファイル直接書き換え保護）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluecore.hooks import bash_config_protection


def _bash_payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.fixture(autouse=True)
def _fixed_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """`find_protected_write` の repo スコープ判定（A-06）を tmp_path 配下に固定する。

    実行環境の git リポジトリに依存せず、cwd と `resolve_repo_root` の両方を
    同一の一時ディレクトリに揃えることでテストを決定的にする。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)
    return tmp_path


class TestFindProtectedWrite:
    @pytest.mark.parametrize(
        "command",
        [
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
            # A-02: 拡張したリダイレクト演算子。
            "echo x &> pyproject.toml",
            "echo x >| pyproject.toml",
            "echo x 1> pyproject.toml",
            "echo x 2>> pyproject.toml",
            # A-02: 書き込み先が引数位置に明示されるコマンド群。
            "cp src.py pyproject.toml",
            "mv src.py pyproject.toml",
            "install -m 644 src.py pyproject.toml",
            "ln -f /dev/null pyproject.toml",
            # F-07: -f 無しの新規 symlink 作成も、以後その名前を読む処理を
            # リンク先へ差し替えるため実質的な書き込み。
            "ln -s weak.toml pyproject.toml",
            # F-07: 長オプション形式の --force も検出する。
            "ln --force /dev/null pyproject.toml",
            "dd if=/dev/zero of=pyproject.toml",
            "perl -i -pe s/a/b/ pyproject.toml",
            "perl -0pi -e 1 pyproject.toml",
        ],
    )
    def test_detects_protected_write(self, command: str) -> None:
        assert bash_config_protection.find_protected_write(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "printf x > notes.txt",
            "cat pyproject.toml",
            "echo pyproject.toml",
            "grep -n commands tox.ini",
            "sed -n '1,5p' pyproject.toml",
            "tee",
            "sed -i",
            "",
            # 保護対象でない名前への ln は対象外。
            "ln -s a.txt b.txt",
        ],
    )
    def test_allows_non_write(self, command: str) -> None:
        assert bash_config_protection.find_protected_write(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            "rm pyproject.toml",
            "rm -f pyproject.toml",
            "rm -rf pyproject.toml",
            "unlink pyproject.toml",
            "shred pyproject.toml",
            "truncate -s 0 pyproject.toml",
            # mv は移動先だけでなく移動元も実質削除
            "mv pyproject.toml /tmp/backup",
            "sudo rm pyproject.toml",
            "env rm pyproject.toml",
        ],
    )
    def test_detects_protected_removal(self, command: str) -> None:
        """削除・切り詰め・移動元も弱体化として検出する。

        書き込み先トークンとしては現れないが、リンタ設定を消せばルールごと
        無効化できるため上書きと同じ強さの弱体化にあたる。
        """
        assert bash_config_protection.find_protected_write(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            # 保護対象でないファイルの削除
            "rm notes.txt",
            # 複製は元が残るため弱体化ではない
            "cp pyproject.toml /tmp/backup",
            # コマンド名だけでファイル指定が無い
            "rm",
            "truncate -s 0",
        ],
    )
    def test_allows_non_weakening_removal(self, command: str) -> None:
        assert bash_config_protection.find_protected_write(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            "sudo cp /tmp/x pyproject.toml",
            "sudo install /tmp/x pyproject.toml",
            "sudo ln -f /dev/null pyproject.toml",
            "env cp /tmp/x pyproject.toml",
        ],
    )
    def test_detects_write_through_command_wrapper(self, command: str) -> None:
        """wrapper 経由（sudo/env/command）でも最終引数・ln -f を検出する。

        tee/sed/perl/dd は `_command_index` を通していたが cp/mv/install と
        ln -f は素の `segment[0]` を見ており、`sudo cp ... pyproject.toml` が
        素通りしていた。
        """
        assert bash_config_protection.find_protected_write(command) is not None

    def test_allows_protected_basename_outside_repo_root(self) -> None:
        """A-06 回帰防止: リポジトリ外の同名ファイルへの書き込みは allow する。"""
        assert (
            bash_config_protection.find_protected_write(
                "printf x > /definitely-outside-the-repo/pyproject.toml"
            )
            is None
        )

    def test_allows_when_repo_root_undetermined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A-06: リポジトリルートが決定できない場合は allow する（deny に倒さない）。"""
        monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: None)

        assert bash_config_protection.find_protected_write("printf x > pyproject.toml") is None

    def test_resolve_repo_root_not_called_without_write_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """保護対象 basename が出現しない大多数の呼び出しでは resolve_repo_root を呼ばない。"""
        called = {"n": 0}

        def _tracking_resolve() -> Path:
            called["n"] += 1
            return Path("/repo")

        monkeypatch.setattr(bash_config_protection, "resolve_repo_root", _tracking_resolve)

        assert bash_config_protection.find_protected_write("ls -la && echo hello") is None
        assert called["n"] == 0

    @pytest.mark.parametrize(
        "command",
        [
            # perl: -i フラグなし（in-place ではない）。
            "perl -pe s/a/b/ pyproject.toml",
            # perl: -i フラグはあるが対象が保護対象でない。
            "perl -i -pe s/a/b/ notes.txt",
            # cp/mv/install: 非オプション引数が無い（書き込み先が確定しない）。
            "cp -v",
            "mv --",
            # ln -f: 非オプション引数が無い。
            "ln -f",
            # dd of=: 出力先が保護対象でない。
            "dd if=/dev/zero of=notes.txt",
        ],
    )
    def test_extended_detector_edge_cases_allow(self, command: str) -> None:
        """拡張検出（perl/cp/mv/ln/dd）の non-match 分岐（in-place 無し・引数無し・非保護対象）。"""
        assert bash_config_protection.find_protected_write(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            # M-01: tee/sed/perl/dd/of= が「実行位置」ではなくただの引数文字列
            # として現れるだけの場合は allow する（コマンド名の文字列一致だけ
            # で deny していた過去の実装は誤検出していた）。
            "echo tee pyproject.toml",
            "echo of=pyproject.toml",
            "echo sed -i pyproject.toml",
            "echo perl -i pyproject.toml",
            "printf 'tee pyproject.toml\\n'",
            "grep -l 'dd of=pyproject.toml' notes.txt",
        ],
    )
    def test_command_position_non_matches_allow(self, command: str) -> None:
        """M-01: tee/sed/perl/dd が実行位置に無ければ allow する。"""
        assert bash_config_protection.find_protected_write(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            # M-01: wrapper を挟んでも実行位置の特定は維持する（過検出防止の
            # ための修正で既存の wrapper 検出力を落とさない）。
            "env X=1 tee pyproject.toml",
            "sudo tee pyproject.toml",
            "command tee pyproject.toml",
            "sudo -u root tee pyproject.toml",
            "env -u FOO tee pyproject.toml",
            "env sed -i 's/a/b/' pyproject.toml",
            "sudo dd if=/dev/zero of=pyproject.toml",
            # 未知の wrapper オプション（値の有無を判定できない）は 1 トークン
            # だけ読み飛ばして実行対象の探索を続ける。
            "sudo -n tee pyproject.toml",
        ],
    )
    def test_wrapped_command_position_still_detected(self, command: str) -> None:
        """M-01: env/sudo/command wrapper 経由でも実行位置を正しく特定して deny する。"""
        assert bash_config_protection.find_protected_write(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            # _command_index が実行対象を特定できない（全トークンが代入/
            # wrapper/オプションで終わる）場合は allow する。
            "env",
            "FOO=bar",
            "sudo -u",
        ],
    )
    def test_command_index_unresolvable_allows(self, command: str) -> None:
        assert bash_config_protection.find_protected_write(command) is None

    def test_within_repo_root_returns_false_on_resolve_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cwd の resolve() が OSError を投げても deny せず False を返す（可用性優先。H-02 の
        resolve_effective_target 経由でこの分岐に入る）。
        """

        def _raising_cwd() -> Path:
            raise OSError("cwd unavailable")

        monkeypatch.setattr(bash_config_protection.Path, "cwd", classmethod(lambda cls: _raising_cwd()))

        assert bash_config_protection._within_repo_root("pyproject.toml", Path("/repo")) is False

    def test_within_repo_root_returns_false_when_repo_root_resolve_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """token 側の解決は成功しても repo_root.resolve() が失敗すれば False を返す。"""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

        class _BoomPath(Path):
            def resolve(self, strict: bool = False) -> Path:  # noqa: ARG002
                raise OSError("repo root unavailable")

        assert bash_config_protection._within_repo_root("pyproject.toml", _BoomPath(tmp_path)) is False


class TestMain:
    def test_main_blocks_redirect_to_protected_file(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("printf x > pyproject.toml"), False),
        )
        assert bash_config_protection.main() == 2
        assert "BLOCKED: Modifying pyproject.toml is not allowed." in capsys.readouterr().err

    def test_main_allows_non_protected_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("printf x > notes.txt"), False),
        )
        assert bash_config_protection.main() == 0

    def test_main_allows_read_only_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("cat pyproject.toml"), False),
        )
        assert bash_config_protection.main() == 0

    def test_main_ignores_non_bash_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": "pyproject.toml", "content": "x"}}
        )
        monkeypatch.setattr(
            bash_config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False)
        )
        assert bash_config_protection.main() == 0

    def test_main_empty_input_is_non_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            bash_config_protection, "read_raw_stdin_with_truncation", lambda: ("", False)
        )
        assert bash_config_protection.main() == 0

    def test_main_truncated_input_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: ("{not-even-json", True),
        )
        assert bash_config_protection.main() == 2
        assert "exceeded" in capsys.readouterr().err

    def test_main_malformed_json_with_write_risk_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """JSON が壊れていても生テキストに保護対象 + 書き込み指示が見えれば deny する。"""
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: ("not-json but printf x > pyproject.toml appears here", False),
        )
        assert bash_config_protection.main() == 2

    def test_main_malformed_json_without_write_risk_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            bash_config_protection, "read_raw_stdin_with_truncation", lambda: ("not-json at all", False)
        )
        assert bash_config_protection.main() == 0

    def test_main_malformed_json_with_indicator_but_no_protected_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """書き込み指示だけあっても保護対象ファイル名が無ければ通す。"""
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: ("not-json but > notes.txt appears here", False),
        )
        assert bash_config_protection.main() == 0

    def test_module_entrypoint_runs_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import runpy

        # runpy が bash_config_protection を fresh に再実行するため、
        # hook_common（sys.modules にキャッシュされ再利用される側）を patch する。
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation", lambda: ("", False)
        )
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bluecore.hooks.bash_config_protection", run_name="__main__")
        assert excinfo.value.code == 0


class TestMalformedFallbackCoversDestructiveVerbs:
    """F-07: malformed JSON fallback がトークン化経路と同じ verb 集合を見ること。"""

    @pytest.mark.parametrize(
        "raw_input",
        [
            "{broken rm ruff.toml",
            "{broken unlink pyproject.toml",
            "{broken shred tox.ini",
            "{broken ln -s x ruff.toml",
        ],
    )
    def test_destructive_verbs_are_detected(self, raw_input: str) -> None:
        """削除・リンク系の verb と保護対象名が同居したら deny 対象になること。

        トークン化できる経路では `_remove_target` / `_ln_target` が既にこれらを
        見ている。JSON の壊れ方だけで同じ入力が通ったり通らなくなったりするのは
        境界として説明できない。
        """
        assert bash_config_protection._raw_text_write_risk(raw_input) is not None

    def test_read_only_verb_is_not_detected(self) -> None:
        """読むだけの verb は保護対象名が見えても deny しないこと。"""
        assert bash_config_protection._raw_text_write_risk("{broken cat ruff.toml") is None


class TestPathProtectionPropagates:
    """`config_protection` の path ベース保護が Bash 経路にも伝播する。

    `bash_config_protection` は A-06 で「`config_protection` の Edit/Write 限定を
    Bash 側で補完する」ために作られた。basename 集合だけを import して path
    ベースの保護（`.git/hooks/` 配下）を取りこぼすと、同じ書込みが Write では
    exit 2、Bash では exit 0 という非対称が生まれ、このモジュールの存在理由
    そのものに反する。
    """

    @pytest.mark.parametrize(
        "command",
        [
            "echo x > .git/hooks/pre-commit",
            "printf x >> .git/hooks/pre-push",
            "sed -i 's/a/b/' .git/hooks/pre-commit",
            "echo x | tee .git/hooks/commit-msg",
            "rm .git/hooks/pre-commit",
        ],
        ids=["redirect", "append", "sed-i", "tee", "rm"],
    )
    def test_writes_under_git_hooks_are_blocked(self, command: str) -> None:
        """`.git/hooks/` 配下への Bash 経由の書込み・削除をブロックする。"""
        assert bash_config_protection.find_protected_write(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "echo x > src/hooks/pre-commit",
            "echo x > .git/config",
            "echo x > README.md",
            "echo x > hooks/pre-commit",
        ],
        ids=["same-basename-elsewhere", "git-dir-but-not-hooks", "prose", "hooks-without-git"],
    )
    def test_lookalike_paths_are_allowed(self, command: str) -> None:
        """`.git/hooks/` 配下でない同名・類似パスは通す。"""
        assert bash_config_protection.find_protected_write(command) is None

    def test_main_blocks_git_hooks_write(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main 経路でも exit 2 になり、対象を名指しする。"""
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("echo x > .git/hooks/pre-commit"), False),
        )

        assert bash_config_protection.main() == 2
        assert ".git/hooks/" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf .git/hooks",
            "mv .git/hooks .git/hooks.off",
            "ln -s /dev/null .git/hooks",
        ],
        ids=["rm-rf-dir", "mv-dir", "ln-over-dir"],
    )
    def test_protected_directory_itself_is_blocked(self, command: str) -> None:
        """保護ディレクトリ自身を消す・退避する操作もブロックする。

        配下ファイルの書換え（`rm .git/hooks/pre-commit`）は deny なのに
        1 コンポーネント短いディレクトリ操作が allow だと、同じ verb・同じ結果
        （フックが走らない状態）に対する非対称が残る。
        """
        assert bash_config_protection.find_protected_write(command) is not None


class TestModeChangeIsBlocked:
    """権限変更による無効化も上書きと同じ強さの弱体化として扱う。

    中身を書き換えなくても実行ビットや読み取り権限を落とせば検査は無効化
    できる。書き換え経路だけを塞ぐと、塞いだ経路の存在が誤った安心になる。
    """

    @pytest.mark.parametrize(
        "command",
        [
            "chmod -x .git/hooks/pre-commit",
            "chmod 000 .eslintrc.json",
            "chmod a-r pyproject.toml",
            "chown nobody .git/hooks/pre-commit",
            "chgrp nogroup .eslintrc.json",
            "chflags uchg pyproject.toml",
            "sudo chmod -x .git/hooks/pre-commit",
            "chmod -R 000 .git/hooks",
        ],
        ids=[
            "chmod-hook",
            "chmod-eslintrc",
            "chmod-pyproject",
            "chown-hook",
            "chgrp-eslintrc",
            "chflags-pyproject",
            "sudo-chmod-hook",
            "chmod-hooks-dir",
        ],
    )
    def test_mode_change_on_protected_target_is_blocked(self, command: str) -> None:
        """保護対象への権限変更をブロックする。"""
        assert bash_config_protection.find_protected_write(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "chmod +x scripts/deploy.sh",
            "chmod 644 README.md",
            "chmod -x src/hooks/pre-commit",
            "chown me:me .git/config",
            "echo chmod -x .git/hooks/pre-commit",
        ],
        ids=["script", "readme", "same-basename-elsewhere", "git-config", "not-executed"],
    )
    def test_mode_change_on_other_targets_is_allowed(self, command: str) -> None:
        """保護対象でないパスへの権限変更は通す。"""
        assert bash_config_protection.find_protected_write(command) is None

    def test_main_blocks_chmod_on_hook(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main 経路でも exit 2 になり、対象を名指しする。"""
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("chmod -x .git/hooks/pre-commit"), False),
        )

        assert bash_config_protection.main() == 2
        assert ".git/hooks/" in capsys.readouterr().err
