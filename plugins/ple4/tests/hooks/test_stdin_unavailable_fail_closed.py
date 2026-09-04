"""stdin を読めなかったとき、4 つの保護 hook が揃って deny することのテスト。

Windows の通常パイプでは ``select.select`` が ``OSError`` になり、旧実装は
それを「入力なし」へ正規化していた。その結果 ``git status`` と
``git commit --no-verify`` が揃って exit 0 になり、保護 hook が 1 バイトも
検査しないまま素通りしていた（release-verify 2026-09-03 の P1-004。
`docs/adr/0019-protection-hooks-fail-closed-when-stdin-cannot-be-read.md`）。

「読む対象が無い」（tty 起動・stdin 未接続・即 EOF）と「読めなかった」を
区別するのが修正の核心なので、両方をこの 1 ファイルで並べて固定する。
4 hook を parametrize でまとめて見るのは、片方だけ強化されて
「Write なら止まるが Bash なら通る」型の非対称が生まれるのを防ぐため。

デシジョンテーブル:
  - StdinUnavailableError → exit 2 + deny JSON + hook 名入りの stderr
  - 空文字列（payload なし） → exit 0（素通り。F-01 の契約を維持）
"""

from __future__ import annotations

import importlib
import json

import pytest

from ple4.hooks.hook_common import StdinUnavailableError

# (hook モジュール名, 差し替え対象モジュール名, deny 理由に載る hook 名)
#
# モジュールは実行時に import_module で引き直す。conftest の
# `_fresh_runpy_module` が runpy 実行のたびに sys.modules から対象を落とす
# ため、収集時に掴んだモジュールオブジェクトは実行時のものと別物になりうる
# （その状態で monkeypatch すると差し替えが効かない）。
_PROTECTION_HOOKS = [
    pytest.param(
        "ple4.hooks.block_no_verify",
        "ple4.hooks.block_no_verify",
        "pre:block-no-verify",
        id="block_no_verify",
    ),
    pytest.param(
        "ple4.hooks.config_protection",
        "ple4.hooks.config_protection",
        "pre:config-protection",
        id="config_protection",
    ),
    pytest.param(
        "ple4.hooks.bash_config_protection",
        "ple4.hooks.bash_config_protection",
        "pre:bash-config-protection",
        id="bash_config_protection",
    ),
    pytest.param(
        "ple4.hooks.pre_bash_commit_quality",
        # main() 内で hook_common から遅延 import するため、差し替えは共通側。
        "ple4.hooks.hook_common",
        "pre:bash-commit-quality",
        id="pre_bash_commit_quality",
    ),
]


def _patch_reader(monkeypatch: pytest.MonkeyPatch, reader_module: str, reader) -> None:  # noqa: ANN001
    """指定モジュールの `read_raw_stdin_with_truncation` を差し替える。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。
        reader_module: 差し替え対象モジュールの dotted name。
        reader: 差し替える callable。

    Returns:
        なし
    """
    monkeypatch.setattr(
        importlib.import_module(reader_module), "read_raw_stdin_with_truncation", reader
    )


@pytest.mark.parametrize(("hook_module", "reader_module", "hook_name"), _PROTECTION_HOOKS)
def test_unreadable_stdin_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook_module: str,
    reader_module: str,
    hook_name: str,
) -> None:
    """読み取り不能な stdin は全保護 hook が exit 2 + deny JSON で拒否すること。"""
    _patch_reader(
        monkeypatch,
        reader_module,
        lambda: (_ for _ in ()).throw(StdinUnavailableError("pipe read failed")),
    )

    assert importlib.import_module(hook_module).main() == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["permissionDecision"] == "deny"
    assert hook_name in captured.err
    assert "pipe read failed" in captured.err


@pytest.mark.parametrize(("hook_module", "reader_module", "hook_name"), _PROTECTION_HOOKS)
def test_absent_stdin_still_passes_through(
    monkeypatch: pytest.MonkeyPatch,
    hook_module: str,
    reader_module: str,
    hook_name: str,
) -> None:
    """payload が無い場合（tty 起動等）は従来どおり素通りすること。

    ここまで deny にすると、payload を渡さない host で全ツール呼び出しが
    拒否され、セッションが復旧不能になる。「入力が無い」と「読めなかった」を
    分けるのはこの差を保つため。
    """
    _patch_reader(monkeypatch, reader_module, lambda: ("", False))

    assert importlib.import_module(hook_module).main() == 0
