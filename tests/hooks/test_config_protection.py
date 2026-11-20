"""config_protection.main() の入力形（Write の基本形・apply_patch の生文字列と Codex の dict・Copilot の小文字ツール名・
native camelCase・旧 file キー・空/不正/切り捨て入力）と、コミット前検査の定義・保護フック自身の設定を守る判定のテスト。

`emit_block_output` は本物の出力へ素通しするスパイで包み、ブロック理由の呼び出し回数と
実際の stdout/stderr を同じ行で検査する。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from claq.hooks import config_protection


def _apply_patch(path: str) -> dict[str, Any]:
    """tool_input が生のパッチ文字列（dict ではない Copilot CLI 実行パス）の apply_patch payload を作る。"""
    return {"tool_name": "apply_patch", "tool_input": f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch\n"}


def _codex_patch(path: str) -> dict[str, Any]:
    """tool_input が ``{"input": パッチ}`` の dict（Codex 実行パス）の apply_patch payload を作る。"""
    return {"tool_name": "apply_patch", "tool_input": {"input": f"*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch"}}


def _write_path(path: str) -> dict[str, Any]:
    """file_path だけを持つ Write payload を作る。"""
    return {"tool_name": "Write", "tool_input": {"file_path": path}}


def _run_protection(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | str, *, truncated: bool = False
) -> tuple[int, list[str]]:
    """stdin を差し替えて main() を実行し、終了コードと emit_block_output へ渡った理由の列を返す。"""
    reasons: list[str] = []
    real_emit = config_protection.emit_block_output

    def _spy(reason: str) -> int:
        """ブロック理由を記録し、本物の出力へ渡す。"""
        reasons.append(reason)
        return real_emit(reason)

    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(config_protection, "emit_block_output", _spy)
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (raw, truncated))
    return config_protection.main(), reasons


_MODIFY_PRETTIERRC = "BLOCKED: Modifying .prettierrc is not allowed."

# (stdin の payload, ブロック理由に含まれる文言)。文言が None の行は allow で、何も出力しない。
_CASES = [
    pytest.param(_apply_patch("/tmp/.prettierrc"), _MODIFY_PRETTIERRC, id="apply-patch-string-protected"),
    pytest.param(_apply_patch("/tmp/example.txt"), None, id="apply-patch-string-unprotected"),
    # extract_file_paths() は apply_patch 以外は tool_name によらず file_path を抽出する（正規化なしの回帰確認）。
    pytest.param({"tool_name": "write", "tool_input": {"file_path": ".prettierrc"}}, _MODIFY_PRETTIERRC, id="copilot-write"),
    pytest.param(
        {"tool_name": "edit", "tool_input": {"file_path": "biome.json"}},
        "BLOCKED: Modifying biome.json is not allowed.",
        id="copilot-edit",
    ),
    # matcher が "*" に広がっても、非書込みツールでは保護判定自体を行わない（早期 return）。
    pytest.param({"tool_name": "Read", "tool_input": {"file_path": ".prettierrc"}}, None, id="non-write-read"),
    pytest.param({"tool_name": "read", "tool_input": {"file_path": ".prettierrc"}}, None, id="copilot-non-write-read"),
    pytest.param({"toolName": "edit", "toolArgs": {"file_path": "ruff.toml"}}, "ruff.toml", id="native-camel-case"),
    pytest.param({"toolName": "edit", "toolArgs": {"file": "ruff.toml"}}, "ruff.toml", id="native-legacy-file-key"),
    # 混在 payload は、いずれかのコンテナが保護対象なら deny する。
    pytest.param(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/sample.py"},
            "toolName": "edit",
            "toolArgs": {"file_path": "ruff.toml"},
        },
        "ruff.toml",
        id="conflicting-native-side-protected",
    ),
    pytest.param(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "ruff.toml"},
            "toolName": "edit",
            "toolArgs": {"file_path": "src/sample.py"},
        },
        "ruff.toml",
        id="conflicting-snake-side-protected",
    ),
    pytest.param({"toolName": "multiedit", "toolArgs": {"file_path": "src/sample.py"}}, None, id="native-unprotected"),
    pytest.param({"tool_name": "Edit", "tool_input": {"file_path": "ruff.toml"}}, "ruff.toml", id="dt03-1-snake-ruff"),
    pytest.param(
        {"toolName": "edit", "toolArgs": json.dumps({"file_path": "ruff.toml"})}, "ruff.toml", id="dt03-3-toolargs-json-string"
    ),
    pytest.param(
        {"tool_name": "Write", "tool_args": {"file_path": ".eslintrc"}}, ".eslintrc", id="dt03-4-tool_args-eslintrc"
    ),
    pytest.param({"toolName": "edit", "toolArgs": {"file_path": "src/ruff.toml"}}, "ruff.toml", id="dt03-7-basename"),
    pytest.param({"toolName": "bash", "toolArgs": {"file_path": "ruff.toml"}}, None, id="dt03-8-non-write-bash"),
    pytest.param({"toolName": "edit", "toolArgs": {}}, None, id="dt03-9-no-path-key"),
    pytest.param(
        {"toolName": "apply_patch", "toolArgs": {"input": "garbage"}},
        "Could not determine target files",
        id="dt03-10-apply-patch-fail-closed",
    ),
    pytest.param(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}, "toolArgs": {"file_path": "src/b.py"}},
        None,
        id="dt03-13-both-unprotected",
    ),
    pytest.param(
        {"tool_name": "Edit", "tool_input": None, "toolArgs": {"file_path": "ruff.toml"}},
        "ruff.toml",
        id="dt03-14-tool-input-none-still-scans",
    ),
    pytest.param(_write_path("eslint.config.js"), "BLOCKED: Modifying eslint.config.js is not allowed.", id="write-protected"),
    pytest.param(_write_path("README.md"), None, id="write-unprotected"),
    pytest.param(_write_path("plugins/claq/model.json"), None, id="write-model-json"),
    # file_path が空なら保護判定をスキップする。
    pytest.param(_write_path(""), None, id="write-blank-file-path"),
    pytest.param(
        {"tool_name": "Write", "tool_input": {"file": "biome.json"}},
        "BLOCKED: Modifying biome.json is not allowed.",
        id="write-legacy-file-key",
    ),
    # コミット前検査の定義そのもの（block_no_verify が --no-verify / core.hooksPath を塞いでも、フックスクリプトや
    # .pre-commit-config.yaml を直接書き換えれば検査が走らない同じ結果になる）。
    pytest.param(_write_path(".pre-commit-config.yaml"), "BLOCKED: Modifying .pre-commit-config.yaml is not allowed.", id="pre-commit-yaml"),
    pytest.param(_write_path("/repo/.pre-commit-config.yml"), "BLOCKED: Modifying .pre-commit-config.yml is not allowed.", id="pre-commit-yml"),
    pytest.param(_write_path(".git/hooks/pre-commit"), "BLOCKED: Modifying .git/hooks/ is not allowed.", id="git-hooks-relative"),
    pytest.param(_write_path("/repo/.git/hooks/commit-msg"), "BLOCKED: Modifying .git/hooks/ is not allowed.", id="git-hooks-absolute"),
    # `.git/hooks/` 配下でない同名・類似パスは通す。
    pytest.param(_write_path("src/hooks/pre-commit"), None, id="same-basename-elsewhere"),
    pytest.param(_write_path(".git/info/exclude"), None, id="git-dir-but-not-hooks"),
    pytest.param(_write_path("docs/.git-hooks-notes.md"), None, id="prose"),
    pytest.param(_write_path("hooks/pre-commit"), None, id="hooks-without-git"),
    pytest.param(_codex_patch("ruff.toml"), "BLOCKED: Modifying ruff.toml is not allowed.", id="codex-apply-patch-protected"),
    pytest.param(_codex_patch("src/main.py"), None, id="codex-apply-patch-unprotected"),
    pytest.param(
        {"tool_name": "apply_patch", "tool_input": {"input": "garbage"}},
        "Could not determine target files",
        id="codex-apply-patch-garbage",
    ),
    # 空入力（tty・stdin 未接続等）は保護判定せず allow する。
    pytest.param("", None, id="empty-input"),
    # matcher が書込み系ツールに限るため、非空だが不正な JSON は保護対象か判定できず fail-closed で block する。
    pytest.param("{not-json", "Could not parse hook input", id="unparseable-json"),
]


@pytest.mark.parametrize(("payload", "reason_snippet"), _CASES)
def test_main_verdict_and_output(
    payload: dict[str, Any] | str,
    reason_snippet: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """保護対象なら理由を 1 回だけ出して exit 2（stdout は deny JSON）、それ以外は何も出さず exit 0。"""
    rc, reasons = _run_protection(monkeypatch, payload)
    captured = capsys.readouterr()

    if reason_snippet is None:
        assert (rc, reasons, captured.out, captured.err) == (0, [], "", "")
    else:
        assert (rc, len(reasons)) == (2, 1)
        assert reason_snippet in reasons[0]
        assert reason_snippet in captured.err
        assert json.loads(captured.out) == {"permissionDecision": "deny", "permissionDecisionReason": reasons[0]}


def test_main_blocks_on_truncated_input(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """入力が切り捨てられた場合は fail-closed でブロックする（自己完結 guard）。"""
    rc, reasons = _run_protection(monkeypatch, "{not-even-json", truncated=True)

    assert (rc, len(reasons)) == (2, 1)
    assert "BLOCKED: Hook input exceeded" in reasons[0]
    assert "BLOCKED: Hook input exceeded" in capsys.readouterr().err


class TestProtectionHookOwnConfig:
    """保護フック自身の設定を守る判定（docs/adr/hook-failure-direction.md が定める `config_protection` の対象）。

    `config_protection` は lint 設定を「エージェントが自分の作業を通すために
    ガードレール側を緩める」経路として守る。同じ動機で最も効く緩め方
    — 保護フックの登録そのものを消す — も対象にする。
    """

    @pytest.mark.parametrize(
        ("file_path", "expected"),
        [
            # プラグイン自身の登録元。1 回の Edit で PreToolUse ガード 4 種を消せる。
            ("/repo/plugins/claq/hooks/hooks.json", 2),
            # 配布物はプラグインキャッシュ配下に置かれる。同じ形で当たること。
            ("/home/u/.claude/plugins/cache/claq/claq/0.9.54/hooks/hooks.json", 2),
            # **消してはいけない陰性対照**: consumer 側の `.claude/hooks.json` は
            # `/harness --apply` が正当に書き込む先。basename `hooks.json` で
            # 判定すると巻き込むため、パス連続一致（`hooks/hooks.json`）で守る。
            ("/repo/.claude/hooks.json", 0),
            # `hooks/` 配下でもファイル名が違えば対象外（テストや実装ファイル）。
            ("/repo/tests/hooks/test_hook_common.py", 0),
        ],
    )
    def test_plugin_hook_manifest_is_protected_by_path_not_basename(
        self, file_path: str, expected: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """プラグインの hooks.json だけを守り、consumer 側は通すこと。"""
        payload = {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": "{}"}}
        assert _run_protection(monkeypatch, payload)[0] == expected

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            # 保護の無効化ではなく、全ツール呼び出しでの任意コード実行になる。
            ('{"env": {"CLAQ_PYTHON": "/tmp/evil"}}', 2),
            ('{"hooks": {"PreToolUse": []}}', 2),
            ('{"disabledPlugins": ["claq"]}', 2),
            ('{"enabledPlugins": []}', 2),
            # **消してはいけない陰性対照**: `env` キー全般を条件にすると
            # `update-config` skill の中心的な仕事（`DEBUG=true`）と
            # `.vscode/settings.json` の `terminal.integrated.env.*` を巻き込む。
            ('{"env": {"DEBUG": "true"}}', 0),
            ('{"permissions": {"allow": ["Bash(npm:*)"]}}', 0),
            ('{"terminal.integrated.env.osx": {"FOO": "1"}}', 0),
        ],
    )
    def test_host_settings_block_only_guard_disabling_keys(
        self, content: str, expected: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ホスト設定は保護を外せるキーのときだけ deny すること。"""
        payload = {"tool_name": "Write", "tool_input": {"file_path": "/repo/.claude/settings.json", "content": content}}
        assert _run_protection(monkeypatch, payload)[0] == expected
