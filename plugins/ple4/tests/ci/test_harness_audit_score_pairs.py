"""採点述語ごとに「満点になる fixture」と「ならない fixture」を対で固定する。

知識カード `audit-score-measures-existence-not-effectiveness` が要求する形式。
片側（不合格になる fixture）だけを置くと、全部を 0 点にする実装でも緑になり、
逆に片側（合格する fixture）だけだと、全部を満点にする実装でも緑になる。両方を
同じファイルに並べて初めて「述語が入力に応じて動いている」ことの証跡になる。

判定は述語ヘルパーではなく `get_repo_checks` / `build_report` が返すチェックの
``pass`` に対して行う。ヘルパー単体を検査しても、将来そのヘルパーを呼ばなくなる
配線変更が緑のまま通ってしまい、カードが指摘している穴がそのまま残るため。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ple4.ci.harness_audit as harness_audit
from ple4.ci.harness_audit_repo_checks import get_repo_checks

_WRAPPER = '"${CLAUDE_PLUGIN_ROOT}/runtime/ple4-hook"'


def _repo_check_pass(root_dir: Path, check_id: str) -> bool:
    """repo モードのチェック 1 件の合否を返す。"""
    checks = {check["id"]: check for check in get_repo_checks(root_dir)}
    return checks[check_id]["pass"]


def _consumer_check_pass(root_dir: Path, check_id: str) -> bool:
    """consumer モードのチェック 1 件の合否を返す。"""
    report = harness_audit.build_report("repo", root_dir=root_dir, target_mode="consumer")
    return next(check for check in report["checks"] if check["id"] == check_id)["pass"]


def _write_skill(root_dir: Path, name: str, frontmatter: str) -> None:
    """skills/<name>/SKILL.md を frontmatter 付きで書き出す。"""
    skill_dir = root_dir / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n本文\n", encoding="utf-8")


def _write_hooks_json(root_dir: Path, hooks: dict) -> None:
    """テスト用の hooks/hooks.json を書き出す。"""
    hooks_dir = root_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def _write_pyproject(root_dir: Path, body: str) -> None:
    """テスト用の pyproject.toml と最小のテスト surface を書き出す。"""
    (root_dir / "pyproject.toml").write_text(body, encoding="utf-8")
    tests_dir = root_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")


class TestContextAlwaysOnBudget:
    """`context-always-on-budget`（2pts）は description を行ではなく値で測る。

    ``description:`` の行を 1 行だけ数えていた頃は、YAML のブロックスカラーが
    2 文字として計上され、実際には数千〜数万文字の常時注入 description でも
    予算 4,000 を満たしているように見えた。下の fixture がその形を再現する。
    （本リポジトリ自身にブロックスカラー description は無く、旧算法 3,672 /
    新算法 3,637 で合否は変わらなかった。穴は実在するが未顕在だった。）
    """

    def test_short_description_passes(self, tmp_path: Path) -> None:
        """予算内の description なら合格すること（満点になる fixture）。"""
        _write_skill(tmp_path, "small", "name: small\ndescription: 短い説明")

        assert _repo_check_pass(tmp_path, "context-always-on-budget") is True

    def test_block_scalar_description_over_budget_fails(self, tmp_path: Path) -> None:
        """ブロックスカラーで折り返した超過 description は不合格になること。

        旧実装では ``description: >`` の行だけを見て 2 文字と数えたため、この
        fixture が満点で通っていた（H-8 の実測再現）。
        """
        folded = "\n".join(f"  {'あ' * 80}" for _ in range(60))
        _write_skill(tmp_path, "huge", f"name: huge\ndescription: >\n{folded}")

        assert _repo_check_pass(tmp_path, "context-always-on-budget") is False


class TestCoverageGateConfigured:
    """`eval-tests-presence`（2pts）はカバレッジ閾値の有効値を要求する。

    ``"fail_under" in pyproject.toml`` の部分文字列照合だった頃は、ゲートが
    無効（0）でもコメントアウトでも満点だった。
    """

    def test_effective_threshold_passes(self, tmp_path: Path) -> None:
        """`fail_under = 100` は合格すること（満点になる fixture）。"""
        _write_pyproject(tmp_path, "[tool.coverage.report]\nfail_under = 100\n")

        assert _repo_check_pass(tmp_path, "eval-tests-presence") is True

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("ゲート無効", "[tool.coverage.report]\nfail_under = 0\n"),
            ("コメントアウト", "[tool.coverage.report]\n# fail_under = 100 (disabled)\n"),
            ("bool は閾値ではない", "[tool.coverage.report]\nfail_under = true\n"),
            ("別セクションの同名キー", "[tool.other]\nfail_under = 100\n"),
            ("TOML として壊れている", "[tool.coverage.report\nfail_under = 100\n"),
        ],
    )
    def test_ineffective_threshold_fails(self, tmp_path: Path, label: str, body: str) -> None:
        """ゲートとして機能しない書き方は不合格になること。"""
        _write_pyproject(tmp_path, body)

        assert _repo_check_pass(tmp_path, "eval-tests-presence") is False, label

    def test_float_threshold_passes(self, tmp_path: Path) -> None:
        """coverage.py が受ける float 閾値も有効値として扱うこと。"""
        _write_pyproject(tmp_path, "[tool.coverage.report]\nfail_under = 99.5\n")

        assert _repo_check_pass(tmp_path, "eval-tests-presence") is True


class TestSecurityPreflightHook:
    """`security-prompt-hook`（2pts）は実ガードモジュールの起動を要求する。

    エントリの有無だけを見ていた頃は、何もしない ``command: "true"`` でも満点に
    なった。同ファイルの `_event_has_matching_command` が argv 照合を既に実装して
    いるのに、セキュリティ側だけ「有無」に留まっていた非対称の解消。
    """

    def test_real_guard_module_passes(self, tmp_path: Path) -> None:
        """実ガードモジュールを起動していれば合格すること（満点になる fixture）。"""
        _write_hooks_json(
            tmp_path,
            {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": f"{_WRAPPER} ple4.hooks.block_no_verify"}]}
                ]
            },
        )

        assert _repo_check_pass(tmp_path, "security-prompt-hook") is True

    def test_real_repo_hooks_json_passes(self) -> None:
        """本リポジトリの hooks.json が合格すること（厳格化の巻き込み防止）。"""
        plugin_root = Path(__file__).resolve().parents[2]

        assert _repo_check_pass(plugin_root, "security-prompt-hook") is True

    @pytest.mark.parametrize(
        ("label", "hooks"),
        [
            (
                "何もしないコマンド",
                {"PreToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]},
            ),
            (
                "wrapper 経由だが無関係なモジュール",
                {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": f"{_WRAPPER} ple4.hooks.pre_compact"}]}
                    ]
                },
            ),
            ("空配列", {"PreToolUse": []}),
            (
                "ガードが実行前イベントに載っていない",
                {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": f"{_WRAPPER} ple4.hooks.config_protection"}]}
                    ]
                },
            ),
        ],
    )
    def test_hook_without_real_guard_fails(self, tmp_path: Path, label: str, hooks: dict) -> None:
        """保護の実体を伴わない宣言は不合格になること。"""
        _write_hooks_json(tmp_path, hooks)

        assert _repo_check_pass(tmp_path, "security-prompt-hook") is False, label

    def test_beforesubmitprompt_alone_passes(self, tmp_path: Path) -> None:
        """ガードが beforeSubmitPrompt だけに載っていても合格すること。

        イベント間を ``all`` で結ぶと、ガードの置き場所が片方へ寄っただけで
        満点を失う。本チェックの契約は「実行前ガードが含まれている」であって
        両イベントの同時宣言ではない。
        """
        _write_hooks_json(
            tmp_path,
            {
                "beforeSubmitPrompt": [
                    {"hooks": [{"type": "command", "command": f"{_WRAPPER} ple4.hooks.config_protection"}]}
                ]
            },
        )

        assert _repo_check_pass(tmp_path, "security-prompt-hook") is True


class TestConsumerTestSuiteVendorPruning:
    """`consumer-test-suite`（4pts）はベンダーツリーのテストを数えない。

    `_walk_dir` に枝刈りが無かったため、`node_modules/dep/a.spec.js` を 1 個
    置いただけのツリーが `has_file_with_extension` 経由で満点を取れた。同じ
    ``pass`` 式に並ぶ `has_python_tests` は枝刈り済みという非対称だった。
    """

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`find_plugin_install` が実 HOME を読まないよう固定する。"""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def test_project_owned_spec_file_passes(self, tmp_path: Path) -> None:
        """プロジェクト自身の spec ファイルなら合格すること（満点になる fixture）。"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.spec.js").write_text("it('x', () => {});\n", encoding="utf-8")

        assert _consumer_check_pass(tmp_path, "consumer-test-suite") is True

    @pytest.mark.parametrize("vendor_dir", ["node_modules", "vendor", ".venv"])
    def test_vendor_only_spec_file_fails(self, tmp_path: Path, vendor_dir: str) -> None:
        """ベンダーツリーにしかテストが無ければ不合格になること（H-8 の実測再現）。"""
        nested = tmp_path / vendor_dir / "dep"
        nested.mkdir(parents=True)
        (nested / "a.spec.js").write_text("it('x', () => {});\n", encoding="utf-8")

        assert _consumer_check_pass(tmp_path, "consumer-test-suite") is False

    def test_vendor_only_python_tests_fail(self, tmp_path: Path) -> None:
        """`has_python_tests` 側の枝刈りが共通化後も効いていること。"""
        nested = tmp_path / "node_modules" / "dep"
        nested.mkdir(parents=True)
        (nested / "test_dep.py").write_text("def test_x(): pass\n", encoding="utf-8")

        assert _consumer_check_pass(tmp_path, "consumer-test-suite") is False
