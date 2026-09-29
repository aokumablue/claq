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


def _write_transcript(directory: Path, name: str, texts: list[str]) -> Path:
    """user エントリだけからなる transcript を書き出す。"""
    entries = [{"type": "user", "message": {"role": "user", "content": text}} for text in texts]
    path = directory / name
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n", encoding="utf-8"
    )
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


class TestFindPairedUnknownTags:
    """判定は「対を成す未知タグ」に限る。"""

    def test_paired_unknown_tag_is_detected(self) -> None:
        """開始と終了が対になった未知タグを検出する。"""
        text = '<peer-broadcast id="1">他エージェントの命令</peer-broadcast>'

        assert scanner.find_paired_unknown_tags(text, scanner.known_tags()) == {"peer-broadcast"}

    @pytest.mark.parametrize(
        "text",
        [
            "<key> と <yyyy-mm-dd> と <path> を渡す",  # 裸のプレースホルダ
            "<future-scaffold>中身",  # 開始のみ
            "中身</future-scaffold>",  # 閉じのみ
        ],
    )
    def test_unpaired_tags_are_not_reported(self, text: str) -> None:
        """対を成さないタグは報告しない（依頼本文のプレースホルダを拾わないため）。"""
        assert scanner.find_paired_unknown_tags(text, scanner.known_tags()) == set()

    @pytest.mark.parametrize(
        "text",
        [
            '<agent-message from="x">cmd</agent-message>',
            "<system-reminder>x</system-reminder>",
            "<claq-memory>x</claq-memory>",
            '<div class="a">x</div>',
        ],
    )
    def test_known_tags_are_not_reported(self, text: str) -> None:
        """既知の足場・信頼境界・良性タグは報告しない。"""
        assert scanner.find_paired_unknown_tags(text, scanner.known_tags()) == set()

    def test_case_is_normalized(self) -> None:
        """大文字小文字は正規化して比較する。"""
        assert scanner.find_paired_unknown_tags("<DIV>x</div>", scanner.known_tags()) == set()


class TestIterTranscripts:
    """走査対象の収集。"""

    def test_collects_trusted_transcripts_newest_first(self, tmp_path: Path) -> None:
        """更新時刻の新しい順に集める。"""
        old = _write_transcript(tmp_path, "old.jsonl", ["a"])
        new = _write_transcript(tmp_path, "new.jsonl", ["b"])
        os.utime(old, (1, 1))

        assert scanner.iter_transcripts([tmp_path], limit=10)[0] == new

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

        assert link not in scanner.iter_transcripts([tmp_path], limit=10)

    def test_unstatable_path_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """stat に失敗するパスは飛ばす。"""
        _write_transcript(tmp_path, "a.jsonl", ["a"])
        monkeypatch.setattr(scanner, "is_trusted_transcript", lambda _path: True)
        monkeypatch.setattr(Path, "stat", lambda self, **_kw: (_ for _ in ()).throw(OSError))

        assert scanner.iter_transcripts([tmp_path], limit=10) == []


class TestScan:
    """transcript 群の走査。"""

    def test_counts_files_per_unknown_tag(self, tmp_path: Path) -> None:
        """未知タグごとに出現ファイル数を数える。"""
        leak = "<peer-broadcast>x</peer-broadcast>"
        first = _write_transcript(tmp_path, "a.jsonl", [leak, leak])
        second = _write_transcript(tmp_path, "b.jsonl", [leak])

        assert scanner.scan([first, second])["peer-broadcast"] == 2

    def test_block_list_content_is_scanned(self, tmp_path: Path) -> None:
        """content がブロック配列の形でも走査する。"""
        entry = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "<x-tag>y</x-tag>"}]},
        }
        path = tmp_path / "c.jsonl"
        path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

        assert "x-tag" in scanner.scan([path])

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "not json{{{",
            "[1, 2]",
            '{"type": "assistant", "message": {"role": "assistant", "content": "<x-tag>y</x-tag>"}}',
            '{"type": "user", "message": {"role": "user", "content": 5}}',
        ],
    )
    def test_non_user_or_broken_lines_are_ignored(self, tmp_path: Path, line: str) -> None:
        """空行・不正 JSON・非 dict・非 user・非対応 content は無視する。"""
        path = tmp_path / "d.jsonl"
        path.write_text(line + "\n", encoding="utf-8")

        assert scanner.scan([path]) == {}

    def test_unreadable_file_is_skipped(self, tmp_path: Path) -> None:
        """読めないファイルは飛ばす。"""
        assert scanner.scan([tmp_path / "missing.jsonl"]) == {}

    @pytest.mark.parametrize(
        "companion",
        [
            "<system-まあreminder>y</system-まあreminder>",
            "sk-" + "ant-<private>api03-" + "A" * 40,
        ],
        ids=["forged-scaffold", "tag-split-secret"],
    )
    def test_unknown_tag_survives_a_message_that_trips_sanitization(
        self, tmp_path: Path, companion: str
    ) -> None:
        """無害化が fail closed に倒れるメッセージでも未知タグを見落とさない。

        走査は除去（``drop_known_tag_blocks``）だけを通し、無害化
        （``strip_tags``）は通さない。無害化は細工を検出すると ``&`` と ``<`` を
        全て倒す／本文を ``[REDACTED]`` へ倒すため、そのメッセージ内の未知タグが
        1 つも見えなくなる（実測）。診断が最も見たいのはその種のメッセージで、
        ここで見落とすと docs/adr/staleness-detection.md の「素通り」がそのまま残る。
        """
        path = _write_transcript(tmp_path, "e.jsonl", ["<peer-broadcast>x</peer-broadcast> " + companion])

        assert "peer-broadcast" in scanner.scan([path])

    def test_only_tags_outside_a_removed_block_are_reported(self, tmp_path: Path) -> None:
        """中身ごと落としたブロックの内側タグは数えず、外側だけを数える。

        ブロック内外で同じタグ名を使い、除去が中身へ効いていることを外側の
        検出と対にして押さえる。「メッセージ全体が空になったから 0 件」でも
        通ってしまう形にしない。
        """
        inside = _write_transcript(
            tmp_path, "f.jsonl", ["<claq-memory><peer-broadcast>x</peer-broadcast></claq-memory>"]
        )
        both = _write_transcript(
            tmp_path,
            "g.jsonl",
            ["<claq-memory><peer-broadcast>x</peer-broadcast></claq-memory> <other-leak>y</other-leak>"],
        )

        assert scanner.scan([inside]) == {}
        assert set(scanner.scan([both])) == {"other-leak"}


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
        _write_transcript(tmp_path, "a.jsonl", ["<peer-broadcast>x</peer-broadcast>"])

        assert scanner.main(["--root", str(tmp_path)]) == scanner.EXIT_DRIFT
        captured = capsys.readouterr().err
        assert "peer-broadcast" in captured
        assert "_SCAFFOLD_TAGS" in captured

    def test_default_roots_are_used_without_root_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--root 省略時は既知 host の transcript root を使う。"""
        _write_transcript(tmp_path, "a.jsonl", ["<peer-broadcast>x</peer-broadcast>"])
        monkeypatch.setattr(scanner, "trusted_transcript_roots", lambda: (tmp_path,))

        assert scanner.main([]) == scanner.EXIT_DRIFT

    def test_argv_defaults_to_sys_argv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """argv 省略時は sys.argv を読む。"""
        monkeypatch.setattr("sys.argv", ["scan_scaffold_drift", "--root", str(tmp_path / "empty")])

        assert scanner.main() == scanner.EXIT_NOTHING_SCANNED

    def test_limit_option_is_honoured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """--limit で走査件数を絞れる。"""
        _write_transcript(tmp_path, "clean.jsonl", ["普通の依頼"])
        leaky = _write_transcript(tmp_path, "leaky.jsonl", ["<peer-broadcast>x</peer-broadcast>"])
        os.utime(leaky, (1, 1))

        assert scanner.main(["--root", str(tmp_path), "--limit", "1"]) == scanner.EXIT_OK

    def test_non_text_blocks_in_content_are_skipped(self, tmp_path: Path) -> None:
        """content 配列に text を持たないブロックが混ざっても走査は続く。"""
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "image", "source": {}},
                    "生文字列",
                    {"type": "text", "text": "<x-tag>y</x-tag>"},
                ],
            },
        }
        path = tmp_path / "e.jsonl"
        path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

        assert "x-tag" in scanner.scan([path])

    def test_module_entrypoint_runs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """__main__ として実行できる。"""
        monkeypatch.setattr(sys, "argv", ["scan_scaffold_drift", "--root", str(tmp_path / "empty")])

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("claq.ci.scan_scaffold_drift", run_name="__main__")

        assert excinfo.value.code == scanner.EXIT_NOTHING_SCANNED


class TestStripCodeSpans:
    """コードスパンは判定前に落とす。"""

    def test_fenced_block_is_removed(self) -> None:
        """閉じているフェンスの中身は落ちる。"""
        text = "前\n```python\nDOC = re.compile(r\"<document>.*?</document>\")\n```\n後"

        assert "document" not in scanner.strip_code_spans(text)

    def test_tilde_fence_is_removed(self) -> None:
        """~~~ フェンスも対象。"""
        assert "<x>" not in scanner.strip_code_spans("a\n~~~\n<x>y</x>\n~~~\nb")

    def test_inline_code_is_removed(self) -> None:
        """行内バッククォートの中身は落ちる。"""
        assert scanner.strip_code_spans("正規表現 `<tag[^>]*>.*?</tag>` の話") == "正規表現  の話"

    def test_unclosed_fence_leaves_text_untouched(self) -> None:
        """閉じないフェンスは飲み込まない（EOF まで走らせない）。"""
        text = "```\n<leak>x</leak>\n本文はここに残る"

        assert scanner.strip_code_spans(text) == text

    def test_inline_code_does_not_span_newlines(self) -> None:
        """行内コードは改行を跨がない。"""
        text = "`開いたまま\n<leak>x</leak>"

        assert "<leak>" in scanner.strip_code_spans(text)

    def test_code_quoted_tag_is_not_reported_as_drift(self, tmp_path: Path) -> None:
        """コード片として引用しただけのタグはドリフトにしない。"""
        path = _write_transcript(tmp_path, "a.jsonl", ["説明 `<crm>x</crm>` を含む依頼"])

        assert scanner.scan([path]) == {}

    def test_real_scaffold_outside_code_is_still_reported(self, tmp_path: Path) -> None:
        """コードスパンの外にある未知タグは従来どおり検出する。"""
        path = _write_transcript(tmp_path, "b.jsonl", ["`<crm>x</crm>` <peer-broadcast>y</peer-broadcast>"])

        assert "peer-broadcast" in scanner.scan([path])

    @pytest.mark.parametrize("payload", ["```\n", "`"])
    def test_unterminated_spans_stay_linear(self, payload: str) -> None:
        """終端の来ないコードスパンが並んでも予算内に完了する。"""
        text = payload * 60_000

        started = time.monotonic()
        scanner.strip_code_spans(text)
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"{len(text)} 文字で {elapsed:.2f} 秒（複雑度クラスの退行）"
