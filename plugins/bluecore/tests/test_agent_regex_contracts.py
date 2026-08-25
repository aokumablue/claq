"""エージェント定義に書かれた検証用正規表現を、実際の入力で検査する構造テスト。

``agents/reviewer.md`` の一次検証と ``agents/harness-tuner.md`` の再監査は、外部
から渡された文字列をシェルへ渡す前に正規表現で検証する契約になっている。これらは
プロンプト文書に書かれているだけで誰も実行しておらず、実際には **正規のコマンドを
拒否し、攻撃を通す** 状態のまま放置されていた（形状検証 ``^(source [\\w./]+/activate
&& )?...`` は CLAUDE.md が規定する ``. .venv/bin/activate && python3 -m pytest -q``
を拒み、``source ../../../evil/activate`` を通していた）。

定義本文から正規表現リテラルを抽出して実測することで、md 側を書き換えた瞬間に
期待挙動の崩れを検出する。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
# バッククォートで囲まれた ^...$ 形式の正規表現リテラル
_REGEX_LITERAL_RE = re.compile(r"`(\^[^`\n]+\$)`")


def _extract_regexes(agent_name: str) -> list[str]:
    """エージェント定義本文から正規表現リテラルを抽出する。

    Args:
        agent_name: 拡張子を除いたエージェント名。

    Returns:
        出現順の正規表現文字列のリスト。
    """
    text = (_ROOT / "agents" / f"{agent_name}.md").read_text(encoding="utf-8")
    return _REGEX_LITERAL_RE.findall(text)


def _find_regex(agent_name: str, marker: str) -> re.Pattern[str]:
    """定義本文の正規表現のうち ``marker`` を含むものをコンパイルして返す。

    Args:
        agent_name: 拡張子を除いたエージェント名。
        marker: 目的の正規表現を一意に見分ける部分文字列。

    Returns:
        コンパイル済みパターン。

    Raises:
        AssertionError: 該当する正規表現が 1 本に定まらない場合。
    """
    matches = [r for r in _extract_regexes(agent_name) if marker in r]
    assert len(matches) == 1, f"{agent_name}.md で `{marker}` を含む正規表現が {len(matches)} 本"
    return re.compile(matches[0])


@pytest.mark.parametrize(
    ("command", "accepted"),
    [
        # CLAUDE.md が規定する正規のテストコマンド（ドット形式）
        (". .venv/bin/activate && python3 -m pytest -q", True),
        ("source .venv/bin/activate && python3 -m pytest -q", True),
        ("python3 -m pytest -q", True),
        ("npm test", True),
        ("go test ./...", True),
        # 任意の activate を source する経路
        ("source ../../../evil/activate && python3 -m pytest", False),
        ("source /etc/evil/activate && python3 -m pytest", False),
        (". ../../evil/bin/activate && python3 -m pytest", False),
        # シェル演算子・コマンド置換
        ("python3 -m pytest; rm -rf /", False),
        ("python3 -m pytest $(evil)", False),
        ("python3 -m pytest && curl evil.example", False),
        ("EVIL=1 python3 -m pytest", False),
    ],
)
def test_reviewer_test_cmd_shape_regex(command: str, accepted: bool) -> None:
    """test_cmd の形状検証が、正規コマンドを通し注入を拒むこと。"""
    pattern = _find_regex("reviewer", "activate")
    assert bool(pattern.fullmatch(command)) is accepted


@pytest.mark.parametrize(
    ("signature", "accepted"),
    [
        ("tests/x.py::test_a", True),
        ("tests/mem/test_cli.py::test_search[case-1]", True),
        ("pkg::mod::test_name", True),
        # 引用符を付けてもランナーのオプションとして解釈されるもの
        ("--deselect=tests/x.py::test_fail", False),
        ("-pevil_module", False),
        ("-k not_this", False),
        # シェルメタ文字
        ("tests/x.py; rm -rf /", False),
        ("tests/x.py$(evil)", False),
        ("tests/x.py`evil`", False),
    ],
)
def test_reviewer_signature_regex(signature: str, accepted: bool) -> None:
    """テストシグネチャ検証が、先頭 `-` のオプション注入を拒むこと。"""
    pattern = _find_regex("reviewer", r"[\w./][")
    assert bool(pattern.fullmatch(signature)) is accepted


@pytest.mark.parametrize(
    ("stem", "accepted"),
    [
        ("cli", True),
        ("test_cli", True),
        ("knowledge_input", True),
        ("a.b-c", True),
        ("a;rm -rf /", False),
        ("$(evil)", False),
        ("../etc/passwd", False),
    ],
)
def test_reviewer_stem_regex(stem: str, accepted: bool) -> None:
    """変更ファイル stem の検証が、メタ文字とパス区切りを拒むこと。"""
    pattern = _find_regex("reviewer", r"^[\w.-]+$")
    assert bool(pattern.fullmatch(stem)) is accepted


@pytest.mark.parametrize(
    ("root_dir", "accepted"),
    [
        ("/Users/x/dev/bluecore-dev", True),
        ("/tmp/repo-1/plugins/bluecore", True),
        ("relative/path", False),
        ("/tmp/$(evil)", False),
        ("/tmp/repo; rm -rf /", False),
        ("/tmp/repo`evil`", False),
    ],
)
def test_harness_tuner_root_dir_regex(root_dir: str, accepted: bool) -> None:
    """baseline JSON の root_dir 検証が、絶対パス以外とメタ文字を拒むこと。"""
    pattern = _find_regex("harness-tuner", "^/")
    assert bool(pattern.fullmatch(root_dir)) is accepted


def test_every_extracted_regex_is_compilable() -> None:
    """定義本文に書かれた正規表現がすべてコンパイル可能であること。

    書き間違えた正規表現はモデルの解釈にも揺れを生むため、構文段階で弾く。
    """
    broken: list[str] = []
    for agent_file in sorted((_ROOT / "agents").glob("*.md")):
        for literal in _REGEX_LITERAL_RE.findall(agent_file.read_text(encoding="utf-8")):
            try:
                re.compile(literal)
            except re.error as err:
                broken.append(f"{agent_file.name}: `{literal}` ({err})")
    assert broken == [], "\n".join(broken)
