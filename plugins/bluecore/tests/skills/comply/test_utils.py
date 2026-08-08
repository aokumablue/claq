"""utils モジュールのテスト — LLM出力からのYAML抽出、および setup_commands の安全実行。

デシジョンテーブル (extract_yaml):
  | # | 入力パターン                        | 先頭フェンス | 末尾フェンス | 期待結果                        |
  |---|-------------------------------------|------------|------------|-------------------------------|
  | 1 | フェンスなし平文YAML                  | なし       | なし       | 入力そのまま返却                |
  | 2 | ```yaml ... ``` で囲まれたYAML        | あり       | あり       | フェンスを除いた内側だけ返却      |
  | 3 | ``` のみ（言語指定なし）で囲まれた    | あり       | あり       | フェンスを除いた内側だけ返却      |
  | 4 | 先頭フェンスのみ（末尾なし）           | あり       | なし       | 先頭フェンスのみ除去して返却      |
  | 5 | 末尾フェンスのみ（先頭なし）           | なし       | あり       | 末尾フェンスのみ除去して返却      |
  | 6 | 空文字列                             | -          | -          | 空文字列返却                    |
  | 7 | フェンスの間に複数行YAML              | あり       | あり       | 中身の複数行を改行つきで返却      |
  | 8 | フェンス行に追加テキスト（```yaml）   | あり       | あり       | フェンスを除いた内側だけ返却      |

デシジョンテーブル (run_validated_setup_command):
  | # | 入力パターン                              | 期待結果                              |
  |---|-------------------------------------------|---------------------------------------|
  | 1 | mkdir foo / mkdir -p foo/bar               | サンドボックス配下にディレクトリ作成    |
  | 2 | touch bar.txt                              | サンドボックス配下にファイル作成        |
  | 3 | echo ... > file / echo ... >> file         | Python側でファイル書き込み（追記可）    |
  | 4 | printf ... > file（リダイレクトなしは拒否） | 同上／リダイレクトなしは ValueError     |
  | 5 | 許可外コマンド（rm, curl 等）                | ValueError                            |
  | 6 | シェルメタ文字を含む（`;` `` ` `` `$()` 等） | ValueError                            |
  | 7 | 絶対パス／`..` によるサンドボックス外パス     | ValueError                            |
  | 8 | 空文字列コマンド                            | ValueError                            |
  | 9 | mkdir/touch のサブプロセスがタイムアウト      | subprocess.TimeoutExpired が伝播       |
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bluecore.skills.comply import utils as utils_module
from bluecore.skills.comply.utils import extract_yaml, run_validated_setup_command


class TestExtractYaml:
    """extract_yaml 関数のデシジョンテーブルテスト。"""

    # ケース1: フェンスなし
    def test_no_fence_returns_as_is(self) -> None:
        """フェンスが付いていない場合は入力をそのまま返す。"""
        text = "id: test\nname: Test"
        assert extract_yaml(text) == text

    # ケース2: ```yaml ... ``` フェンスあり
    def test_yaml_fence_removed(self) -> None:
        """```yaml ``` で囲まれた場合フェンスを除去して中身だけ返す。"""
        text = "```yaml\nid: test\nname: Test\n```"
        result = extract_yaml(text)
        assert result == "id: test\nname: Test"

    # ケース3: ``` のみ（言語指定なし）
    def test_backtick_fence_no_lang_removed(self) -> None:
        """言語指定なしのバッククォートフェンスも除去される。"""
        text = "```\nid: test\n```"
        result = extract_yaml(text)
        assert result == "id: test"

    # ケース4: 先頭フェンスのみ
    def test_only_leading_fence_removed(self) -> None:
        """先頭フェンスのみがある場合は先頭フェンスだけ除去する。"""
        text = "```yaml\nid: test\nname: Test"
        result = extract_yaml(text)
        assert result == "id: test\nname: Test"

    # ケース5: 末尾フェンスのみ
    def test_only_trailing_fence_removed(self) -> None:
        """末尾フェンスのみがある場合は末尾フェンスだけ除去する。"""
        text = "id: test\nname: Test\n```"
        result = extract_yaml(text)
        assert result == "id: test\nname: Test"

    # ケース6: 空文字列
    def test_empty_string_returns_empty(self) -> None:
        """空文字列を渡した場合は空文字列を返す。"""
        assert extract_yaml("") == ""

    # ケース7: 複数行YAML
    def test_multiline_yaml_preserved(self) -> None:
        """フェンス内の複数行は改行を維持して返す。"""
        text = "```yaml\nid: test\nsteps:\n  - id: s1\n```"
        result = extract_yaml(text)
        assert result == "id: test\nsteps:\n  - id: s1"

    # ケース8: フェンス行に追加テキスト（```yaml）
    def test_fence_with_language_identifier(self) -> None:
        """```yaml のように言語識別子がついたフェンスも除去される。"""
        text = "```yaml\nkey: value\n```"
        result = extract_yaml(text)
        assert result == "key: value"

    def test_spaces_stripped_from_outer(self) -> None:
        """前後の空白・改行はストリップされてから処理される。"""
        text = "\n```yaml\nid: test\n```\n"
        result = extract_yaml(text)
        assert result == "id: test"

    def test_single_line_no_fence(self) -> None:
        """1行のみ・フェンスなしはそのまま返る。"""
        assert extract_yaml("id: only") == "id: only"


# ================================
# run_validated_setup_command テスト
# ================================


class TestRunValidatedSetupCommandAllowed:
    """許可されたコマンドが期待通り動作すること。"""

    def test_mkdir_creates_directory(self, tmp_path: Path) -> None:
        """ケース1: mkdir でサンドボックス配下にディレクトリが作成される。"""
        run_validated_setup_command("mkdir foo", cwd=tmp_path, timeout=5)
        assert (tmp_path / "foo").is_dir()

    def test_mkdir_with_p_flag_creates_nested_directory(self, tmp_path: Path) -> None:
        """ケース1: mkdir -p でネストしたディレクトリが作成される。"""
        run_validated_setup_command("mkdir -p foo/bar", cwd=tmp_path, timeout=5)
        assert (tmp_path / "foo" / "bar").is_dir()

    def test_touch_creates_file(self, tmp_path: Path) -> None:
        """ケース2: touch でサンドボックス配下にファイルが作成される。"""
        run_validated_setup_command("touch bar.txt", cwd=tmp_path, timeout=5)
        assert (tmp_path / "bar.txt").is_file()

    def test_echo_redirect_writes_file(self, tmp_path: Path) -> None:
        """ケース3: echo と > で内容がファイルに書き込まれる（Python側で処理）。"""
        run_validated_setup_command("echo hello world > out.txt", cwd=tmp_path, timeout=5)
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello world\n"

    def test_echo_append_redirect(self, tmp_path: Path) -> None:
        """ケース3: echo と >> で既存ファイルに追記される。"""
        (tmp_path / "out.txt").write_text("first\n", encoding="utf-8")
        run_validated_setup_command("echo second >> out.txt", cwd=tmp_path, timeout=5)
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "first\nsecond\n"

    def test_printf_redirect_writes_file(self, tmp_path: Path) -> None:
        """ケース4: printf と > で内容がファイルに書き込まれる（末尾改行は付与しない）。"""
        run_validated_setup_command('printf "%s" hello > out.txt', cwd=tmp_path, timeout=5)
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "%s hello"

    def test_mkdir_uses_shell_false_explicitly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """mkdir/touch のサブプロセス実行は shell=False で行われること。"""
        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            return None

        monkeypatch.setattr(utils_module.subprocess, "run", fake_run)
        run_validated_setup_command("mkdir foo", cwd=tmp_path, timeout=5)

        assert calls[0]["shell"] is False


class TestRunValidatedSetupCommandRejected:
    """危険・許可外のコマンドが ValueError で拒否されること。"""

    def test_rejects_disallowed_command_rm(self, tmp_path: Path) -> None:
        """ケース5: rm -rf / は許可外コマンドとして拒否される。"""
        with pytest.raises(ValueError, match="disallowed setup command"):
            run_validated_setup_command("rm -rf /", cwd=tmp_path, timeout=5)

    def test_rejects_disallowed_command_curl(self, tmp_path: Path) -> None:
        """ケース5: curl は許可外コマンドとして拒否される。"""
        with pytest.raises(ValueError, match="disallowed setup command"):
            run_validated_setup_command("curl http://evil.example", cwd=tmp_path, timeout=5)

    def test_rejects_backtick_command_substitution(self, tmp_path: Path) -> None:
        """ケース6: バッククォートによるコマンド置換を含む文字列は拒否される。"""
        with pytest.raises(ValueError, match="shell metacharacter"):
            run_validated_setup_command("echo `whoami` > out.txt", cwd=tmp_path, timeout=5)

    def test_rejects_dollar_paren_command_substitution(self, tmp_path: Path) -> None:
        """ケース6: $() によるコマンド置換を含む文字列は拒否される。"""
        with pytest.raises(ValueError, match="shell metacharacter"):
            run_validated_setup_command("echo $(whoami) > out.txt", cwd=tmp_path, timeout=5)

    def test_rejects_shell_chaining_semicolon(self, tmp_path: Path) -> None:
        """ケース6: セミコロンによるコマンド連結を含む文字列は拒否される。"""
        with pytest.raises(ValueError, match="shell metacharacter"):
            run_validated_setup_command("touch a; rm -rf /", cwd=tmp_path, timeout=5)

    def test_rejects_pipe(self, tmp_path: Path) -> None:
        """ケース6: パイプを含む文字列は拒否される。"""
        with pytest.raises(ValueError, match="shell metacharacter"):
            run_validated_setup_command("echo hi | rm -rf /", cwd=tmp_path, timeout=5)

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        """ケース7: 絶対パスへの touch は拒否される。"""
        with pytest.raises(ValueError, match="absolute path"):
            run_validated_setup_command("touch /etc/passwd", cwd=tmp_path, timeout=5)

    def test_rejects_path_traversal_in_mkdir(self, tmp_path: Path) -> None:
        """ケース7: .. によるサンドボックス外へのパストラバーサルは拒否される。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        with pytest.raises(ValueError, match="escapes sandbox"):
            run_validated_setup_command("mkdir -p ../outside", cwd=sandbox, timeout=5)

    def test_rejects_path_traversal_in_redirect_target(self, tmp_path: Path) -> None:
        """ケース7: リダイレクト先が .. でサンドボックス外を指す場合は拒否される。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        with pytest.raises(ValueError, match="escapes sandbox"):
            run_validated_setup_command("echo hi > ../evil.txt", cwd=sandbox, timeout=5)

    def test_rejects_empty_command(self, tmp_path: Path) -> None:
        """ケース8: 空文字列は拒否される。"""
        with pytest.raises(ValueError, match="empty setup command"):
            run_validated_setup_command("   ", cwd=tmp_path, timeout=5)

    def test_echo_without_redirect_rejected(self, tmp_path: Path) -> None:
        """リダイレクトを伴わない echo は非対応として拒否される。"""
        with pytest.raises(ValueError, match="without redirection"):
            run_validated_setup_command("echo hello", cwd=tmp_path, timeout=5)

    def test_redirect_with_multiple_targets_rejected(self, tmp_path: Path) -> None:
        """リダイレクト先が複数トークンの場合は拒否される。"""
        with pytest.raises(ValueError, match="exactly one file"):
            run_validated_setup_command("echo hi > a.txt b.txt", cwd=tmp_path, timeout=5)


class TestRunValidatedSetupCommandTimeout:
    """ケース9: mkdir/touch のサブプロセスタイムアウトはそのまま呼び出し側へ伝播すること。"""

    def test_mkdir_timeout_propagates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="mkdir", timeout=5)

        monkeypatch.setattr(utils_module.subprocess, "run", fake_run)

        with pytest.raises(subprocess.TimeoutExpired):
            run_validated_setup_command("mkdir foo", cwd=tmp_path, timeout=5)
