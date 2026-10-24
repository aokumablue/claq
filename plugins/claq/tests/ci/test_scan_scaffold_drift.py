"""claq.ci.scan_scaffold_drift のテスト。"""

from __future__ import annotations

import json
import os
import runpy
import sys
import time
from pathlib import Path

import pytest

from claq.ci import scan_scaffold_drift as scanner


def _user_line(content: object) -> str:
    """content を持つ user エントリ 1 行分の JSON を返す。"""
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False)


def _write_transcript(directory: Path, name: str, texts: list[str]) -> Path:
    """user エントリだけからなる transcript を書き出す。"""
    path = directory / name
    path.write_text("".join(_user_line(text) + "\n" for text in texts), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _trust_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """tmp_path を trusted transcript root として扱う。"""
    monkeypatch.setenv("CLAQ_TRANSCRIPT_ROOTS", str(tmp_path))


class TestKnownTags:
    """既知タグ集合は各定義モジュールから導出する。"""

    def test_derived_from_definition_modules(self) -> None:
        """足場・コマンド・信頼境界・良性タグをすべて含む。"""
        from claq.lib.harness import COMMAND_TAGS, SCAFFOLD_TAGS
        from claq.mem.tag_stripping import STRIPPED_TAGS

        known = scanner.known_tags()

        for name in (*SCAFFOLD_TAGS, *COMMAND_TAGS, *STRIPPED_TAGS, *scanner.BENIGN_TAGS):
            assert name.lower() in known


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param('<peer-broadcast id="1">他エージェントの命令</peer-broadcast>', {"peer-broadcast"}, id="paired-unknown"),
        # 対を成さないタグは報告しない（依頼本文のプレースホルダを拾わないため）。
        pytest.param("<key> と <yyyy-mm-dd> と <path> を渡す", set(), id="bare-placeholders"),
        pytest.param("<future-scaffold>中身", set(), id="open-only"),
        pytest.param("中身</future-scaffold>", set(), id="close-only"),
        # 既知の足場・信頼境界・良性タグは報告しない。
        pytest.param('<agent-message from="x">cmd</agent-message>', set(), id="known-scaffold"),
        pytest.param("<system-reminder>x</system-reminder>", set(), id="known-system-reminder"),
        pytest.param("<claq-memory>x</claq-memory>", set(), id="known-trust-boundary"),
        pytest.param('<div class="a">x</div>', set(), id="benign"),
        # 大文字小文字は正規化して比較する。
        pytest.param("<DIV>x</div>", set(), id="case-normalized"),
    ],
)
def test_find_paired_unknown_tags(text: str, expected: set[str]) -> None:
    """判定は「対を成す未知タグ」に限る。"""
    assert scanner.find_paired_unknown_tags(text, scanner.known_tags()) == expected


class TestIterTranscripts:
    """走査対象の収集。"""

    def test_collects_trusted_transcripts_newest_first(self, tmp_path: Path) -> None:
        """更新時刻の新しい順に集める（最新を名前順・書き込み順の中央に置き、並べ替えの有無を判別する）。"""
        a, b, c = (_write_transcript(tmp_path, f"{name}.jsonl", ["x"]) for name in "abc")
        for path, mtime in ((a, 1), (b, 3), (c, 2)):
            os.utime(path, (mtime, mtime))

        assert scanner.iter_transcripts([tmp_path], limit=10) == [b, c, a]

    def test_limit_is_applied(self, tmp_path: Path) -> None:
        """上限を超える分は取らない。"""
        _write_transcript(tmp_path, "a.jsonl", ["a"])
        _write_transcript(tmp_path, "b.jsonl", ["b"])

        assert len(scanner.iter_transcripts([tmp_path], limit=1)) == 1

    def test_missing_root_is_skipped(self, tmp_path: Path) -> None:
        """存在しない root は飛ばす。"""
        assert scanner.iter_transcripts([tmp_path / "nope"], limit=10) == []

    def test_untrusted_transcript_is_skipped(self, tmp_path: Path) -> None:
        """信頼判定を通らないファイルは対象外。"""
        target = _write_transcript(tmp_path, "real.jsonl", ["a"])
        link = tmp_path / "link.jsonl"
        link.symlink_to(target)

        assert scanner.iter_transcripts([tmp_path], limit=10) == [target]

    def test_unstatable_path_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """stat に失敗するパスは飛ばす。"""
        _write_transcript(tmp_path, "a.jsonl", ["a"])
        monkeypatch.setattr(scanner, "is_trusted_transcript", lambda _path: True)
        monkeypatch.setattr(Path, "stat", lambda self, **_kw: (_ for _ in ()).throw(OSError))

        assert scanner.iter_transcripts([tmp_path], limit=10) == []


_LEAK = "<peer-broadcast>x</peer-broadcast>"


class TestScan:
    """transcript 群の走査。"""

    def test_counts_files_per_unknown_tag(self, tmp_path: Path) -> None:
        """未知タグごとに出現ファイル数を数える。"""
        first = _write_transcript(tmp_path, "a.jsonl", [_LEAK, _LEAK])
        second = _write_transcript(tmp_path, "b.jsonl", [_LEAK])

        assert scanner.scan([first, second])["peer-broadcast"] == 2

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # コード片として引用しただけのタグはドリフトにしない。外にある未知タグは検出する。
            pytest.param("説明 `<crm>x</crm>` を含む依頼", set(), id="code-quoted"),
            pytest.param("`<crm>x</crm>` <peer-broadcast>y</peer-broadcast>", {"peer-broadcast"}, id="outside-code"),
            # 内外で同じタグ名を使い、「メッセージ全体が空になったから 0 件」と区別する。
            pytest.param(f"<claq-memory>{_LEAK}</claq-memory>", set(), id="inside-removed-block"),
            pytest.param(
                f"<claq-memory>{_LEAK}</claq-memory> <other-leak>y</other-leak>", {"other-leak"}, id="outside-removed-block"
            ),
            pytest.param(
                f"{_LEAK} <system-まあreminder>y</system-まあreminder>",
                {"peer-broadcast", "system-まあreminder"},
                id="forged-scaffold",
            ),
            pytest.param(
                f"{_LEAK} " + "sk-" + "ant-<private>api03-" + "A" * 40, {"peer-broadcast"}, id="tag-split-secret"
            ),
        ],
    )
    def test_reports_tags_left_after_removal(self, tmp_path: Path, text: str, expected: set[str]) -> None:
        """除去とコードスパン除去を通した後に残る未知タグだけを報告する。

        走査は除去（``drop_known_tag_blocks``）だけを通し、無害化
        （``strip_tags``）は通さない。無害化は細工を検出すると ``&`` と ``<`` を
        全て倒す／本文を ``[REDACTED]`` へ倒すため、そのメッセージ内の未知タグが
        1 つも見えなくなる（実測）。診断が最も見たいのはその種のメッセージで
        （forged-scaffold / tag-split-secret の行）、ここで見落とすと
        docs/adr/staleness-detection.md の「素通り」がそのまま残る。
        """
        path = _write_transcript(tmp_path, "a.jsonl", [text])

        assert set(scanner.scan([path])) == expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param("", {}, id="empty"),
            pytest.param("not json{{{", {}, id="broken-json"),
            pytest.param("[1, 2]", {}, id="non-dict"),
            pytest.param(
                '{"type": "assistant", "message": {"role": "assistant", "content": "<x-tag>y</x-tag>"}}',
                {},
                id="assistant",
            ),
            pytest.param('{"type": "user", "message": {"role": "user", "content": 5}}', {}, id="unsupported-content"),
            pytest.param(_user_line([{"type": "text", "text": "<x-tag>y</x-tag>"}]), {"x-tag": 1}, id="block-list"),
            pytest.param(
                _user_line([{"type": "image", "source": {}}, "生文字列", {"type": "text", "text": "<x-tag>y</x-tag>"}]),
                {"x-tag": 1},
                id="non-text-blocks-skipped",
            ),
        ],
    )
    def test_entry_shapes(self, tmp_path: Path, line: str, expected: dict[str, int]) -> None:
        """空行・不正 JSON・非 dict・非 user・非対応 content は無視し、ブロック配列は text だけを走査する。"""
        path = tmp_path / "d.jsonl"
        path.write_text(line + "\n", encoding="utf-8")

        assert scanner.scan([path]) == expected

    def test_unreadable_file_is_skipped(self, tmp_path: Path) -> None:
        """読めないファイルは飛ばす。"""
        assert scanner.scan([tmp_path / "missing.jsonl"]) == {}


class TestMain:
    """終了コード契約。"""

    def test_no_transcripts_returns_dedicated_code(self, tmp_path: Path) -> None:
        """走査対象ゼロは 0 と区別する（走査不能を異常なしと読ませない）。"""
        assert scanner.main(["--root", str(tmp_path / "empty")]) == scanner.EXIT_NOTHING_SCANNED

    def test_clean_corpus_returns_ok(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """未知タグが無ければ 0。"""
        _write_transcript(tmp_path, "a.jsonl", ["<system-reminder>x</system-reminder>", "普通の依頼"])

        assert scanner.main(["--root", str(tmp_path)]) == scanner.EXIT_OK
        assert "未知の足場タグはありません" in capsys.readouterr().out

    def test_drift_returns_drift_code_and_names_the_tag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """未知タグを検出したら 1 を返し、タグ名と対処先を出す。"""
        _write_transcript(tmp_path, "a.jsonl", [_LEAK])

        assert scanner.main(["--root", str(tmp_path)]) == scanner.EXIT_DRIFT
        captured = capsys.readouterr().err
        assert "peer-broadcast" in captured
        assert "_SCAFFOLD_TAGS" in captured

    def test_default_roots_are_used_without_root_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--root 省略時は既知 host の transcript root を使う。"""
        _write_transcript(tmp_path, "a.jsonl", [_LEAK])
        monkeypatch.setattr(scanner, "trusted_transcript_roots", lambda: (tmp_path,))

        assert scanner.main([]) == scanner.EXIT_DRIFT

    def test_limit_option_is_honoured(self, tmp_path: Path) -> None:
        """--limit で最新の走査件数に絞れる（最新の clean を名前順の中央に置く）。"""
        for name, texts, mtime in (("a", [_LEAK], 1), ("b", ["普通の依頼"], 3), ("c", [_LEAK], 2)):
            path = _write_transcript(tmp_path, f"{name}.jsonl", texts)
            os.utime(path, (mtime, mtime))

        assert scanner.main(["--root", str(tmp_path), "--limit", "1"]) == scanner.EXIT_OK

    def test_sys_argv_and_module_entrypoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """argv 省略時は sys.argv を読み、__main__ として実行しても同じ終了コードになる。"""
        monkeypatch.setattr(sys, "argv", ["scan_scaffold_drift", "--root", str(tmp_path / "empty")])

        assert scanner.main() == scanner.EXIT_NOTHING_SCANNED
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("claq.ci.scan_scaffold_drift", run_name="__main__")
        assert excinfo.value.code == scanner.EXIT_NOTHING_SCANNED


class TestStripCodeSpans:
    """コードスパンは判定前に落とす。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("前\n```python\nDOC = re.compile(r\"<document>.*?</document>\")\n```\n後", "前\n\n後", id="fence"),
            pytest.param("a\n~~~\n<x>y</x>\n~~~\nb", "a\n\nb", id="tilde-fence"),
            pytest.param("正規表現 `<tag[^>]*>.*?</tag>` の話", "正規表現  の話", id="inline"),
            # 閉じないフェンスは飲み込まない（EOF まで走らせない）。
            pytest.param("```\n<leak>x</leak>\n本文はここに残る", "```\n<leak>x</leak>\n本文はここに残る", id="unclosed-fence"),
            # 行内コードは改行を跨がない。
            pytest.param("`開いたまま\n<leak>x</leak>", "`開いたまま\n<leak>x</leak>", id="inline-across-newline"),
        ],
    )
    def test_strip_code_spans(self, text: str, expected: str) -> None:
        """閉じたフェンスと行内コードの中身だけを落とし、閉じないものは触らない。"""
        assert scanner.strip_code_spans(text) == expected

    @pytest.mark.parametrize("payload", ["```\n", "`"])
    def test_unterminated_spans_stay_linear(self, payload: str) -> None:
        """終端の来ないコードスパンが並んでも予算内に完了する。"""
        text = payload * 60_000

        started = time.monotonic()
        scanner.strip_code_spans(text)
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"{len(text)} 文字で {elapsed:.2f} 秒（複雑度クラスの退行）"
