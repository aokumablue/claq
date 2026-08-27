"""agents/skills/commands md 内の相対 .md 参照が実在することを検証する構造テスト。

バッククォートで囲まれた `../` または `references/` 始まりの .md 参照を、
記載ファイル自身の場所を基準に解決して存在を確認する。
スキル本文の参照切れは実行時の Read 失敗（プロンプト破損）に直結するため CI で検出する。
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_DIRS = ("agents", "skills", "commands")
_REF_PATTERN = re.compile(r"`([^`\n]+\.md)`")
# リポジトリ内パスとして解決してはいけない参照。各エントリに理由を持たせる。
_NON_REPO_MD_REFS = {
    "CLAUDE.md": "利用者側リポジトリのファイル",
    "README.md": "利用者側リポジトリのファイル",
    "docs/adr/README.md": "利用者側リポジトリのファイル",
    "user_notes.md": "eval の run 成果物（実行時に生成）",
    "checkpoint-2026-05-09-article-loop.md": "命名例として本文に書かれたファイル名",
}


def _iter_md_files() -> Iterator[Path]:
    """検証対象の md ファイルを列挙する。"""
    for dirname in _TARGET_DIRS:
        yield from sorted((_ROOT / dirname).rglob("*.md"))


def test_relative_md_references_resolve() -> None:
    """バッククォート内の .md 参照が、記載ファイルからの相対で実在に解決されること。

    実行時にこの md を読むモデルの cwd は利用者のプロジェクトであり、plugin root
    相対も repo root 相対も解決手段を持たない。referrer 相対だけが唯一実行可能な
    形式なので、その基準のみで検査する。

    以前は ``../`` と ``references/`` 始まりだけを対象にしていたため、
    ``skills/checkpoint/SKILL.md``（repo root 相対のつもり）や ``schemas.md``
    （別ディレクトリのファイル）のような参照が検査を素通りしていた。
    """
    broken: list[str] = []
    for md_file in _iter_md_files():
        text = md_file.read_text(encoding="utf-8")
        for match in _REF_PATTERN.finditer(text):
            ref = match.group(1)
            if ref.startswith("~/") or any(ch in ref for ch in "{<*") or ref in _NON_REPO_MD_REFS:
                continue
            if not (md_file.parent / ref).resolve().is_file():
                broken.append(f"{md_file.relative_to(_ROOT)}: `{ref}`")
    assert broken == [], "解決できない md 参照:\n" + "\n".join(broken)


def test_code_refiner_and_harness_tuner_have_missing_input_fail_contract() -> None:
    """入力不足時に即 FAIL する契約が 2 エージェント定義に明示されていること。"""
    refiner = (_ROOT / "agents" / "code-refiner.md").read_text(encoding="utf-8")
    tuner = (_ROOT / "agents" / "harness-tuner.md").read_text(encoding="utf-8")

    assert "FAIL" in refiner
    assert "対象パスまたは diff" in refiner
    assert "リポジトリ全体の探索は行わない" in refiner

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


def test_mem_learn_invocations_pass_no_status_or_source_flag() -> None:
    r"""``bluecore_mem_learn`` 呼び出しは ``--status``/``--source`` を渡さないこと（H-01 対応）。

    `runtime/bluecore-helpers.sh` の `bluecore_mem_learn` は `--status`/`--source`
    の option parsing を持たない（未知オプションで exit 2）。md がこれらを渡す
    記述のまま残ると、記載どおりに実行したときに壊れる。バックスラッシュ
    継続行をまたぐ呼び出しも見逃さないよう、行末 ``\`` を連結してから判定する。
    `list`/`search` の絞り込み用 `--status`（``bluecore_run bluecore.mem.cli list
    --status pending`` 等）は対象外 — 判定は ``bluecore_mem_learn`` を含む論理行に限る。
    """
    violations: list[str] = []
    for md_file in _iter_md_files():
        text = md_file.read_text(encoding="utf-8")
        for block in _BASH_FENCE_RE.findall(text):
            joined = re.sub(r"\\\n", " ", block)
            for line in joined.splitlines():
                if "bluecore_mem_learn" in line and ("--status" in line or "--source" in line):
                    violations.append(f"{md_file.relative_to(_ROOT)}: {line.strip()!r}")
    assert violations == [], "\n".join(violations)


_EXPECTED_AGENT_TOOLS = {
    "bench-analyzer": {"Read", "Grep", "Glob"},
    "code-refiner": {"Read", "Grep", "Glob", "Edit", "Write", "Bash"},
    "comparator": {"Read", "Grep", "Glob"},
    "grader": {"Read", "Grep", "Glob"},
    "harness-tuner": {"Read", "Grep", "Glob", "Edit", "Write", "Bash"},
    "planner": {"Read", "Grep", "Glob"},
    "reviewer": {"Read", "Grep", "Glob", "Bash"},
    "security-auditor": {"Read", "Grep", "Glob"},
    "tdd-writer": {"Read", "Grep", "Glob", "Edit", "Write", "Bash"},
}


def _declared_tools(agent_name: str) -> set[str]:
    """エージェント定義の frontmatter が宣言する tools 集合を返す。

    Args:
        agent_name: 拡張子を除いたエージェント名。

    Returns:
        宣言されたツール名の集合。
    """
    frontmatter = (_ROOT / "agents" / f"{agent_name}.md").read_text(encoding="utf-8").split("---")[1]
    tools_line = next(line for line in frontmatter.splitlines() if line.strip().startswith("tools:"))
    return {tool.strip() for tool in tools_line.split(":", 1)[1].split(",")}


def test_agent_tools_match_expected_exactly() -> None:
    """9 定義の tools 宣言が期待集合と完全一致すること（F-05 / ADR-0004 / ADR-0010）。

    security-auditor の read-only は「本文の約束」ではなく tools 権限による技術的
    強制であり、reviewer との権限非対称は ADR-0004 の決定そのもの。ADR-0010 は
    さらに「新規エージェントに Task を与えない（子が子を呼ぶ階層を作らない）」を
    tools 権限で構造的に保証すると定めた。

    以前の実装は Bash/Edit/Write の**非包含**だけを見ていたため、`Task`（Bash 持ち
    サブエージェントへ委譲できる）や `NotebookEdit` の追加を素通りさせていた。権限
    はレビューできる場所に固定しておく必要があるため、集合の完全一致で検査する。
    """
    actual = {name: _declared_tools(name) for name in _EXPECTED_AGENT_TOOLS}
    assert actual == _EXPECTED_AGENT_TOOLS


def test_agent_files_match_expected_tools_map() -> None:
    """tools 期待マップが agents/*.md の実体と過不足なく対応すること。

    エージェントを追加・削除したときにマップの更新を強制する（マップから漏れた
    定義は test_agent_tools_match_expected_exactly の検査を受けない）。
    """
    actual = {p.stem for p in (_ROOT / "agents").glob("*.md")}
    assert actual == set(_EXPECTED_AGENT_TOOLS)


def test_no_agent_can_spawn_subagents() -> None:
    """どのエージェントも Task を持たないこと（ADR-0010「子が子を呼ばない」）。"""
    holders = [name for name in _EXPECTED_AGENT_TOOLS if "Task" in _declared_tools(name)]
    assert holders == []


_SECTION_REF_RE = re.compile(r"「(#+ [^」]+)」")
_BACKTICK_SECTION_REF_RE = re.compile(r"`(#+ [^`\n。、]+)`")
_MD_PATH_REF_RE = re.compile(r"`([^`\n]+\.md)`")
_HEADING_RE = re.compile(r"^(#+ .+)$", re.M)
_FENCE_RE = re.compile(r"```.*?```", re.S)
# `#` が行コメントである言語のフェンスは見出しを持たない（`# FAIL: ...` はコメント）
_COMMENT_HASH_FENCE_RE = re.compile(r"^```(?:bash|sh|shell|zsh|console|python|yaml|yml|terraform|toml|ini)\b")


def _collect_headings(text: str) -> tuple[set[str], set[str]]:
    """見出しを「実見出し」と「コードフェンス内のテンプレート見出し」に分けて返す。

    フェンス内の行をまとめて実見出し扱いすると、出力テンプレート由来の偽見出しが
    dangling 参照を隠す（実測: agents/skills 全体で 64 個）。一方 code-refiner の
    ``## 未確認・スコープ外`` のようにテンプレート内にしか存在しない節も参照される
    ため、除外ではなく別集合として扱う。

    Args:
        text: Markdown ファイルの全文。

    Returns:
        (実見出しの集合, テンプレート見出しの集合)。
    """
    template: set[str] = set()
    for match in _FENCE_RE.finditer(text):
        block = match.group(0)
        if _COMMENT_HASH_FENCE_RE.match(block):
            continue
        template |= {m.group(1).strip() for m in _HEADING_RE.finditer(block)}
    real = {m.group(1).strip() for m in _HEADING_RE.finditer(_FENCE_RE.sub("", text))}
    return real, template


def _iter_section_refs(text: str) -> Iterator[tuple[str, tuple[str, ...]]]:
    """節参照と、その解決先候補（同一行に現れる .md パスすべて）を列挙する。

    鉤括弧形式（「## 節名」）とバッククォート形式（``## 節名``）の両方を拾う。
    同じ行に ``.md`` パス参照があれば、その節は他ファイルの見出しを指しうる
    (checkpoint → loop-dev の ``## Human Gate`` 等)。1 行が複数のファイルへ言及
    することがある（loop-dev の record 行は learn と checkpoint の両方を挙げつつ
    checkpoint 側の節を参照する）ため、最初の 1 件ではなく全件を候補として返す。

    Args:
        text: Markdown ファイルの全文。

    Yields:
        (節見出しテキスト, 解決先候補の相対パスのタプル)。候補が無ければ空タプル。
    """
    for line in text.splitlines():
        targets = tuple(match.group(1) for match in _MD_PATH_REF_RE.finditer(line))
        for pattern in (_SECTION_REF_RE, _BACKTICK_SECTION_REF_RE):
            for match in pattern.finditer(line):
                yield match.group(1).strip(), targets


def _resolves(ref: str, headings: tuple[set[str], set[str]]) -> bool:
    """節参照が実見出しまたはテンプレート見出しのいずれかに解決するか判定する。

    前方一致を許すのは見出し側が長い場合のみ（``## モード: benchmark_analysis`` →
    実見出し ``## モード: benchmark_analysis — ベンチマーク結果の分析``）。逆向きは
    現行コーパスで 1 件も必要とされず、削除済み節への参照がたまたま別見出しの
    先頭語を含むだけで通る検出漏れを生むため許さない。

    Args:
        ref: 参照された節見出しテキスト。
        headings: _collect_headings の戻り値。

    Returns:
        解決すれば True。
    """
    return any(head == ref or head.startswith(ref) for head in headings[0] | headings[1])


def test_intra_document_section_references_resolve() -> None:
    """節参照が、同一ファイルまたは同一行で指した .md の見出しとして実在すること。

    節を削除・改名したときに参照だけが残ると、モデルは存在しないルールを探し、
    見つからないまま幻覚で補完する。実例として 5d200fc が ``## 確信度ゲート`` を
    撤去した際、agents/reviewer.md に参照が 2 箇所残った。
    test_relative_md_references_resolve はバッククォート付きの相対 .md **ファイル**
    参照しか見ないため、文書内・文書間の節参照はどのテストにも掛かっていなかった。
    """
    broken: list[str] = []
    cache: dict[Path, tuple[set[str], set[str]]] = {}
    for md_file in _iter_md_files():
        text = md_file.read_text(encoding="utf-8")
        cache[md_file] = _collect_headings(text)
        for ref, targets in _iter_section_refs(text):
            candidates = [cache[md_file]]
            for target in targets:
                target_path = (md_file.parent / target).resolve()
                if not target_path.is_file():
                    continue  # パス自体の実在は test_relative_md_references_resolve の担当
                if target_path not in cache:
                    cache[target_path] = _collect_headings(target_path.read_text(encoding="utf-8"))
                candidates.append(cache[target_path])
            if not any(_resolves(ref, headings) for headings in candidates):
                where = f" (候補: {', '.join(targets)})" if targets else ""
                broken.append(f"{md_file.relative_to(_ROOT)}: 「{ref}」{where}")
    assert broken == [], "見つからない節参照:\n" + "\n".join(broken)


def test_section_ref_helpers_detect_and_accept() -> None:
    """節参照ヘルパーが、壊れた参照を検出し正しい参照を通すこと。

    本体テストはコーパス経由でしかヘルパーを実行しないため、正規表現を将来
    狭めても静かに検出範囲だけが縮む。陽性・陰性を直接固定する。
    """
    broken_doc = "## 絞り込み基準\n\n本文（確信度ゲートは「## 確信度ゲート」に従う）\n"
    assert [ref for ref, _ in _iter_section_refs(broken_doc)] == ["## 確信度ゲート"]
    assert not _resolves("## 確信度ゲート", _collect_headings(broken_doc))

    ok_doc = "## 原則\n\n確信度の扱いは「## 原則」に従う。\n"
    assert _resolves("## 原則", _collect_headings(ok_doc))

    # 実見出しとフェンス内テンプレート見出しを取り違えない
    fenced = "## 出力形式\n\n```\n## 未確認・スコープ外\n```\n\n`## 未確認・スコープ外` に理由を明記する。\n"
    real, template = _collect_headings(fenced)
    assert "## 未確認・スコープ外" in template and "## 未確認・スコープ外" not in real
    assert _resolves("## 未確認・スコープ外", (real, template))

    # `#` が行コメントの言語フェンスは見出しを持たない
    assert _collect_headings("```bash\n# 開発リポジトリでは repo 版を source する\n```\n")[1] == set()

    # 他ファイルを指す参照は解決先ファイルとして返る
    cross = "- Human Gate 4 点 → `../loop-dev/SKILL.md` `## Human Gate`\n"
    assert list(_iter_section_refs(cross)) == [("## Human Gate", ("../loop-dev/SKILL.md",))]

    # 1 行が複数ファイルへ言及する場合、最初の 1 件に決め打たない
    multi = "基準は `../learn/SKILL.md`。収束状況は `## 反復履歴` が単一情報源（`../checkpoint/SKILL.md` 参照）。\n"
    assert list(_iter_section_refs(multi)) == [("## 反復履歴", ("../learn/SKILL.md", "../checkpoint/SKILL.md"))]


_PLUGIN_REF_RE = re.compile(r"bluecore:([a-z][\w-]*)")
_MODULE_REF_RE = re.compile(r"bluecore_run\s+(bluecore[\w.]*)")


def test_plugin_component_references_exist() -> None:
    """md が名指しする `bluecore:<name>` が agents/skills/commands に実在すること。

    エージェント名・スキル名は文字列でしか書けず、綴りを間違えても、または
    リネーム後に参照が残っても、実行するまで気づけない（呼び出し元は該当なし
    として黙って別経路へ倒れる）。相対 .md パス参照と同じく構造で固定する。
    """
    known = (
        {p.stem for p in (_ROOT / "agents").glob("*.md")}
        | {p.name for p in (_ROOT / "skills").iterdir() if p.is_dir()}
        | {p.stem for p in (_ROOT / "commands").glob("*.md")}
    )
    missing: list[str] = []
    for md_file in _iter_md_files():
        for match in _PLUGIN_REF_RE.finditer(md_file.read_text(encoding="utf-8")):
            if match.group(1) not in known:
                missing.append(f"{md_file.relative_to(_ROOT)}: bluecore:{match.group(1)}")
    assert missing == [], "実在しない参照:\n" + "\n".join(sorted(set(missing)))


def test_bluecore_run_module_references_are_importable() -> None:
    """md が `bluecore_run` に渡す Python モジュールが import 可能であること。

    hooks.json のドット区切り参照と同じく、モジュール名は Python の import 文
    に現れないため、リネームやモジュール削除で静かに壊れる。
    """
    missing: list[str] = []
    for md_file in _iter_md_files():
        for match in _MODULE_REF_RE.finditer(md_file.read_text(encoding="utf-8")):
            module = match.group(1)
            if importlib.util.find_spec(module) is None:
                missing.append(f"{md_file.relative_to(_ROOT)}: {module}")
    assert missing == [], "import できないモジュール参照:\n" + "\n".join(sorted(set(missing)))
