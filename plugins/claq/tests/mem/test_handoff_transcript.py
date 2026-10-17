"""claq.mem.handoff の transcript の読み込み・信頼判定・要約の組み立てのテスト。

transcript の要約（直近の依頼・ツール・変更ファイル）、trusted transcript root の
allowlist と所有者・symlink 検査、足場・非発話ブロック・重複依頼の扱い、発話ブロックの
allowlist を検証する。明示 handoff 経路と書き込み側の無害化は ``test_handoff.py`` に分ける。
共有する補助と、tmp_path を trusted transcript root にする autouse フィクスチャは
``_support.py`` に置く。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest

from claq.mem import handoff as handoff_mod
from claq.mem.handoff import build_handoff

from ._support import _from_transcript, _user, _write_transcript
from ._support import _trust_tmp_path_as_transcript_root as _trust_tmp_path_as_transcript_root


class TestTranscriptSummary:
    """トランスクリプトからの要約組み立て。"""

    def test_missing_transcript_yields_nothing(self, tmp_path: Path) -> None:
        """トランスクリプトが存在しなければ空文字列。"""
        assert build_handoff({"transcript_path": str(tmp_path / "missing.jsonl")}) == ""

    def test_symlink_transcript_is_rejected(self, tmp_path: Path) -> None:
        """symlink 経由のトランスクリプトは読まない。

        is_file() は symlink を辿ってしまうため、symlink → 任意の
        readable file を指させることで transcript_path の性質検証を
        すり抜けられてはならない。
        """
        target = _write_transcript(tmp_path, [_user("狙われたファイル")])
        link = tmp_path / "transcript_link.jsonl"
        link.symlink_to(target)

        assert build_handoff({"transcript_path": str(link)}) == ""

    def test_transcript_stat_failure_is_treated_as_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_file() 通過後に stat() が失敗した場合も空文字列（race 耐性）。"""
        path = _write_transcript(tmp_path, [_user("消えたファイル")])
        original_stat = Path.stat

        def _flaky_stat(self: Path, *args: object, **kwargs: object) -> object:
            if str(self) == path:
                raise OSError("vanished")
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _flaky_stat)

        assert build_handoff({"transcript_path": path}) == ""

    def test_transcript_owned_by_another_user_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """所有者が実行ユーザーと異なるトランスクリプトは読まない。"""
        path = _write_transcript(tmp_path, [_user("他ユーザーの依頼")])
        monkeypatch.setattr(handoff_mod.os, "getuid", lambda: -1)

        assert build_handoff({"transcript_path": path}) == ""

    def test_transcript_resolve_failure_is_treated_as_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """allowlist 判定用の resolve() 自体が失敗した場合も安全側（空文字列）に倒す。"""
        path = _write_transcript(tmp_path, [_user("依頼")])
        original_resolve = Path.resolve

        def _boom(self: Path, *args: object, **kwargs: object) -> Path:
            if self == Path(path):
                raise OSError("resolve failed")
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _boom)

        assert build_handoff({"transcript_path": path}) == ""

    def test_keeps_only_the_last_three_user_messages(self, tmp_path: Path) -> None:
        """ユーザー依頼は直近 3 件だけ載せる。"""
        entries = [_user(f"依頼{index}") for index in range(5)]

        result = _from_transcript(tmp_path, entries)

        assert result == "直近の依頼:\n- 依頼2\n- 依頼3\n- 依頼4"

    def test_collects_tools_and_modified_files(self, tmp_path: Path) -> None:
        """assistant の tool_use ブロックからツール名と変更ファイルを集める。"""
        entries = [
            _user("cli を直して"),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/cli.py"}},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
                        {"type": "text", "text": "書き換えました"},
                    ],
                },
            },
            {"type": "tool_use", "tool_name": "Write", "tool_input": {"file_path": "src/new.py"}},
        ]

        result = _from_transcript(tmp_path, entries)

        assert result == (
            "直近の依頼:\n- cli を直して\n"
            "変更ファイル: src/cli.py, src/new.py\n"
            "使用ツール: Bash, Edit, Write"
        )

    def test_collects_tools_from_camel_case_and_toolargs_containers(self, tmp_path: Path) -> None:
        """camelCase / toolArgs / JSON 文字列化コンテナの tool_use も収集する。

        ``tool_name`` / ``tool_input`` だけを直接読むと、Grok の
        ``toolInput``・Copilot の ``toolArgs``（JSON 文字列で来ることが多い）を
        取りこぼす。抽出を lib/harness へ寄せていることの回帰防止。
        """
        entries = [
            {"type": "tool_use", "toolName": "Edit", "toolInput": {"file_path": "src/a.py"}},
            {"type": "tool_use", "toolName": "write", "toolArgs": json.dumps({"file_path": "src/b.py"})},
            {"type": "tool_use", "tool_name": "run_terminal_command", "tool_args": {"command": "ls"}},
        ]

        result = _from_transcript(tmp_path, entries)

        assert result == "変更ファイル: src/a.py, src/b.py\n使用ツール: Bash, Edit, Write"

    def test_file_changes_without_user_messages_are_handed_off(self, tmp_path: Path) -> None:
        """ユーザー発話が無くても変更ファイルがあれば引き継ぐ。"""
        entries = [
            {"type": "tool_use", "tool_name": "Edit", "tool_input": {"file_path": "src/cli.py"}},
        ]

        result = _from_transcript(tmp_path, entries)

        assert result == "変更ファイル: src/cli.py\n使用ツール: Edit"

    def test_secrets_in_user_messages_are_redacted(self, tmp_path: Path) -> None:
        """ユーザー発話に貼られたシークレットは圧縮前に除去する。"""
        entries = [_user("鍵は " + "password" + "=hunter2hunter2 で通る")]

        result = _from_transcript(tmp_path, entries)

        assert result == "直近の依頼:\n- 鍵は [REDACTED] で通る"

    def test_long_paths_are_shortened_not_redacted(self, tmp_path: Path) -> None:
        """長い絶対パスは末尾 3 セグメントへ縮め、redact で潰さない。"""
        entries = [
            _user("依頼"),
            {
                "type": "tool_use",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/Users/me/dev/claq-dev/plugins/claq/src/claq/mem/handoff.py"},
            },
            {"type": "tool_use", "tool_name": "Write", "tool_input": {"file_path": "docs/mem.md"}},
        ]

        result = _from_transcript(tmp_path, entries)

        assert "変更ファイル: …/claq/mem/handoff.py, docs/mem.md" in result
        assert "REDACTED" not in result

    def test_normalizes_apply_patch_into_edit(self, tmp_path: Path) -> None:
        """Codex の apply_patch は Edit へ正規化しパッチから対象ファイルを拾う。"""
        patch = "*** Begin Patch\n*** Update File: src/a.py\n*** End Patch"
        entries = [
            _user("codex 経路"),
            {"type": "tool_use", "tool_name": "apply_patch", "tool_input": patch},
        ]

        result = _from_transcript(tmp_path, entries)

        assert "変更ファイル: src/a.py" in result
        assert "使用ツール: Edit" in result

    def test_tools_without_any_context_are_not_handed_off(self, tmp_path: Path) -> None:
        """ユーザー依頼も変更ファイルも無ければツール名だけでは記録しない。"""
        entries = [{"type": "tool_use", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}]

        assert _from_transcript(tmp_path, entries) == ""

    def test_unnamed_tool_entry_is_ignored(self, tmp_path: Path) -> None:
        """ツール名が空の tool_use エントリは無視する。"""
        entries = [_user("依頼"), {"type": "tool_use", "tool_name": "", "name": ""}]

        assert _from_transcript(tmp_path, entries) == "直近の依頼:\n- 依頼"

    @pytest.mark.parametrize(
        "line",
        ["", "   ", "{壊れた JSON", "[1, 2, 3]"],
        ids=["empty", "blank", "invalid-json", "not-an-object"],
    )
    def test_unparseable_lines_are_skipped(self, tmp_path: Path, line: str) -> None:
        """空行・不正 JSON・非オブジェクト行は読み飛ばす。"""
        entries: list[dict | str] = [line, _user("依頼")]

        assert _from_transcript(tmp_path, entries) == "直近の依頼:\n- 依頼"

    @pytest.mark.parametrize(
        "entry",
        [
            {"role": "user", "content": "依頼"},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "依頼"}]}},
            {"type": "user", "content": "依頼"},
        ],
        ids=["role-field", "content-blocks", "top-level-content"],
    )
    def test_accepts_alternative_user_entry_shapes(self, tmp_path: Path, entry: dict) -> None:
        """ハーネスごとに異なるユーザーエントリの形を吸収する。"""
        assert _from_transcript(tmp_path, [entry]) == "直近の依頼:\n- 依頼"

    @pytest.mark.parametrize(
        "entry",
        [
            {"type": "assistant", "message": {"role": "assistant", "content": "応答"}},
            {"type": "user", "message": "文字列メッセージ"},
            {"type": "user", "message": {"role": "user", "content": 42}},
            {"type": "user", "message": {"role": "user", "content": "   "}},
        ],
        ids=["assistant", "non-dict-message", "non-text-content", "blank-text"],
    )
    def test_non_user_text_is_not_collected(self, tmp_path: Path, entry: dict) -> None:
        """ユーザー発話でない、または本文を取り出せないエントリは載せない。"""
        assert _from_transcript(tmp_path, [entry]) == ""

    def test_strips_ansi_and_injected_memory_tags(self, tmp_path: Path) -> None:
        """ANSI エスケープと注入済み記憶タグは本文から落とす。"""
        text = "\x1b[31m<claq-memory>\n## 共通知識\n- [fact] 古い記憶\n</claq-memory>\x1b[0m 次の作業"

        result = _from_transcript(tmp_path, [_user(text)])

        assert result == "直近の依頼:\n- 次の作業"

    def test_reads_only_the_tail_of_a_huge_transcript(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """上限バイトを超えるトランスクリプトは末尾だけ読む。"""
        monkeypatch.setattr(handoff_mod, "TRANSCRIPT_MAX_BYTES", 120)
        entries = [_user("先頭の依頼"), _user("末尾の依頼")]

        result = _from_transcript(tmp_path, entries)

        assert result == "直近の依頼:\n- 末尾の依頼"

    def test_scan_stops_at_the_hard_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """走査がハードタイムアウトを超えたら残りの行を捨てる。"""
        clock = iter([0.0, 0.0, 99.0])
        monkeypatch.setattr(handoff_mod.time, "monotonic", lambda: next(clock))
        entries = [_user("1 行目"), _user("2 行目")]

        result = _from_transcript(tmp_path, entries)

        assert result == "直近の依頼:\n- 1 行目"


class TestTrustedTranscriptRoots:
    """既知 host transcript root の allowlist + CLAQ_TRANSCRIPT_ROOTS 拡張。"""

    def test_transcript_outside_known_roots_is_rejected_without_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """allowlist 外の任意ファイルは、性質検査（symlink/所有者）を通っても要約されない。"""
        monkeypatch.delenv("CLAQ_TRANSCRIPT_ROOTS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        path = _write_transcript(tmp_path, [_user("依頼")])

        assert build_handoff({"transcript_path": path}) == ""

    @pytest.mark.parametrize("subdir", [(".claude", "projects"), (".copilot",), (".codex",)])
    def test_transcript_under_default_host_root_is_trusted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, subdir: tuple[str, ...]
    ) -> None:
        """既知 host（Claude Code / Copilot CLI / Codex）の既定 root 配下は要約される。"""
        monkeypatch.delenv("CLAQ_TRANSCRIPT_ROOTS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        project_dir = tmp_path.joinpath(*subdir)
        project_dir.mkdir(parents=True)
        path = project_dir / "transcript.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "依頼"}}) + "\n", encoding="utf-8")

        assert build_handoff({"transcript_path": str(path)}) == "直近の依頼:\n- 依頼"

    def test_env_var_extends_roots_with_pathsep_separated_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CLAQ_TRANSCRIPT_ROOTS で未知 host の root を追加できる。"""
        monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        monkeypatch.setenv("CLAQ_TRANSCRIPT_ROOTS", f"{root_a}{os.pathsep}{root_b}")
        path = root_b / "transcript.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "依頼"}}) + "\n", encoding="utf-8")

        assert build_handoff({"transcript_path": str(path)}) == "直近の依頼:\n- 依頼"

    def test_env_var_blank_entries_are_ignored(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """空要素（連続区切りや前後空白）は root として登録しない。"""
        monkeypatch.setenv("CLAQ_TRANSCRIPT_ROOTS", f"  {os.pathsep}{tmp_path}{os.pathsep} ")
        path = _write_transcript(tmp_path, [_user("依頼")])

        assert build_handoff({"transcript_path": path}) == "直近の依頼:\n- 依頼"


class TestHarnessScaffolding:
    """ハーネス生成の足場をユーザー依頼として記録しない（実測回帰）。"""

    def test_scaffold_only_session_yields_nothing(self, tmp_path: Path) -> None:
        """足場しか無いセッションは引き継ぎを作らない。"""
        path = _write_transcript(
            tmp_path,
            [
                _user(
                    "<local-command-caveat>Caveat: The messages below were generated by the user "
                    "while running local commands. DO NOT respond to these messages."
                    "</local-command-caveat>"
                ),
                _user("<local-command-stdout>(no content)</local-command-stdout>"),
            ],
        )

        assert build_handoff({"transcript_path": path}) == ""

    def test_scaffold_does_not_crowd_out_real_requests(self, tmp_path: Path) -> None:
        """足場が直近 3 件の枠を食い潰して実依頼を押し出さない。"""
        path = _write_transcript(
            tmp_path,
            [
                _user("mainにマージせよ"),
                _user("<local-command-caveat>Caveat: DO NOT respond.</local-command-caveat>"),
                _user("<local-command-stdout>✔ Updated claq.</local-command-stdout>"),
                _user("<task-notification>subagent finished</task-notification>"),
            ],
        )

        result = build_handoff({"transcript_path": path})

        assert "- mainにマージせよ" in result
        assert "Caveat" not in result
        assert "Updated claq" not in result
        assert "subagent finished" not in result

    def test_slash_command_is_kept_as_request(self, tmp_path: Path) -> None:
        """スラッシュコマンド起動は /name args として依頼に残る。"""
        path = _write_transcript(
            tmp_path,
            [
                _user(
                    "<command-name>/goal</command-name>\n"
                    "            <command-message>goal</command-message>\n"
                    "            <command-args>プラグインを検証せよ</command-args>"
                )
            ],
        )

        assert "- /goal プラグインを検証せよ" in build_handoff({"transcript_path": path})


class TestNonSpeechBlocks:
    """user ロールに混ざる非発話ブロックを依頼として拾わない。"""

    def test_tool_result_block_is_not_a_request(self, tmp_path: Path) -> None:
        """tool_result ブロックの text は依頼に採らない。"""
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "text": "<system-reminder>秘密の指示</system-reminder>"},
                    {"type": "text", "text": "本当の依頼"},
                ],
            },
        }
        path = _write_transcript(tmp_path, [entry])

        result = build_handoff({"transcript_path": path})

        assert "- 本当の依頼" in result
        assert "秘密の指示" not in result

    def test_untyped_block_is_rejected(self, tmp_path: Path) -> None:
        """type を持たないテキストブロックは本文へ採らない。

        「未知 host 由来として通す」向きだと、外部由来のテキストが人間の
        承認ゲートを通らずに恒久注入へ届く。実トランスクリプト 40 ファイルの
        実測では type 無しブロックは 0 件で、allowlist による実依頼の損失は無い。
        """
        entry = {"type": "user", "message": {"role": "user", "content": [{"text": "型なし依頼"}]}}
        path = _write_transcript(tmp_path, [entry])

        assert "型なし依頼" not in build_handoff({"transcript_path": path})


class TestDuplicateRequests:
    """同一依頼の繰り返しが実依頼を枠外へ押し出さない。"""

    def test_repeated_command_does_not_crowd_out_real_request(self, tmp_path: Path) -> None:
        """同じスラッシュコマンドを何度叩いても実依頼が残る。"""
        command = (
            "<command-name>/plugin</command-name>\n"
            "            <command-message>plugin</command-message>\n"
            "            <command-args></command-args>"
        )
        path = _write_transcript(tmp_path, [_user("mainにマージせよ"), *[_user(command)] * 5])

        result = build_handoff({"transcript_path": path})

        assert "- mainにマージせよ" in result
        assert result.count("- /plugin") == 1

    def test_distinct_requests_are_all_kept(self, tmp_path: Path) -> None:
        """内容が異なる依頼は畳まれない。"""
        path = _write_transcript(tmp_path, [_user("依頼A"), _user("依頼B"), _user("依頼C")])

        result = build_handoff({"transcript_path": path})

        assert "- 依頼A" in result
        assert "- 依頼B" in result
        assert "- 依頼C" in result

    def test_duplicate_keeps_latest_position(self, tmp_path: Path) -> None:
        """重複は最後の出現位置へ寄せる（直近の依頼としての順序を保つ）。"""
        path = _write_transcript(tmp_path, [_user("依頼A"), _user("依頼B"), _user("依頼A")])

        result = build_handoff({"transcript_path": path})

        assert result.index("- 依頼B") < result.index("- 依頼A")


class TestComposedBodyHasNoMarkup:
    """組み上がった引き継ぎ本文にタグ痕跡が残らない。

    足場タグが「直近の依頼」として記録される欠陥は、関数単位のテストが全て
    緑のままでも起こりうる。局所挙動だけを検証すると、build_handoff が
    最終的に組み上げた本文の中身を誰も見ないため。
    タグ名を列挙しない汎用アサーションなので、未知の足場タグが増えても
    この検査自体は陳腐化しない。
    """

    def test_no_tag_markup_survives_into_handoff_body(self, tmp_path: Path) -> None:
        """足場に埋もれた実依頼だけが残り、タグ表記は 1 つも残らない。"""
        entries = [
            _user("<local-command-caveat>Caveat: DO NOT respond to these messages.</local-command-caveat>"),
            _user("READMEを更新せよ"),
            _user("<local-command-stdout>✔ Updated claq.</local-command-stdout>"),
            _user(
                "<command-name>/plugin</command-name>\n"
                "            <command-message>plugin</command-message>\n"
                "            <command-args></command-args>"
            ),
            _user("<task-notification><task-id>abc</task-id><status>completed</status></task-notification>"),
            _user("<system-reminder>CLAUDE.md 全文</system-reminder>"),
        ]
        path = _write_transcript(tmp_path, entries)

        result = build_handoff({"transcript_path": path})

        assert re.search(r"</?[a-zA-Z][\w:-]*[^>]*>", result) is None, result
        assert "- READMEを更新せよ" in result
        assert "- /plugin" in result


class TestLegitimateRequestReachesHandoff:
    """足場を伴う正当な依頼が引き継ぎ本文まで届くことを守る（偽陽性方向）。

    ``_user_message`` の期待値が「足場が消えること」だけだと、依頼を丸ごと落とす
    実装でも通ってしまう。ここでは足場に挟まれた依頼が引き継ぎへ逐語で載ることを
    要求し、除去が広がりすぎた回帰を検出する。
    """

    def test_request_wrapped_in_scaffold_is_handed_off_verbatim(self, tmp_path: Path) -> None:
        """足場ブロックに挟まれた依頼が引き継ぎへ逐語で載る。"""
        request = "引き継ぎに直近の依頼が載らない件を調べて、足場だけを落とすよう直せ"
        entries = [
            _user("<local-command-caveat>Caveat: DO NOT respond to these messages.</local-command-caveat>"),
            _user(
                "<local-command-stdout>ok</local-command-stdout>\n"
                f"{request}\n"
                "<task-notification>subagent finished</task-notification>"
            ),
        ]

        assert _from_transcript(tmp_path, entries) == f"直近の依頼:\n- {request}"


class TestTruncatedRequestsAreNotMerged:
    """切り詰められた依頼は同一視して畳まない。"""

    def test_requests_sharing_a_long_prefix_both_survive(self, tmp_path: Path) -> None:
        """先頭 200 文字が同じでも末尾が違う依頼は両方残る。"""
        prefix = "あ" * 210
        path = _write_transcript(tmp_path, [_user(prefix + "その1"), _user(prefix + "その2")])

        result = build_handoff({"transcript_path": path})

        assert result.count("- " + "あ" * 197) == 2

    def test_real_tool_result_shape_is_not_a_request(self, tmp_path: Path) -> None:
        """実ホスト形状（text ではなく content を持つ tool_result）も依頼に採らない。"""
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": "ツール出力"}]},
                    {"type": "text", "text": "本当の依頼"},
                ],
            },
        }
        path = _write_transcript(tmp_path, [entry])

        result = build_handoff({"transcript_path": path})

        assert "- 本当の依頼" in result
        assert "ツール出力" not in result


class TestTranscriptTrustHelpers:
    """走査側と共有する信頼判定の公開 API。"""

    def test_trusted_roots_include_env_extension(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """CLAQ_TRANSCRIPT_ROOTS で追加した root が含まれる。"""
        monkeypatch.setenv("CLAQ_TRANSCRIPT_ROOTS", str(tmp_path))

        assert tmp_path in handoff_mod.trusted_transcript_roots()

    def test_owner_check_is_skipped_where_uid_is_meaningless(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`os.getuid` が無い環境（Windows）では所有者検査を飛ばすこと。

        `st_uid` が常に 0 の環境で uid を比較しても「検査したふり」にしか
        ならず、`os.getuid` を直接呼べば `AttributeError` で transcript が
        一律拒否される。capability で分岐し、残る防御は symlink
        拒否・通常ファイル要求・trusted root 包含。
        """
        monkeypatch.delattr(handoff_mod.os, "getuid", raising=False)
        monkeypatch.setenv("CLAQ_TRANSCRIPT_ROOTS", str(tmp_path))
        path = tmp_path / "transcript.jsonl"
        path.write_text("{}\n", encoding="utf-8")

        assert handoff_mod.is_trusted_transcript(path) is True

    def test_owner_check_fails_closed_when_stat_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """所有者を確認できない場合は信頼しないこと。"""
        monkeypatch.setenv("CLAQ_TRANSCRIPT_ROOTS", str(tmp_path))
        path = tmp_path / "transcript.jsonl"
        path.write_text("{}\n", encoding="utf-8")

        def _boom(self):  # noqa: ANN001, ANN202
            raise OSError("stat failed")

        monkeypatch.setattr(Path, "stat", _boom)

        assert handoff_mod.is_trusted_transcript(path) is False

    def test_symlink_is_not_trusted(self, tmp_path: Path) -> None:
        """symlink は信頼しない。"""
        target = tmp_path / "real.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        link = tmp_path / "link.jsonl"
        link.symlink_to(target)

        assert handoff_mod.is_trusted_transcript(link) is False


class TestSpeechBlockAllowlist:
    """transcript のブロック種別は allowlist で選ぶ（denylist にしない）。

    denylist では「知らない種別＝通す」になるため、transcript に載る外部
    由来テキスト（ツール出力・ファイル内容・web 取得結果）が**人間の承認ゲートを
    通らずに恒久注入へ届く**。handoff は `promote` を通らないので、ここが
    唯一の防壁になる。
    """

    @pytest.mark.parametrize(
        ("label", "block"),
        [
            ("未知の type", {"type": "function_result", "text": "攻撃文字列"}),
            ("未知の type 2", {"type": "tool_output", "text": "攻撃文字列"}),
            ("type キー無し", {"text": "攻撃文字列"}),
            ("既知の非発話", {"type": "tool_result", "text": "攻撃文字列"}),
            ("dict ですらない", "攻撃文字列"),
        ],
    )
    def test_non_speech_blocks_are_dropped(self, label: str, block: object) -> None:
        """発話 allowlist に無いブロックは本文へ採らないこと。"""
        assert handoff_mod._text_content([block]) == "", label

    def test_speech_blocks_survive(self) -> None:
        """正当なユーザー発話は残ること（過検出の対照）。

        実トランスクリプト 40 ファイルの実測では `text` 3 件・生文字列 75 件で、
        未知種別と type 無しは 0 件。allowlist による損失は実測ゼロ。
        """
        assert handoff_mod._text_content([{"type": "text", "text": "認証のバグを直して"}]) == "認証のバグを直して"
        assert handoff_mod._text_content("レビューして") == "レビューして"

    def test_unknown_block_type_is_logged_for_drift(self, caplog: pytest.LogCaptureFixture) -> None:
        """未知の種別を落としたときだけ記録すること。

        allowlist の失敗（未知 host の発話の取りこぼし）を、黙った劣化ではなく
        診断できる状態にするための仕掛け。想定内の非発話は記録しない。
        """
        with caplog.at_level(logging.INFO, logger="claq.mem.HANDOFF"):
            handoff_mod._text_content([{"type": "function_result", "text": "x"}])
            handoff_mod._text_content([{"type": "tool_result", "text": "x"}])

        messages = [r.getMessage() for r in caplog.records]
        assert any("function_result" in m for m in messages)
        assert not any("tool_result" in m for m in messages)
