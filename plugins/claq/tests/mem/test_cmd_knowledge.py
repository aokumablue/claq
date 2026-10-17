"""claq.mem.cmd_knowledge のテスト（learn / list / search / show / promote / forget）"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claq.mem import cmd_knowledge, command_support
from claq.mem.database import Database
from claq.mem.models import Knowledge

from ._support import _repo_id, _run_cli, _seed


class TestLearn:
    """learn コマンド。"""

    def test_stores_repo_scoped_card_with_generated_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """scope 省略時は repo スコープで登録し、1 行だけ出力する。

        source 省略時は既定 agent であり、status 既定は pending
        （`/instinct promote` を経ないと SessionStart に注入されない）。
        """
        payload = {"kind": "howto", "title": "run pytest with pipefail", "body": "why"}
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stderr, exit_code) == ("", 0)
        assert stdout == "learned: run-pytest-with-pipefail\n"

        with Database(tmp_path / "mem.db") as db:
            rows = db.list_knowledge()
        assert len(rows) == 1
        assert (rows[0].scope, rows[0].status, rows[0].source) == ("repo", "pending", "agent")
        assert rows[0].repo_id is not None
        assert rows[0].body == "why"

    def test_stores_global_card_with_all_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """明示された全項目をそのまま保存する（source/status は含まない。常に固定値）。"""
        payload = {
            "key": "pipefail",
            "scope": "global",
            "kind": "pitfall",
            "title": "set -o pipefail",
            "body": "パイプ先の失敗を拾う",
            "domain": "testing",
            "confidence": 0.9,
            "source_ref": "CLAUDE.md",
        }
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("learned: pipefail\n", 0)

        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("pipefail")
        assert found is not None
        assert (found.scope, found.repo_id, found.domain) == ("global", None, "testing")
        assert (found.confidence, found.source, found.source_ref) == (0.9, "agent", "CLAUDE.md")

    @pytest.mark.parametrize("source", ["human", "observer"])
    def test_non_default_source_in_payload_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
    ) -> None:
        """回帰防止: JSON の非既定 source（human/observer 自己申告）は usage error にする。

        受け付けると、caller が JSON へ ``source: "human"`` と書くだけで人間承認を
        偽装できる。
        """
        payload = {"scope": "global", "kind": "fact", "title": "observed", "source": source}
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("", 1)
        assert "source" in stderr
        with Database(tmp_path / "mem.db") as db:
            assert db.get_knowledge_by_key("observed") is None

    def test_status_flag_on_learn_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """回帰防止: `learn --status` は指定できない（caller の自己承認経路を塞ぐ）。"""
        payload = {"scope": "global", "kind": "fact", "title": "observed"}
        stdout, stderr, exit_code = _run_cli(
            monkeypatch, tmp_path, ["learn", "--status", "active"], payload
        )
        assert (stdout, exit_code) == ("", 1)
        assert "learn --status は指定できません" in stderr
        with Database(tmp_path / "mem.db") as db:
            assert db.get_knowledge_by_key("observed") is None

    @pytest.mark.parametrize("status", ["active", "archived"])
    def test_non_default_status_in_payload_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
    ) -> None:
        """回帰防止: JSON の非既定 status（active/archived 自己申告）は usage error にする。"""
        payload = {"scope": "global", "kind": "fact", "title": "observed", "status": status}
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("", 1)
        assert "status" in stderr
        with Database(tmp_path / "mem.db") as db:
            assert db.get_knowledge_by_key("observed") is None

    def test_status_can_come_from_json(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """JSON の status も受け付ける。"""
        payload = {"scope": "global", "kind": "fact", "title": "draft", "status": "pending"}
        _stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert exit_code == 0
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("draft")
        assert found is not None and found.status == "pending"

    def test_same_key_updates_instead_of_duplicating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """同じ key の再投入は更新に落ちる。"""
        base = {"key": "dup", "scope": "global", "kind": "fact", "title": "first"}
        _run_cli(monkeypatch, tmp_path, ["learn"], base)
        _run_cli(monkeypatch, tmp_path, ["learn"], {**base, "title": "second"})

        with Database(tmp_path / "mem.db") as db:
            rows = db.list_knowledge()
        assert len(rows) == 1
        assert rows[0].title == "second"

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"kind": "fact"}, "title は必須です"),
            ({"title": "t"}, "kind は"),
            ({"kind": "fact", "title": "t", "scope": "team"}, "scope は"),
            ({"kind": "fact", "title": "t", "source": "robot"}, "source は"),
            ({"kind": "fact", "title": "t", "status": "draft"}, "status は"),
            ({"kind": "fact", "title": "t", "confidence": "high"}, "confidence は数値"),
            ({"kind": "fact", "title": "t", "confidence": 1.5}, "confidence は 0.0〜1.0"),
        ],
    )
    def test_invalid_payload_returns_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict, expected: str
    ) -> None:
        """不正な入力は stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("", 1)
        assert len(stderr.splitlines()) == 1
        assert expected in stderr


class TestPromotedCardIsImmutableToLearn:
    """回帰防止: 人間の承認が再 learn で黙って取り消されないこと。

    ``upsert_knowledge`` の ``ON CONFLICT`` は ``status = excluded.status`` で
    無条件に上書きする。learn は常に ``pending`` を書くため、ガードが無いと
    ``learn -> promote -> 同じ key で再 learn`` で **人間の承認が取り消されて
    注入対象から外れ**、本文まで差し替わる。「同じ違反の 2 回目は既存カードと
    同じ key で更新する」は本リポジトリの規定ワークフローなので、「人間が承認済み」
    かつ「再発するほど重要」という最も価値の高いカードだけが狙い撃ちで壊れる。
    """

    _PAYLOAD = {
        "key": "h3-card",
        "scope": "global",
        "kind": "pitfall",
        "title": "元のタイトル",
        "body": "人間が承認した本文",
    }

    def _learn_then_promote(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """learn -> promote を通して active カードを 1 枚作る。"""
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], self._PAYLOAD)
        assert (stdout, exit_code) == ("learned: h3-card\n", 0)

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["promote", "h3-card"])
        assert (stdout, exit_code) == ("promoted: h3-card\n", 0)

        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("h3-card")
        assert found is not None
        assert found.status == "active"

    def test_relearn_of_active_card_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """active カードへの再 learn は usage error になり、行は一切変わらない。"""
        self._learn_then_promote(monkeypatch, tmp_path)

        updated = {**self._PAYLOAD, "title": "書き換えたタイトル", "body": "agent が書いた本文"}
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], updated)

        assert (stdout, exit_code) == ("", 1)
        assert "既に active" in stderr

        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("h3-card")
        assert found is not None
        # 承認が取り消されていないこと、本文が差し替わっていないこと。
        assert found.status == "active"
        assert found.title == "元のタイトル"
        assert found.body == "人間が承認した本文"

    def test_rejection_message_steers_away_from_duplicate_keys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """拒否メッセージが「別 key で作り直すな」を明示すること。

        ここが曖昧だと、agent は別 key で作り直して重複カードを量産する
        （この経路が防ごうとしている事故そのもの）。
        """
        self._learn_then_promote(monkeypatch, tmp_path)

        _stdout, stderr, _exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], self._PAYLOAD)

        assert "別の key で作り直さないでください" in stderr

    def test_active_card_stays_injected_after_rejected_relearn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """拒否後も SessionStart 注入（status='active' の list）に載り続けること。

        この欠陥の実害は「注入対象から静かに外れる」ことなので、status 列だけでなく
        注入経路の既定絞り込みで実際に引けることまで確認する。
        """
        self._learn_then_promote(monkeypatch, tmp_path)
        _run_cli(monkeypatch, tmp_path, ["learn"], self._PAYLOAD)

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list"])

        assert exit_code == 0
        assert "元のタイトル" in stdout

    @pytest.mark.parametrize("status", ["pending", "archived"])
    def test_non_active_cards_remain_updatable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
    ) -> None:
        """active 以外は再 learn で更新できること。

        拒否対象を active に絞る根拠の固定。pending は昇格前の下書きであり、
        archived への再 learn は pending へ戻すだけで注入はされないため、
        どちらも人間の承認を奪わない。
        """
        _run_cli(monkeypatch, tmp_path, ["learn"], self._PAYLOAD)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("h3-card")
            assert found is not None
            db.set_knowledge_status(found.id, status)

        updated = {**self._PAYLOAD, "title": "更新後タイトル"}
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], updated)

        assert (stdout, exit_code) == ("learned: h3-card\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("h3-card")
        assert found is not None
        assert found.title == "更新後タイトル"
        assert found.status == "pending"

    def test_repo_scoped_active_card_is_also_protected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """repo スコープでも同じ保護が効くこと（key 照合に repo_id を含める）。"""
        payload = {**self._PAYLOAD, "scope": "repo"}
        _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        _run_cli(monkeypatch, tmp_path, ["promote", "h3-card"])

        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)

        assert exit_code == 1
        assert "既に active" in stderr

    def test_same_key_in_other_scope_is_unaffected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """global の active カードが repo スコープの同名 key を巻き添えにしないこと。

        両スコープは式インデックス ``(COALESCE(repo_id,''), key)`` で別行になる。
        照合が repo_id を無視していると、無関係なカードまで拒否してしまう。
        """
        self._learn_then_promote(monkeypatch, tmp_path)

        repo_payload = {**self._PAYLOAD, "scope": "repo", "title": "repo 側のカード"}
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], repo_payload)

        assert (stdout, exit_code) == ("learned: h3-card\n", 0)


class TestList:
    """list コマンド。"""

    def test_empty_result_prints_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """0 件なら 1 文字も出力しない。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list"])
        assert (stdout, stderr, exit_code) == ("", "", 0)

    def test_lists_repo_and_global_titles_without_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既定は repo + global を 1 件 1 行、body は出さない。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", kind="pitfall", title="global card", body="GLOBAL BODY")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, kind="howto", title="repo card")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list"])
        assert exit_code == 0
        assert sorted(stdout.splitlines()) == ["- [howto] repo card (r)", "- [pitfall] global card (g)"]
        assert "GLOBAL BODY" not in stdout

    def test_global_flag_limits_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--global は global スコープだけを出す。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", title="global card")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, title="repo card")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--global"])
        assert stdout == "- [fact] global card (g)\n"

    def test_repo_flag_limits_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--repo は cwd の repo スコープだけを出す。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", title="global card")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, title="repo card")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--repo"])
        assert stdout == "- [fact] repo card (r)\n"

    def test_both_scope_flags_conflict(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--global と --repo の同時指定はエラー。"""
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list", "--global", "--repo"])
        assert exit_code == 1
        assert "同時に指定できません" in stderr

    def test_status_filter(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--status で pending だけを出せる。"""
        _seed(tmp_path, key="a", title="active card")
        _seed(tmp_path, key="p", title="pending card", status="pending")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--status", "pending"])
        assert stdout == "- [fact] pending card (p)\n"

    def test_kind_filter(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--kind で種別を絞れる。"""
        _seed(tmp_path, key="a", kind="fact", title="a card")
        _seed(tmp_path, key="b", kind="howto", title="b card")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--kind", "howto"])
        assert stdout == "- [howto] b card (b)\n"

    def test_limit_caps_rows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--limit は件数上限として効く。"""
        for index in range(3):
            _seed(tmp_path, key=f"k{index}", title=f"card {index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--limit", "1"])
        assert len(stdout.splitlines()) == 1

    def test_default_limit_is_20(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """既定は 20 件で打ち切る。"""
        for index in range(25):
            _seed(tmp_path, key=f"k{index}", title=f"c{index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list"])
        assert len(stdout.splitlines()) == cmd_knowledge.LIST_DEFAULT_LIMIT

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["list", "--status", "gone"], "status は"),
            (["list", "--kind", "rumor"], "kind は"),
            (["list", "--limit", "many"], "--limit は整数"),
            (["list", "--limit", "0"], "--limit は 1 以上"),
        ],
    )
    def test_invalid_options_return_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: str
    ) -> None:
        """不正なオプション値は stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, argv)
        assert (stdout, exit_code) == ("", 1)
        assert expected in stderr

    def test_json_flag_emits_single_line(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--json は機械処理用に 1 行の JSON を出す（body は含めない）。"""
        _seed(tmp_path, key="g", title="global card", body="BODY")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list", "--json"])
        assert json.loads(stdout) == [
            {"key": "g", "scope": "global", "kind": "fact", "title": "global card"}
        ]
        assert len(stdout.splitlines()) == 1

    def test_output_is_truncated_at_char_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """予算超過分は捨てて ``… +N 件`` の 1 行だけ添える。"""
        for index in range(20):
            _seed(tmp_path, key=f"k{index}", title="あ" * 100)

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["list"])
        lines = stdout.splitlines()
        assert lines[-1].startswith("… +")
        assert len(stdout) <= cmd_knowledge.LIST_CHAR_BUDGET + len(lines[-1]) + 1

    def test_fit_budget_keeps_everything_within_budget(self) -> None:
        """予算内なら全行そのまま返す。"""
        assert command_support._fit_budget(["a", "b"], cmd_knowledge.LIST_CHAR_BUDGET) == ["a", "b"]

    def test_fit_budget_truncates_first_line(self) -> None:
        """1 行目から予算超過なら告知行だけを返す。"""
        lines = ["x" * (cmd_knowledge.LIST_CHAR_BUDGET + 1), "y"]
        assert command_support._fit_budget(lines, cmd_knowledge.LIST_CHAR_BUDGET) == ["… +2 件"]


class TestQueryTerms:
    """検索クエリの語分解。"""

    def test_splits_on_whitespace_and_lowercases(self) -> None:
        """空白で割り、小文字化する。2 語以上なら連結語も足す。"""
        assert cmd_knowledge._query_terms("Alpha BETA") == ("alpha", "beta", "alphabeta")

    def test_single_term_is_used_as_is(self) -> None:
        """空白の無い日本語クエリは、そのままクエリ全体の部分一致になる。"""
        assert cmd_knowledge._query_terms("型ヒント") == ("型ヒント",)

    def test_duplicate_terms_are_deduplicated(self) -> None:
        """同じ語の重複はスコアの二重加算を避けるため畳む。"""
        assert cmd_knowledge._query_terms("ab ab") == ("ab", "abab")

    def test_blank_query_raises(self) -> None:
        """空白のみのクエリは CommandError。"""
        with pytest.raises(command_support.CommandError, match="検索クエリ"):
            cmd_knowledge._query_terms("   ")


class TestScoring:
    """検索スコアの構成要素。"""

    _NOW = datetime(2026, 8, 9, tzinfo=UTC)

    def _card(self, **overrides: object) -> Knowledge:
        """採点用の Knowledge を組み立てる。"""
        fields: dict = {
            "key": "k",
            "scope": "global",
            "kind": "fact",
            "title": "title",
            "source": "agent",
            "status": "active",
            "updated_at": self._NOW.isoformat(),
        }
        fields.update(overrides)
        return Knowledge(**fields)

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"title": "alpha"}, 3.0),
            ({"key": "alpha"}, 1.5),
            ({"domain": "alpha"}, 1.0),
            ({"body": "alpha"}, 0.5),
            ({"title": "alpha", "body": "alpha"}, 3.5),
            ({}, 0.0),
        ],
    )
    def test_match_score_weights_each_field(self, overrides: dict, expected: float) -> None:
        """項目ごとの重みを合算する。domain が None でも落ちない。"""
        assert cmd_knowledge._match_score(self._card(**overrides), ("alpha",)) == expected

    def test_match_score_is_case_insensitive(self) -> None:
        """大文字小文字を無視して部分一致する。"""
        assert cmd_knowledge._match_score(self._card(title="ALPHA guide"), ("alpha",)) == 3.0

    def test_match_score_accumulates_per_term(self) -> None:
        """語ごとに加算されるので、カバーした語数が多い行ほど高くなる。"""
        card = self._card(title="alpha beta")
        assert cmd_knowledge._match_score(card, ("alpha", "beta")) == 6.0
        assert cmd_knowledge._match_score(card, ("alpha",)) == 3.0

    @pytest.mark.parametrize(
        ("updated_at", "expected"),
        [
            ("2026-08-09T00:00:00+00:00", 1.0),
            ("2026-11-07T00:00:00+00:00", 1.0),
            ("2026-05-11T00:00:00+00:00", 0.75),
        ],
    )
    def test_recency_factor(self, updated_at: str, expected: float) -> None:
        """現在・未来は 1.0、90 日前は 0.75。"""
        assert cmd_knowledge._recency_factor(updated_at, self._NOW) == pytest.approx(expected)

    def test_recency_factor_never_reaches_floor(self) -> None:
        """どれだけ古くても下限 0.5 を下回らない。"""
        factor = cmd_knowledge._recency_factor("1990-01-01T00:00:00+00:00", self._NOW)
        assert cmd_knowledge._SEARCH_MULTIPLIER_FLOOR < factor < 0.51

    def test_score_is_zero_without_any_hit(self) -> None:
        """1 語も当たらなければ 0.0。"""
        assert cmd_knowledge._score_knowledge(self._card(), ("alpha",), self._NOW) == 0.0

    def test_score_multiplies_confidence(self) -> None:
        """confidence は 0.5〜1.0 の係数として掛かる。"""
        low = cmd_knowledge._score_knowledge(self._card(title="alpha", confidence=0.0), ("alpha",), self._NOW)
        high = cmd_knowledge._score_knowledge(self._card(title="alpha", confidence=1.0), ("alpha",), self._NOW)
        assert (low, high) == (pytest.approx(1.5), pytest.approx(3.0))

    def test_matched_row_always_scores_positive(self) -> None:
        """confidence=0.0 でも一致した行は正のスコアを持つ（除外されない）。"""
        card = self._card(body="alpha", confidence=0.0, updated_at="1990-01-01T00:00:00+00:00")
        assert cmd_knowledge._score_knowledge(card, ("alpha",), self._NOW) > 0.0


class TestSearch:
    """search コマンド。"""

    def test_no_hit_prints_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ヒット 0 件なら 1 文字も出力しない。"""
        _seed(tmp_path, key="g", title="global card")

        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["search", "nomatch"])
        assert (stdout, stderr, exit_code) == ("", "", 0)

    def test_blank_query_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """クエリ無しは stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["search"])
        assert (stdout, exit_code) == ("", 1)
        assert "検索クエリ" in stderr

    def test_matches_body_but_never_prints_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body はスコア計算にだけ使い、出力には出さない。"""
        _seed(tmp_path, key="g", kind="pitfall", title="short title", body="SECRET alpha detail")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert stdout == "- [pitfall] short title (g)\n"

    def test_long_body_does_not_grow_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body の長さは出力バイト数に影響しない。"""
        _seed(tmp_path, key="g", title="short title", body="alpha " + "x" * 20000)
        long_body_output, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])

        with Database(tmp_path / "mem.db") as db:
            card = db.get_knowledge_by_key("g")
            card.body = "alpha"
            db.upsert_knowledge(card)
        short_body_output, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])

        assert long_body_output == short_body_output

    def test_title_outranks_body(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """title ヒットは body ヒットより上位。"""
        _seed(tmp_path, key="b", title="unrelated", body="alpha")
        _seed(tmp_path, key="t", title="alpha guide")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert stdout.splitlines() == ["- [fact] alpha guide (t)", "- [fact] unrelated (b)"]

    def test_excludes_rows_without_any_hit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """1 語も当たらない知識は結果に出さない。"""
        _seed(tmp_path, key="hit", title="alpha guide")
        _seed(tmp_path, key="miss", title="beta guide")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert stdout == "- [fact] alpha guide (hit)\n"

    def test_japanese_query_without_spaces(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """空白の無い日本語クエリはクエリ全体の部分一致で拾う。"""
        _seed(tmp_path, key="j", kind="convention", title="カバレッジは 100% を維持する")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "カバレッジ"])
        assert stdout == "- [convention] カバレッジは 100% を維持する (j)\n"

    def test_japanese_spaced_query_matches_unspaced_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """利用者が入れた空白は連結語のフォールバックで吸収する。"""
        _seed(tmp_path, key="j", kind="howto", title="型ヒントを付ける")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "型", "ヒント"])
        assert stdout == "- [howto] 型ヒントを付ける (j)\n"

    def test_matches_domain_and_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """domain と key も検索対象。"""
        _seed(tmp_path, key="alpha-slug", title="one")
        _seed(tmp_path, key="two", title="two", domain="alpha")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert sorted(stdout.splitlines()) == ["- [fact] one (alpha-slug)", "- [fact] two (two)"]

    def test_default_limit_is_5(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """既定は 5 件で打ち切る。"""
        for index in range(8):
            _seed(tmp_path, key=f"k{index}", title=f"alpha {index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])
        assert len(stdout.splitlines()) == cmd_knowledge.SEARCH_DEFAULT_LIMIT

    def test_limit_option_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--limit は既定件数を上書きする。"""
        for index in range(8):
            _seed(tmp_path, key=f"k{index}", title=f"alpha {index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--limit", "2"])
        assert len(stdout.splitlines()) == 2

    def test_kind_and_status_filters(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--kind と --status は list と同じ意味で効く。"""
        _seed(tmp_path, key="a", kind="fact", title="alpha fact")
        _seed(tmp_path, key="b", kind="howto", title="alpha howto")
        _seed(tmp_path, key="c", kind="fact", title="alpha pending", status="pending")

        by_kind, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--kind", "howto"])
        by_status, _stderr, _exit = _run_cli(
            monkeypatch, tmp_path, ["search", "alpha", "--status", "pending"]
        )
        assert by_kind == "- [howto] alpha howto (b)\n"
        assert by_status == "- [fact] alpha pending (c)\n"

    def test_scope_flags(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--global / --repo でスコープを片方に絞る。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", title="alpha global")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, title="alpha repo")

        only_global, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--global"])
        only_repo, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--repo"])
        assert only_global == "- [fact] alpha global (g)\n"
        assert only_repo == "- [fact] alpha repo (r)\n"

    def test_json_flag_emits_single_line(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--json は body を含まない 1 行 JSON を出す。"""
        _seed(tmp_path, key="g", title="alpha card", body="LONG BODY alpha")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha", "--json"])
        assert json.loads(stdout) == [
            {"key": "g", "scope": "global", "kind": "fact", "title": "alpha card"}
        ]

    def test_output_is_truncated_at_char_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """予算超過分は捨てて ``… +N 件`` の 1 行だけ添える。"""
        for index in range(cmd_knowledge.SEARCH_DEFAULT_LIMIT):
            _seed(tmp_path, key=f"k{index}", title="共通" + "あ" * 200)

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "共通"])
        lines = stdout.splitlines()
        assert lines[-1].startswith("… +")
        assert len(stdout) <= cmd_knowledge.SEARCH_CHAR_BUDGET + len(lines[-1]) + 1


class TestShow:
    """show コマンド。"""

    def test_shows_all_fields_including_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body を含む全項目を人間可読で出す。"""
        _seed(
            tmp_path,
            key="pipefail",
            kind="pitfall",
            title="set -o pipefail",
            body="パイプ先の失敗を拾う",
            domain="testing",
            confidence=0.9,
        )
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["show", "pipefail"])
        assert exit_code == 0
        fields = dict(line.split(": ", 1) for line in stdout.splitlines())
        assert fields["key"] == "pipefail"
        assert fields["scope"] == "global"
        assert fields["repo"] == "-"
        assert fields["kind"] == "pitfall"
        assert fields["body"] == "パイプ先の失敗を拾う"
        assert fields["domain"] == "testing"
        assert fields["confidence"] == "0.90"
        assert fields["status"] == "active"
        assert fields["source"] == "agent"

    def test_repo_scope_wins_over_global(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """同じ key が両スコープにあれば repo スコープを返す。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="dup", title="global one")
        _seed(tmp_path, key="dup", scope="repo", repo_id=repo_id, title="repo one")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["show", "dup"])
        assert "title: repo one" in stdout
        assert f"repo: {repo_id}" in stdout

    def test_empty_body_and_domain_render_as_dash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """body / domain が空なら ``-`` を出す。"""
        _seed(tmp_path, key="bare", title="bare card")
        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["show", "bare"])
        assert "body: -" in stdout
        assert "domain: -" in stdout

    def test_missing_key_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """該当が無ければ stderr 1 行と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["show", "nope"])
        assert (stdout, exit_code) == ("", 1)
        assert stderr.splitlines() == ["知識が見つかりません: nope"]

    def test_key_argument_is_required(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """key 未指定は exit_code=1。"""
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["show"])
        assert exit_code == 1
        assert "key を指定してください" in stderr


class TestPromote:
    """promote コマンド。"""

    def test_pending_becomes_active(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """pending を active に上げて 1 行だけ出力する。"""
        _seed(tmp_path, key="draft", title="draft card", status="pending")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["promote", "draft"])
        assert (stdout, exit_code) == ("promoted: draft\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("draft")
        assert found is not None and found.status == "active"


class TestForget:
    """forget コマンド。"""

    def test_archives_card(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """status を archived にして 1 行だけ出力する。"""
        _seed(tmp_path, key="old", title="old card")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["forget", "old"])
        assert (stdout, exit_code) == ("forgot: old\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None
        assert (found.status, found.superseded_by) == ("archived", None)

    def test_records_successor(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """--superseded-by で置き換え先の id を残す。"""
        _seed(tmp_path, key="old", title="old card")
        successor = _seed(tmp_path, key="new", title="new card")

        stdout, _stderr, exit_code = _run_cli(
            monkeypatch, tmp_path, ["forget", "old", "--superseded-by", "new"]
        )
        assert (stdout, exit_code) == ("forgot: old (superseded by new)\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None
        assert (found.status, found.superseded_by) == ("archived", successor.id)

    def test_unknown_successor_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """置き換え先が無ければエラーにして元の行も変えない。"""
        _seed(tmp_path, key="old", title="old card")

        _stdout, stderr, exit_code = _run_cli(
            monkeypatch, tmp_path, ["forget", "old", "--superseded-by", "ghost"]
        )
        assert exit_code == 1
        assert "知識が見つかりません: ghost" in stderr
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None and found.status == "active"
