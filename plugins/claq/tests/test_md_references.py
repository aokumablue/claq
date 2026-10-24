"""agents/skills/commands md 内の相対 .md 参照が実在することを検証する構造テスト。

バッククォートで囲まれた `../` または `references/` 始まりの .md 参照を、
記載ファイル自身の場所を基準に解決して存在を確認する。
スキル本文の参照切れは実行時の Read 失敗（プロンプト破損）に直結するため CI で検出する。
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_DIRS = ("agents", "skills", "commands")
_MD_REF_RE = re.compile(r"`([^`\n]+\.md)`")
# リポジトリ内パスとして解決してはいけない参照。各エントリに理由を持たせる。
_NON_REPO_MD_REFS = {
    "CLAUDE.md": "利用者側リポジトリのファイル",
    "README.md": "利用者側リポジトリのファイル",
    "docs/adr/README.md": "利用者側リポジトリのファイル",
    "transcript.md": "eval の run 成果物（実行時に生成）",
}
# claq リポジトリのルート基準で解決する出典表記。配布物（プラグインディレクトリ）
# には同梱されないため referrer 相対では解決できないが、リポジトリ側では実在する
# 必要がある（綴り間違い・ADR 削除を検出する）。
_REPO_ROOT_MD_REF_PREFIXES = ("docs/adr/",)
_REPO_ROOT = _ROOT.parent.parent


@functools.cache
def _md_corpus() -> tuple[tuple[Path, str], ...]:
    """検証対象の md ファイルと本文の組を列挙する。"""
    return tuple(
        (path, path.read_text(encoding="utf-8"))
        for dirname in _TARGET_DIRS
        for path in sorted((_ROOT / dirname).rglob("*.md"))
    )


def test_relative_md_references_resolve() -> None:
    """バッククォート内の .md 参照が、記載ファイルからの相対で実在に解決されること。

    実行時にこの md を読むモデルの cwd は利用者のプロジェクトであり、plugin root
    相対も repo root 相対も解決手段を持たない。referrer 相対だけが唯一実行可能な
    形式なので、その基準のみで検査する。

    ``../`` と ``references/`` 始まりだけを対象にすると、
    ``skills/checkpoint/SKILL.md``（repo root 相対のつもり）や ``schemas.md``
    （別ディレクトリのファイル）のような参照が検査を素通りする。

    referrer 相対で解決しても**プラグインディレクトリの外へ抜ける参照は不合格**
    にする。``docs/`` はリポジトリ統治文書であって配布物に含まれないため、
    ``../../../../docs/adr/xxxx.md`` はリポジトリでは解決するのに installed build
    では 100% 切れる。リポジトリ基準で検査すると通ってしまうこの死角が、
    ドリフトを生き残らせる。
    出典として ADR を挙げたい場合は ``docs/adr/`` 始まりのリポジトリルート基準表記
    を使う（``_REPO_ROOT_MD_REF_PREFIXES``）。
    """
    broken: list[str] = []
    for md_file, text in _md_corpus():
        for match in _MD_REF_RE.finditer(text):
            ref = match.group(1)
            if ref.startswith("~/") or any(ch in ref for ch in "{<*") or ref in _NON_REPO_MD_REFS:
                continue
            if ref.startswith(_REPO_ROOT_MD_REF_PREFIXES):
                if not (_REPO_ROOT / ref).is_file():
                    broken.append(f"{md_file.relative_to(_ROOT)}: `{ref}`（リポジトリルート基準）")
                continue
            resolved = (md_file.parent / ref).resolve()
            if not resolved.is_file():
                broken.append(f"{md_file.relative_to(_ROOT)}: `{ref}`")
            elif not resolved.is_relative_to(_ROOT):
                broken.append(
                    f"{md_file.relative_to(_ROOT)}: `{ref}` はプラグイン外へ抜ける"
                    f"（配布物に含まれず installed build では解決しない）"
                )
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
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\(plugins/claq/commands/([a-z-]+\.md)\)", readme))
    actual = {p.name for p in (_ROOT / "commands").glob("*.md")}
    assert linked == actual, f"README Commands 表と実体の差分: リンクのみ={linked - actual} 実体のみ={actual - linked}"


def test_readme_agent_skill_command_counts_match_filesystem() -> None:
    """README 内 `Agents (N)` / `Skills (N` / `Commands (N)` の件数表記が実体と一致すること。

    `Agents (16)`（実 13）のような在庫ドリフトを防ぐため、表記が複数箇所に
    あってもすべて実数と一致することを確認する。
    """
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    counts = {
        "Agents": len(list((_ROOT / "agents").glob("*.md"))),
        "Skills": len([p for p in (_ROOT / "skills").iterdir() if p.is_dir()]),
        "Commands": len(list((_ROOT / "commands").glob("*.md"))),
    }

    for label, pattern in (
        ("Agents", r"Agents \((\d+)\)"),
        ("Skills", r"Skills \((\d+)"),
        ("Commands", r"Commands \((\d+)\)"),
    ):
        matches = re.findall(pattern, readme)
        assert matches, f"README に `{label} (N)` 表記が見つからない"
        for n in matches:
            assert int(n) == counts[label], f"README の {label} 件数表記 {n} が実体 {counts[label]} と不一致"


_VENDOR_PATH_MARKERS = (".copilot", ".grok", "installed-plugins", "CLAUDE_PLUGIN_ROOT")

# シェルコマンドを載せるフェンスの言語表記。```bash だけを対象にすると、
# 同じ内容を ```sh / ```shell / ```console で書いた瞬間に、このファイルの
# シェル系検査 3 件（bootstrap 同梱・--status/--source 禁止・呼び出し可能シンボル
# 在庫）が無言でスキップされ、「書き方を変えるだけで検査が消える」状態になる。
# `_COMMENT_HASH_FENCE_RE` と同じ語彙にし、両者が同じ定数を共有することで
# この取りこぼしを防ぐ。
_SHELL_FENCE_LANGS = ("bash", "sh", "shell", "zsh", "console")
_BASH_FENCE_RE = re.compile(rf"```(?:{'|'.join(_SHELL_FENCE_LANGS)})\n(.*?)```", re.DOTALL)
_ENV_POINTER_LINE = '. "$HOME/.claq/env.sh"'


def test_md_surfaces_have_no_vendor_specific_paths() -> None:
    """agents/skills/commands の md にベンダ固有パスが 1 件も出現しないこと。

    ``CLAUDE_PLUGIN_ROOT`` は Bash tool の環境変数に乗らない（実測）ため、
    plugin root の解決はもう md に書かない。``~/.claq/env.sh`` ポインタ
    （``claq.lib.env_pointer``、全 hook 起動時に launcher が書く）だけを
    md から参照する設計にした。``.copilot`` / ``.grok`` / ``installed-plugins`` /
    ``CLAUDE_PLUGIN_ROOT`` が re-appear したら、この設計が崩れて元のホスト別
    候補探索ループへ戻っている印。``src/`` の ``lib/grok_plugin_root.py`` は
    正当にベンダパスを持つ別モジュールなので対象外（このテストの対象は
    agents/commands/skills の md のみ）。
    """
    violations = [
        f"{md_file.relative_to(_ROOT)}: ベンダ固有マーカー `{marker}` を含む"
        for md_file, text in _md_corpus()
        for marker in _VENDOR_PATH_MARKERS
        if marker in text
    ]
    assert violations == [], "\n".join(violations)


def test_claq_run_bash_fences_bootstrap_in_same_block() -> None:
    """``claq_run``/``claq_mem_learn`` を呼ぶ ```bash フェンスは、同一フェンス内に
    ``. "$HOME/.claq/env.sh"`` の bootstrap を持つこと。

    shell 関数は Bash tool 呼び出しを跨いで継続しない。「別のコードブロックで
    source 済み」という前提は実行時に ``claq_run: command not found``
    （exit 127）になるため、呼び出しと bootstrap は必ず同一フェンスに置く。
    """
    violations = [
        f"{md_file.relative_to(_ROOT)}: bootstrap を欠く bash フェンス: {block[:80]!r}"
        for md_file, text in _md_corpus()
        for block in _BASH_FENCE_RE.findall(text)
        if ("claq_run" in block or "claq_mem_learn" in block) and _ENV_POINTER_LINE not in block
    ]
    assert violations == [], "\n".join(violations)


def test_mem_learn_invocations_pass_no_status_or_source_flag() -> None:
    r"""``claq_mem_learn`` 呼び出しは ``--status``/``--source`` を渡さないこと。

    `runtime/claq-helpers.sh` の `claq_mem_learn` は `--status`/`--source`
    の option parsing を持たない（未知オプションで exit 2）。md がこれらを渡す
    記述のまま残ると、記載どおりに実行したときに壊れる。バックスラッシュ
    継続行をまたぐ呼び出しも見逃さないよう、行末 ``\`` を連結してから判定する。
    `list`/`search` の絞り込み用 `--status`（``claq_run claq.mem.cli list
    --status pending`` 等）は対象外 — 判定は ``claq_mem_learn`` を含む論理行に限る。
    """
    violations = [
        f"{md_file.relative_to(_ROOT)}: {line.strip()!r}"
        for md_file, text in _md_corpus()
        for block in _BASH_FENCE_RE.findall(text)
        for line in re.sub(r"\\\n", " ", block).splitlines()
        if "claq_mem_learn" in line and ("--status" in line or "--source" in line)
    ]
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
    """エージェント定義の frontmatter が宣言する tools 集合を返す。"""
    frontmatter = (_ROOT / "agents" / f"{agent_name}.md").read_text(encoding="utf-8").split("---")[1]
    tools_line = next(line for line in frontmatter.splitlines() if line.strip().startswith("tools:"))
    return {tool.strip() for tool in tools_line.split(":", 1)[1].split(",")}


def test_agent_tools_match_expected_exactly() -> None:
    """agents/*.md の実体と tools 宣言が期待マップと完全一致し、どのエージェントも Task を持たないこと。

    security-auditor の read-only は「本文の約束」ではなく tools 権限による技術的
    強制であり、reviewer との権限非対称は docs/adr/definition-and-subagent-design.md の決定そのもの。同ファイルは
    さらに「新規エージェントに Task を与えない（子が子を呼ぶ階層を作らない）」を
    tools 権限で構造的に保証すると定めた。

    Bash/Edit/Write の**非包含**だけを見ると、`Task`（Bash 持ち
    サブエージェントへ委譲できる）や `NotebookEdit` の追加を素通りさせる。権限
    はレビューできる場所に固定しておく必要があるため、集合の完全一致で検査する。
    キーも実体の全件と突き合わせ、エージェントの追加・削除でマップの更新を強制する。
    manifest は agents を列挙せず auto-discovery に任せるため、ここが 9 体の surface の
    欠落を検出する唯一の安全網でもある。Task の不在は期待マップごと書き換えられても
    残るよう、宣言そのものに対して別に見る。
    """
    actual = {p.stem: _declared_tools(p.stem) for p in (_ROOT / "agents").glob("*.md")}
    assert actual == _EXPECTED_AGENT_TOOLS
    assert [name for name, tools in actual.items() if "Task" in tools] == []


_SECTION_REF_RE = re.compile(r"「(#+ [^」]+)」")
_BACKTICK_SECTION_REF_RE = re.compile(r"`(#+ [^`\n。、]+)`")
_HEADING_RE = re.compile(r"^(#+ .+)$", re.M)
_FENCE_RE = re.compile(r"```.*?```", re.S)
# `#` が行コメントである言語のフェンスは見出しを持たない（`# FAIL: ...` はコメント）。
# シェル系は `_SHELL_FENCE_LANGS` を単一情報源として共有する（片側だけ語彙が
# 増える食い違いを構造的に防ぐ）。
_COMMENT_HASH_FENCE_RE = re.compile(
    r"^```(?:" + "|".join((*_SHELL_FENCE_LANGS, "python", "yaml", "yml", "terraform", "toml", "ini")) + r")\b"
)


def _collect_headings(text: str) -> tuple[set[str], set[str]]:
    """見出しを「実見出し」と「コードフェンス内のテンプレート見出し」に分けて返す。

    フェンス内の行をまとめて実見出し扱いすると、出力テンプレート由来の偽見出しが
    dangling 参照を隠す（実測: agents/skills 全体で 64 個）。一方 code-refiner の
    ``## 未確認・スコープ外`` のようにテンプレート内にしか存在しない節も参照される
    ため、除外ではなく別集合として扱う。
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
    """
    for line in text.splitlines():
        targets = tuple(match.group(1) for match in _MD_REF_RE.finditer(line))
        for pattern in (_SECTION_REF_RE, _BACKTICK_SECTION_REF_RE):
            for match in pattern.finditer(line):
                yield match.group(1).strip(), targets


def _resolves(ref: str, headings: tuple[set[str], set[str]]) -> bool:
    """節参照が実見出しまたはテンプレート見出しのいずれかに解決するか判定する。

    前方一致を許すのは見出し側が長い場合のみ（``## モード: benchmark_analysis`` →
    実見出し ``## モード: benchmark_analysis — ベンチマーク結果の分析``）。逆向きは
    現行コーパスで 1 件も必要とされず、削除済み節への参照がたまたま別見出しの
    先頭語を含むだけで通る検出漏れを生むため許さない。
    """
    return any(head == ref or head.startswith(ref) for head in headings[0] | headings[1])


def test_intra_document_section_references_resolve() -> None:
    """節参照が、同一ファイルまたは同一行で指した .md の見出しとして実在すること。

    節を削除・改名したときに参照だけが残ると、モデルは存在しないルールを探し、
    見つからないまま幻覚で補完する。
    test_relative_md_references_resolve はバッククォート付きの相対 .md **ファイル**
    参照しか見ないため、文書内・文書間の節参照はここで検査する。
    """
    broken: list[str] = []
    cache: dict[Path, tuple[set[str], set[str]]] = {}
    for md_file, text in _md_corpus():
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


_PLUGIN_REF_RE = re.compile(r"claq:([a-z][\w-]*)")

# md が名指しする Python モジュールの参照形。`claq_run <module>` だけを見ると、
# md に実在する `python3 -m claq.X` 形（skills/maintain/SKILL.md や
# skills/release-verify/SKILL.md）が import 可能性検査を素通りし、
# モジュールをリネームしても md が dangling のまま緑になる。
#
# モジュール名の各要素を ASCII の Python 識別子に限る理由: `\w` は既定で Unicode
# 対応なので、`[\w.]*` 系のパターンは散文の `python3 -m claq.モジュール名`
# （skills/release-verify/SKILL.md）まで拾って偽 RED を出す（実測:
# `re.search(r"claq[\w.]*\w", "claq.モジュール名")` はマッチする）。さらに
# ドット付き要素を 1 個以上必須にすることで、その散文はどの位置でもマッチしない。
_MODULE_REF_RE = re.compile(r"(?:claq_run|python3 -m)\s+(claq(?:\.[A-Za-z_][A-Za-z0-9_]*)+)")


def test_plugin_component_references_exist() -> None:
    """md が名指しする `claq:<name>` が agents/skills/commands に実在すること。

    エージェント名・スキル名は文字列でしか書けず、綴りを間違えても、または
    リネーム後に参照が残っても、実行するまで気づけない（呼び出し元は該当なし
    として黙って別経路へ倒れる）。相対 .md パス参照と同じく構造で固定する。
    """
    known = (
        {p.stem for p in (_ROOT / "agents").glob("*.md")}
        | {p.name for p in (_ROOT / "skills").iterdir() if p.is_dir()}
        | {p.stem for p in (_ROOT / "commands").glob("*.md")}
    )
    missing = [
        f"{md_file.relative_to(_ROOT)}: claq:{name}"
        for md_file, text in _md_corpus()
        for name in _PLUGIN_REF_RE.findall(text)
        if name not in known
    ]
    assert missing == [], "実在しない参照:\n" + "\n".join(sorted(set(missing)))


def test_claq_run_module_references_are_importable() -> None:
    """md が `claq_run` に渡す Python モジュールが import 可能であること。

    hooks.json のドット区切り参照と同じく、モジュール名は Python の import 文
    に現れないため、リネームやモジュール削除で静かに壊れる。
    """
    missing = [
        f"{md_file.relative_to(_ROOT)}: {module}"
        for md_file, text in _md_corpus()
        for module in _MODULE_REF_RE.findall(text)
        if importlib.util.find_spec(module) is None
    ]
    assert missing == [], "import できないモジュール参照:\n" + "\n".join(sorted(set(missing)))


# ルーブリック版を書き写している md と、そこから版を取り出す正規表現。
# 実装の定数を含めて 3 箇所が同じ文字列を持つ。1 箇所しか照合しないと
# 残りが取り残され、版を上げても古い版のスコアと比較する助言が出せてしまう。
_RUBRIC_VERSION_SITES = ((Path("agents") / "harness-tuner.md", re.compile(r'"rubric_version":\s*"([^"]+)"')),)
_RUBRIC_VERSION_ANY_RE = re.compile(r'(?:ルーブリック版: `|"rubric_version":\s*")(\d{4}-\d{2}-\d{2})')


def test_md_rubric_version_matches_audit_constant() -> None:
    """ルーブリック版を書き写している md がすべて実装の定数と一致し、照合箇所の外にも食い違う版が無いこと。

    スコアはルーブリック版の中でのみ比較可能で、版が上がると過去のベースラインとは
    比較不能になる。実装側の定数と md の散文は別々に書かれるため、照合しないと
    片方だけ更新しても何も落ちず、比較不能なスコア比較が静かに通る。
    照合箇所を列挙する方式は新しい書き写し先が増えたときに取りこぼすため、md 全体の
    「日付形式の rubric 版らしき文字列」も実装と照合し、`_RUBRIC_VERSION_SITES` の更新漏れ自体を検出する。
    """
    from claq.ci.harness_audit import RUBRIC_VERSION

    for relative_path, pattern in _RUBRIC_VERSION_SITES:
        found = pattern.findall((_ROOT / relative_path).read_text(encoding="utf-8"))
        assert found, f"{relative_path} にルーブリック版の表記が見つからない（{pattern.pattern}）"
        assert set(found) == {RUBRIC_VERSION}, (
            f"{relative_path} のルーブリック版 {found} が実装の {RUBRIC_VERSION} と不一致"
        )
    stale = [
        f"{md_file.relative_to(_ROOT)}: {version}"
        for md_file, text in _md_corpus()
        for version in _RUBRIC_VERSION_ANY_RE.findall(text)
        if version != RUBRIC_VERSION
    ]
    assert stale == [], "実装の RUBRIC_VERSION と食い違う版表記:\n" + "\n".join(sorted(stale))


# md が `name(...)` の呼び出し形で名指しするシンボル。実装側の関数名と md の散文は
# 別々に書かれるため、リネームや実装取り止めで静かに壊れる（md が指示する関数が
# src/ にも runtime/claq-helpers.sh にも存在しないと、手順が実行不能になる）。
_MD_CALLABLE_RE = re.compile(r"`([a-z_][a-z0-9_]*)\([^`)]*\)`")
_SHELL_FUNC_RE = re.compile(r"^([a-z_][a-z0-9_]*)\(\)", re.M)
_PY_DEF_RE = re.compile(r"^\s*def ([a-z_][a-z0-9_]*)\(", re.M)
_HELPERS_SH = _ROOT / "runtime" / "claq-helpers.sh"

# 実装シンボルではない呼び出し表記と、その理由。
_MD_CALLABLE_EXEMPTIONS = {
    "fetch": "SSRF の説明に使う汎用の擬似コード（agents/security-auditor.md）",
    "search": "検証手段の書き方を示す例示（agents/planner.md）",
    # CSS の組み込み関数はブラウザ側の言語仕様であって、このリポジトリの
    # シンボルではない。リネームで壊れる対象ではないので在庫検査から外す。
    # 逆に列挙を広げすぎると本来の drift 検出が緩むため、実際に md で使う
    # 関数だけを名指しする（skills/html-gen）。
    "var": "CSS のカスタムプロパティ参照（skills/html-gen）",
    "clamp": "CSS の流体サイズ関数（skills/html-gen）",
    "minmax": "CSS グリッドのトラックサイズ関数（skills/html-gen）",
    "oklch": "CSS の色関数。shadcn/ui のトークンがこの表記で配られる（skills/html-gen）",
    "model_dump": "pydantic の API。SDK の実測記録が SDK 内部の呼び方を引用する（skills/mcp-gen）",
}


def _function_names(path: Path) -> set[str]:
    """Python ファイルが定義する関数の名前を集める。"""
    return set(_PY_DEF_RE.findall(path.read_text(encoding="utf-8")))


@functools.cache
def _shell_helpers() -> frozenset[str]:
    """runtime/claq-helpers.sh が定義するシェル関数の名前を集める。"""
    return frozenset(_SHELL_FUNC_RE.findall(_HELPERS_SH.read_text(encoding="utf-8")))


@functools.cache
def _shared_callables() -> frozenset[str]:
    """全ての md が名指ししてよい名前（src/ の Python 関数と runtime シェルヘルパ）を集める。"""
    return _shell_helpers().union(*(_function_names(py) for py in (_ROOT / "src").rglob("*.py")))


@functools.cache
def _skill_asset_callables(skill: str) -> frozenset[str]:
    """スキルのスケルトン（skills/<name>/assets）の Python 関数の名前を集める。

    スケルトンは生成先へ複製される実装であり、名指ししてよいのはそのスキルの md だけ。
    """
    return frozenset(name for py in (_ROOT / "skills" / skill).glob("assets/**/*.py") for name in _function_names(py))


def _defined_callables(md_file: Path) -> frozenset[str]:
    """md が名指ししてよい関数の名前を返す。

    src/ の Python 関数と runtime シェルヘルパの名前は全ての md に効く。スキルのスケルトン
    （skills/<name>/assets）の関数の名前は、同じ skills/<name>/ 配下の md にだけ効く。全スキルの
    スケルトンの名前を全ての md に効かせると、open・view・resolve のような汎用の名前が
    スケルトンと無関係な md でも定義済みとして通り、ずれを見逃す。
    """
    parts = md_file.relative_to(_ROOT).parts
    if parts[0] != "skills":
        return _shared_callables()
    return _shared_callables() | _skill_asset_callables(parts[1])


def test_md_named_callables_are_defined() -> None:
    """md が `name(...)` 形で名指しする関数が実装に存在すること。

    存在しない関数の呼び出しを手順に書くと、記載どおり実行した時点で詰まる。
    静的 validator も既存のリンクチェッカも、md 本文が名指しする**シンボル**の
    実在は見ていなかった。スキルのスケルトンの関数は、そのスキルの md でだけ定義済みとみなす
    （`_defined_callables`）。
    """
    missing = [
        f"{md_file.relative_to(_ROOT)}: `{name}(...)`"
        for md_file, text in _md_corpus()
        for name in _MD_CALLABLE_RE.findall(text)
        if name not in _defined_callables(md_file) and name not in _MD_CALLABLE_EXEMPTIONS
    ]
    assert missing == [], "実装に存在しない呼び出し表記:\n" + "\n".join(sorted(set(missing)))
    assert all(_MD_CALLABLE_EXEMPTIONS.values()), "免除には理由が要る"


def test_skill_asset_callables_apply_only_to_their_skill() -> None:
    """スケルトンの関数名が、同じスキルの md でだけ定義済みになり、スキルの外の md では未定義のままであること。

    本体テストはコーパス経由でしか `_defined_callables` を実行しないため、範囲を全ての md に
    戻しても何も落ちない（範囲が広がると検出が緩むだけ）。範囲そのものを直接固定する。
    """
    skills = sorted(path.name for path in (_ROOT / "skills").iterdir() if path.is_dir())
    owner = next(skill for skill in skills if _skill_asset_callables(skill) - _shared_callables())
    other = next(skill for skill in skills if skill != owner)
    exclusive = _skill_asset_callables(owner) - _shared_callables() - _skill_asset_callables(other)
    assert exclusive, f"{owner} のスケルトンにだけある関数名が無い（検査の前提が崩れている）"
    assert exclusive <= _defined_callables(_ROOT / "skills" / owner / "references" / "any.md")
    for outside in (
        _ROOT / "agents" / "reviewer.md",
        _ROOT / "commands" / "any.md",
        _ROOT / "skills" / other / "SKILL.md",
    ):
        assert exclusive.isdisjoint(_defined_callables(outside)), outside.relative_to(_ROOT)


def test_md_shell_helper_invocations_are_defined() -> None:
    """bash フェンスが呼ぶ claq_* / collect_* ヘルパが実在すること。

    シェル関数名は Python の import に現れないため、リネームしても何も落ちず、
    実行時に exit 127（command not found）として初めて現れる。
    """
    missing = [
        f"{md_file.relative_to(_ROOT)}: {token}"
        for md_file, text in _md_corpus()
        for block in _BASH_FENCE_RE.findall(text)
        for token in re.findall(r"(?<![\w./-])((?:claq|collect)_[a-z0-9_]+)", block)
        if token not in _shell_helpers()
    ]
    assert missing == [], "未定義のシェルヘルパ呼び出し:\n" + "\n".join(sorted(set(missing)))


def test_md_max_score_matches_real_audit() -> None:
    """harness-tuner.md の baseline スキーマ例と、md が散文へ書き写した `max_score` が実装の実測値と一致すること。

    この値はエージェントが「満点いくつの尺度か」を読み取る唯一の手がかりで、
    ずれると改善幅の見積もりが黙って狂う。実装側は check の points 合計なので、
    check を足すたびに変わる。JSON 例だけを照合すると、散文へ書き写した
    「`max_score`（`repo` では N）」の形は素通りする。
    docs/adr/definition-and-subagent-design.md の「定義に書いた検証規則は CI で実測する」の適用。

    監査は ``check=True`` で起動しない。監査は「check が 1 件でも不合格」でも exit 1 を
    返すため、``max_score``（不合格でも必ず出力される値）を読むだけのこのテストが
    無関係な不合格に巻き込まれる。監査全体の合否は `tests/ci/test_harness_audit_score_pairs.py` の
    ``test_real_repo_passes_every_repo_check`` が check id を名指しで見る。
    """
    result = subprocess.run(
        [sys.executable, "-m", "claq.ci.harness_audit", "repo", "--format", "json", "--root", str(_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.stdout.strip(), f"監査が JSON を出力しなかった（exit={result.returncode}）: {result.stderr.strip()}"
    real = json.loads(result.stdout)["max_score"]
    tuner = (_ROOT / "agents" / "harness-tuner.md").read_text(encoding="utf-8")
    schema = json.loads(re.search(r"```json\n(\{.*?\})\n```", tuner, re.DOTALL).group(1))

    assert schema["max_score"] == real
    stale = [
        f"{md_file.relative_to(_ROOT)}: {value}"
        for md_file, text in _md_corpus()
        for value in re.findall(r"`repo` では(\d+)", text)
        if int(value) != real
    ]
    assert stale == [], f"実装の max_score={real} と食い違う満点表記:\n" + "\n".join(sorted(stale))
