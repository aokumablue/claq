"""agents/skills/commands md 内の相対 .md 参照が実在することを検証する構造テスト。

バッククォートで囲まれた `../` または `references/` 始まりの .md 参照を、
記載ファイル自身の場所を基準に解決して存在を確認する。
スキル本文の参照切れは実行時の Read 失敗（プロンプト破損）に直結するため CI で検出する。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_DIRS = ("agents", "skills", "commands")
_REF_PATTERN = re.compile(r"`((?:\.\./|references/)[^`\n]+?\.md)`")


def _iter_md_files() -> Iterator[Path]:
    """検証対象の md ファイルを列挙する。"""
    for dirname in _TARGET_DIRS:
        yield from sorted((_ROOT / dirname).rglob("*.md"))


def test_relative_md_references_resolve() -> None:
    """バッククォート内の相対 .md 参照がすべて実在ファイルに解決されること。"""
    broken: list[str] = []
    for md_file in _iter_md_files():
        text = md_file.read_text(encoding="utf-8")
        for match in _REF_PATTERN.finditer(text):
            target = (md_file.parent / match.group(1)).resolve()
            if not target.is_file():
                broken.append(f"{md_file.relative_to(_ROOT)}: `{match.group(1)}`")
    assert broken == [], "解決できない md 参照:\n" + "\n".join(broken)


def test_dead_code_cleaner_and_harness_tuner_have_missing_input_fail_contract() -> None:
    """入力不足時に即 FAIL する契約が 2 エージェント定義に明示されていること。"""
    cleaner = (_ROOT / "agents" / "dead-code-cleaner.md").read_text(encoding="utf-8")
    tuner = (_ROOT / "agents" / "harness-tuner.md").read_text(encoding="utf-8")

    assert "FAIL" in cleaner
    assert "対象パスまたは diff" in cleaner
    assert "リポジトリ全体の探索は行わない" in cleaner

    assert "FAIL" in tuner
    assert "baseline JSON が無い場合は直ちに **FAIL**" in tuner
    assert "自分で1回だけ採取" not in tuner
    assert "自己収集" not in tuner


def test_readme_command_table_matches_filesystem() -> None:
    """README の Commands 表リンクが `commands/*.md` の実体と過不足なく一致すること。

    リンク切れだけでなく、ファイルは存在するのに表から抜けている（在庫ドリフト）も検出する。
    """
    readme = (_ROOT.parent.parent / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\(plugins/bluecore/commands/([a-z-]+\.md)\)", readme))
    actual = {p.name for p in (_ROOT / "commands").glob("*.md")}
    assert linked == actual, f"README Commands 表と実体の差分: リンクのみ={linked - actual} 実体のみ={actual - linked}"


def test_readme_agent_skill_command_counts_match_filesystem() -> None:
    """README 内 `Agents (N)` / `Skills (N` / `Commands (N)` の件数表記が実体と一致すること。

    README.md v0.9.30 時点では `Agents (16)`（実 13）のような在庫ドリフトが
    起きていた。表記が複数箇所にあってもすべて実数と一致することを確認する。
    """
    readme = (_ROOT.parent.parent / "README.md").read_text(encoding="utf-8")
    counts = {
        "agents": len(list((_ROOT / "agents").glob("*.md"))),
        "skills": len([p for p in (_ROOT / "skills").iterdir() if p.is_dir()]),
        "commands": len(list((_ROOT / "commands").glob("*.md"))),
    }

    for label, pattern, key in (
        ("Agents", r"Agents \((\d+)\)", "agents"),
        ("Skills", r"Skills \((\d+)", "skills"),
        ("Commands", r"Commands \((\d+)\)", "commands"),
    ):
        matches = re.findall(pattern, readme)
        assert matches, f"README に `{label} (N)` 表記が見つからない"
        for n in matches:
            assert int(n) == counts[key], f"README の {label} 件数表記 {n} が実体 {counts[key]} と不一致"


_VENDOR_PATH_MARKERS = (".copilot", ".grok", "installed-plugins", "CLAUDE_PLUGIN_ROOT")
_BASH_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_ENV_POINTER_LINE = '. "$HOME/.bluecore/env.sh"'


def test_md_surfaces_have_no_vendor_specific_paths() -> None:
    """agents/skills/commands の md にベンダ固有パスが 1 件も出現しないこと（M-02 対応）。

    ``CLAUDE_PLUGIN_ROOT`` は Bash tool の環境変数に乗らない（実測）ため、
    plugin root の解決はもう md に書かない。``~/.bluecore/env.sh`` ポインタ
    （``bluecore.lib.env_pointer``、全 hook 起動時に launcher が書く）だけを
    md から参照する設計にした。``.copilot`` / ``.grok`` / ``installed-plugins`` /
    ``CLAUDE_PLUGIN_ROOT`` が re-appear したら、この設計が崩れて元のホスト別
    候補探索ループへ戻っている印。``src/`` の ``lib/grok_plugin_root.py`` は
    正当にベンダパスを持つ別モジュールなので対象外（このテストの対象は
    agents/commands/skills の md のみ）。
    """
    violations: list[str] = []
    for md_file in _iter_md_files():
        text = md_file.read_text(encoding="utf-8")
        for marker in _VENDOR_PATH_MARKERS:
            if marker in text:
                violations.append(f"{md_file.relative_to(_ROOT)}: ベンダ固有マーカー `{marker}` を含む")
    assert violations == [], "\n".join(violations)


def test_bluecore_run_bash_fences_bootstrap_in_same_block() -> None:
    """``bluecore_run``/``bluecore_mem_learn`` を呼ぶ ```bash フェンスは、同一フェンス内に
    ``. "$HOME/.bluecore/env.sh"`` の bootstrap を持つこと（M-02 対応）。

    shell 関数は Bash tool 呼び出しを跨いで継続しない。「別のコードブロックで
    source 済み」という前提は実行時に ``bluecore_run: command not found``
    （exit 127）になるため、呼び出しと bootstrap は必ず同一フェンスに置く。
    """
    violations: list[str] = []
    for md_file in _iter_md_files():
        text = md_file.read_text(encoding="utf-8")
        for block in _BASH_FENCE_RE.findall(text):
            if ("bluecore_run" in block or "bluecore_mem_learn" in block) and _ENV_POINTER_LINE not in block:
                violations.append(f"{md_file.relative_to(_ROOT)}: bootstrap を欠く bash フェンス: {block[:80]!r}")
    assert violations == [], "\n".join(violations)


def test_security_auditor_has_no_bash_access() -> None:
    """security-auditor は tools frontmatter で Bash を持たない（F-05 対応）。

    reviewer は ruff check / test_cmd 再実行が職務で Bash を保持するが、
    security-auditor の 10 項目チェックリストは Read/Grep/Glob だけで
    完結し、本文も「コマンド実行はしない」と宣言している。frontmatter
    の tools からも Bash を外し、権限として技術的に強制する。
    """
    frontmatter = (_ROOT / "agents" / "security-auditor.md").read_text(encoding="utf-8").split("---")[1]
    tools_line = next(line for line in frontmatter.splitlines() if line.strip().startswith("tools:"))
    declared_tools = {tool.strip() for tool in tools_line.split(":", 1)[1].split(",")}

    assert "Bash" not in declared_tools
    assert "Edit" not in declared_tools
    assert "Write" not in declared_tools
