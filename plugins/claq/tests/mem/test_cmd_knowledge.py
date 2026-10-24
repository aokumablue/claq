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

# learn の自己承認拒否テストが共有する、それ自体は正当な payload。
_OBSERVED = {"scope": "global", "kind": "fact", "title": "observed"}


class TestLearn:
    """learn コマンド。"""

    def test_stores_repo_scoped_card_with_generated_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """scope 省略時は repo スコープで登録し、1 行だけ出力する。

        source 省略時は既定 agent であり、status 既定は pending
        （`/instinct promote` を経ないと SessionStart に注入されない）。
        """
        payload = {"kind": "howto", "title": "run pytest with pipefail", "body": "why"}
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, stderr, exit_code) == ("learned: run-pytest-with-pipefail\n", "", 0)

        with Database(tmp_path / "mem.db") as db:
            rows = db.list_knowledge()
        assert len(rows) == 1
        assert (rows[0].scope, rows[0].status, rows[0].source, rows[0].body) == ("repo", "pending", "agent", "why")
        assert rows[0].repo_id is not None

    def test_stores_global_card_with_all_fields(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """明示された全項目をそのまま保存する。既定値と同じ status=pending の明示も受け付ける。"""
        payload = {
            "key": "pipefail",
            "scope": "global",
            "kind": "pitfall",
            "title": "set -o pipefail",
            "body": "パイプ先の失敗を拾う",
            "domain": "testing",
            "confidence": 0.9,
            "source_ref": "CLAUDE.md",
            "status": "pending",
        }
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("learned: pipefail\n", 0)

        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("pipefail")
        assert found is not None
        assert (found.scope, found.repo_id, found.domain, found.status) == ("global", None, "testing", "pending")
        assert (found.confidence, found.source, found.source_ref) == (0.9, "agent", "CLAUDE.md")

    @pytest.mark.parametrize(
        ("options", "payload", "expected"),
        [
            ([], {**_OBSERVED, "source": "human"}, "source は"),
            ([], {**_OBSERVED, "source": "observer"}, "source は"),
            ([], {**_OBSERVED, "status": "active"}, "status は"),
            ([], {**_OBSERVED, "status": "archived"}, "status は"),
            (["--status", "active"], _OBSERVED, "learn --status は指定できません"),
            ([], {"kind": "fact"}, "title は必須です"),
            ([], {"title": "t"}, "kind は"),
            ([], {"kind": "fact", "title": "t", "scope": "team"}, "scope は"),
            ([], {"kind": "fact", "title": "t", "source": "robot"}, "source は"),
            ([], {"kind": "fact", "title": "t", "status": "draft"}, "status は"),
            ([], {"kind": "fact", "title": "t", "confidence": "high"}, "confidence は数値"),
            ([], {"kind": "fact", "title": "t", "confidence": 1.5}, "confidence は 0.0〜1.0"),
        ],
        ids=[
            "source-human",
            "source-observer",
            "status-active",
            "status-archived",
            "status-flag",
            "title-missing",
            "kind-missing",
            "scope-unknown",
            "source-unknown",
            "status-unknown",
            "confidence-non-numeric",
            "confidence-out-of-range",
        ],
    )
    def test_invalid_input_is_rejected_without_storing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: list[str], payload: dict, expected: str
    ) -> None:
        """不正な入力は stderr 1 行と exit_code=1 で拒否し、行を作らない。

        回帰防止: JSON の非既定 source（human/observer）・status（active/archived）や
        ``learn --status`` を受け付けると、caller が書くだけで人間承認を偽装・自己承認できる。
        """
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn", *options], payload)
        assert (stdout, exit_code) == ("", 1)
        assert len(stderr.splitlines()) == 1
        assert expected in stderr
        with Database(tmp_path / "mem.db") as db:
            assert db.list_knowledge() == []

    def test_same_key_updates_instead_of_duplicating(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """同じ key の再投入は更新に落ちる。"""
        base = {"key": "dup", "scope": "global", "kind": "fact", "title": "first"}
        _run_cli(monkeypatch, tmp_path, ["learn"], base)
        _run_cli(monkeypatch, tmp_path, ["learn"], {**base, "title": "second"})

        with Database(tmp_path / "mem.db") as db:
            rows = db.list_knowledge()
        assert [row.title for row in rows] == ["second"]


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

    @staticmethod
    def _learn_then_promote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict) -> None:
        """learn -> promote を通して active カードを 1 枚作る。"""
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], payload)
        assert (stdout, exit_code) == ("learned: h3-card\n", 0)
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["promote", "h3-card"])
        assert (stdout, exit_code) == ("promoted: h3-card\n", 0)

    @pytest.mark.parametrize(
        ("initial", "relearn"),
        [
            pytest.param({}, {}, id="global-same-payload"),
            pytest.param({}, {"title": "書き換えたタイトル", "body": "agent が書いた本文"}, id="global-rewritten"),
            pytest.param({"scope": "repo"}, {}, id="repo-same-payload"),
        ],
    )
    def test_relearn_of_active_card_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial: dict, relearn: dict
    ) -> None:
        """active カードへの再 learn は usage error になり、行は変わらず注入され続ける。

        repo スコープでも同じ保護が効く（key 照合に repo_id を含める）。拒否メッセージは
        「別 key で作り直すな」を明示する。曖昧だと agent は別 key で作り直して重複カードを
        量産する（この経路が防ごうとしている事故そのもの）。実害は「注入対象から静かに外れる」
        ことなので、status 列だけでなく注入経路の既定絞り込み（list）で引けることまで確かめる。
        """
        payload = {**self._PAYLOAD, **initial}
        self._learn_then_promote(monkeypatch, tmp_path, payload)

        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], {**payload, **relearn})

        assert (stdout, exit_code) == ("", 1)
        assert "既に active" in stderr
        assert "別の key で作り直さないでください" in stderr
        repo_id = _repo_id(tmp_path) if payload["scope"] == "repo" else None
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("h3-card", repo_id)
        assert found is not None
        assert (found.status, found.title, found.body) == ("active", "元のタイトル", "人間が承認した本文")
        listed, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list"])
        assert exit_code == 0
        assert "元のタイトル" in listed

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
        assert (found.title, found.status) == ("更新後タイトル", "pending")

    def test_same_key_in_other_scope_is_unaffected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """global の active カードが repo スコープの同名 key を巻き添えにしないこと。

        両スコープは式インデックス ``(COALESCE(repo_id,''), key)`` で別行になる。
        照合が repo_id を無視していると、無関係なカードまで拒否してしまう。
        """
        self._learn_then_promote(monkeypatch, tmp_path, self._PAYLOAD)

        repo_payload = {**self._PAYLOAD, "scope": "repo", "title": "repo 側のカード"}
        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["learn"], repo_payload)

        assert (stdout, exit_code) == ("learned: h3-card\n", 0)


_LIST = ["list"]
_SEARCH = ["search", "alpha"]


class TestListAndSearch:
    """list と search に共通する絞り込み・件数・出力形式。"""

    @pytest.mark.parametrize("command", [_LIST, _SEARCH], ids=["list", "search"])
    @pytest.mark.parametrize(
        ("options", "expected"),
        [
            (["--global"], "- [fact] alpha global (g)\n"),
            (["--repo"], "- [howto] alpha repo (r)\n"),
            (["--status", "pending"], "- [fact] alpha pending (p)\n"),
            (["--kind", "howto"], "- [howto] alpha repo (r)\n"),
        ],
        ids=["global", "repo", "status", "kind"],
    )
    def test_filters_narrow_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: list[str], options: list[str], expected: str
    ) -> None:
        """--global / --repo はスコープを片方に、--status / --kind は status / 種別を絞る。"""
        _seed(tmp_path, key="g", title="alpha global")
        _seed(tmp_path, key="r", scope="repo", repo_id=_repo_id(tmp_path), kind="howto", title="alpha repo")
        _seed(tmp_path, key="p", title="alpha pending", status="pending")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, [*command, *options])
        assert stdout == expected

    @pytest.mark.parametrize(
        ("argv", "seeded", "expected"),
        [
            ([*_LIST, "--limit", "1"], 3, 1),
            (_LIST, 25, cmd_knowledge.LIST_DEFAULT_LIMIT),
            (_SEARCH, 8, cmd_knowledge.SEARCH_DEFAULT_LIMIT),
            ([*_SEARCH, "--limit", "2"], 8, 2),
        ],
        ids=["list-limit", "list-default-20", "search-default-5", "search-limit"],
    )
    def test_limit_caps_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], seeded: int, expected: int
    ) -> None:
        """--limit は件数上限として効き、未指定なら list は 20 件・search は 5 件で打ち切る。"""
        for index in range(seeded):
            _seed(tmp_path, key=f"k{index}", title=f"alpha {index}")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, argv)
        assert len(stdout.splitlines()) == expected

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["list", "--status", "gone"], "status は"),
            (["list", "--kind", "rumor"], "kind は"),
            (["list", "--limit", "many"], "--limit は整数"),
            (["list", "--limit", "0"], "--limit は 1 以上"),
            (["search"], "検索クエリ"),
        ],
    )
    def test_invalid_options_return_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], expected: str
    ) -> None:
        """不正なオプション値・空の検索クエリは stderr と exit_code=1。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, argv)
        assert (stdout, exit_code) == ("", 1)
        assert expected in stderr

    @pytest.mark.parametrize("command", [_LIST, _SEARCH], ids=["list", "search"])
    def test_json_flag_emits_single_line_without_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: list[str]
    ) -> None:
        """--json は機械処理用に body を含まない 1 行の JSON を出す。"""
        _seed(tmp_path, key="g", title="alpha card", body="LONG BODY alpha")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, [*command, "--json"])
        assert json.loads(stdout) == [{"key": "g", "scope": "global", "kind": "fact", "title": "alpha card"}]
        assert len(stdout.splitlines()) == 1

    @pytest.mark.parametrize(
        ("argv", "seeded", "title", "budget"),
        [
            (_LIST, 20, "あ" * 100, cmd_knowledge.LIST_CHAR_BUDGET),
            (["search", "共通"], cmd_knowledge.SEARCH_DEFAULT_LIMIT, "共通" + "あ" * 200, cmd_knowledge.SEARCH_CHAR_BUDGET),
        ],
        ids=["list", "search"],
    )
    def test_output_is_truncated_at_char_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], seeded: int, title: str, budget: int
    ) -> None:
        """予算超過分は捨てて ``… +N 件`` の 1 行だけ添える。"""
        for index in range(seeded):
            _seed(tmp_path, key=f"k{index}", title=title)

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, argv)
        lines = stdout.splitlines()
        assert lines[-1].startswith("… +")
        assert len(stdout) <= budget + len(lines[-1]) + 1


class TestList:
    """list コマンド。"""

    def test_empty_result_prints_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """0 件なら 1 文字も出力しない。"""
        assert _run_cli(monkeypatch, tmp_path, ["list"]) == ("", "", 0)

    def test_lists_repo_and_global_titles_without_body(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """既定は repo + global を 1 件 1 行、body は出さない。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="g", kind="pitfall", title="global card", body="GLOBAL BODY")
        _seed(tmp_path, key="r", scope="repo", repo_id=repo_id, kind="howto", title="repo card")

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["list"])
        assert exit_code == 0
        assert sorted(stdout.splitlines()) == ["- [howto] repo card (r)", "- [pitfall] global card (g)"]
        assert "GLOBAL BODY" not in stdout

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            (["a", "b"], ["a", "b"]),
            (["x" * (cmd_knowledge.LIST_CHAR_BUDGET + 1), "y"], ["… +2 件"]),
        ],
        ids=["within-budget", "first-line-over-budget"],
    )
    def test_fit_budget(self, lines: list[str], expected: list[str]) -> None:
        """予算内なら全行そのまま、1 行目から予算超過なら告知行だけを返す。"""
        assert command_support._fit_budget(lines, cmd_knowledge.LIST_CHAR_BUDGET) == expected


class TestQueryTerms:
    """検索クエリの語分解。"""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Alpha BETA", ("alpha", "beta", "alphabeta")),
            ("型ヒント", ("型ヒント",)),
            ("ab ab", ("ab", "abab")),
        ],
        ids=["split-lowercase-and-joined", "single-term-as-is", "duplicates-folded"],
    )
    def test_splits_lowercases_and_joins(self, query: str, expected: tuple[str, ...]) -> None:
        """空白で割って小文字化し、2 語以上なら連結語も足す。重複はスコアの二重加算を避けるため畳む。"""
        assert cmd_knowledge._query_terms(query) == expected

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
        ("overrides", "terms", "expected"),
        [
            ({"title": "alpha"}, ("alpha",), 3.0),
            ({"key": "alpha"}, ("alpha",), 1.5),
            ({"domain": "alpha"}, ("alpha",), 1.0),
            ({"body": "alpha"}, ("alpha",), 0.5),
            ({"title": "alpha", "body": "alpha"}, ("alpha",), 3.5),
            ({}, ("alpha",), 0.0),
            ({"title": "ALPHA guide"}, ("alpha",), 3.0),
            ({"title": "alpha beta"}, ("alpha", "beta"), 6.0),
            ({"title": "alpha beta"}, ("alpha",), 3.0),
        ],
    )
    def test_match_score_weights_each_field(self, overrides: dict, terms: tuple[str, ...], expected: float) -> None:
        """項目ごとの重みを語ごとに合算し、大文字小文字を無視する。domain が None でも落ちない。"""
        assert cmd_knowledge._match_score(self._card(**overrides), terms) == expected

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
        assert _run_cli(monkeypatch, tmp_path, ["search", "nomatch"]) == ("", "", 0)

    @pytest.mark.parametrize(
        ("seeds", "query", "expected"),
        [
            pytest.param(
                [{"key": "g", "kind": "pitfall", "title": "short title", "body": "SECRET alpha detail"}],
                ["alpha"],
                "- [pitfall] short title (g)\n",
                id="body-hit-never-printed",
            ),
            pytest.param(
                [
                    {"key": "b1", "title": "unrelated one", "body": "alpha"},
                    {"key": "t", "title": "alpha guide"},
                    {"key": "b2", "title": "unrelated two", "body": "alpha"},
                ],
                ["alpha"],
                "- [fact] alpha guide (t)\n- [fact] unrelated two (b2)\n- [fact] unrelated one (b1)\n",
                id="title-outranks-body",
            ),
            pytest.param(
                [{"key": "hit", "title": "alpha guide"}, {"key": "miss", "title": "beta guide"}],
                ["alpha"],
                "- [fact] alpha guide (hit)\n",
                id="no-hit-row-excluded",
            ),
            pytest.param(
                [{"key": "j", "kind": "convention", "title": "カバレッジは 100% を維持する"}],
                ["カバレッジ"],
                "- [convention] カバレッジは 100% を維持する (j)\n",
                id="japanese-unspaced-query",
            ),
            pytest.param(
                [{"key": "j", "kind": "howto", "title": "型ヒントを付ける"}],
                ["型", "ヒント"],
                "- [howto] 型ヒントを付ける (j)\n",
                id="japanese-spaced-query-joined",
            ),
            pytest.param(
                [{"key": "alpha-slug", "title": "one"}, {"key": "two", "title": "two", "domain": "alpha"}],
                ["alpha"],
                "- [fact] one (alpha-slug)\n- [fact] two (two)\n",
                id="key-and-domain-hit",
            ),
        ],
    )
    def test_ranks_hits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seeds: list[dict], query: list[str], expected: str
    ) -> None:
        """title/key/domain/body を重み付きで採点し、当たらない行を落とし、body は出力しない。

        空白の無い日本語クエリはクエリ全体の部分一致で、利用者が入れた空白は連結語で吸収する。
        """
        for fields in seeds:
            _seed(tmp_path, **fields)

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", *query])
        assert stdout == expected

    def test_long_body_does_not_grow_output(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """body の長さは出力バイト数に影響しない。"""
        _seed(tmp_path, key="g", title="short title", body="alpha " + "x" * 20000)
        long_body_output, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])

        with Database(tmp_path / "mem.db") as db:
            card = db.get_knowledge_by_key("g")
            card.body = "alpha"
            db.upsert_knowledge(card)
        short_body_output, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["search", "alpha"])

        assert long_body_output == short_body_output


class TestShow:
    """show コマンド。"""

    def test_shows_all_fields_including_body(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
        assert fields.pop("updated_at")
        assert fields == {
            "key": "pipefail",
            "scope": "global",
            "repo": "-",
            "kind": "pitfall",
            "title": "set -o pipefail",
            "body": "パイプ先の失敗を拾う",
            "domain": "testing",
            "confidence": "0.90",
            "status": "active",
            "source": "agent",
        }

    def test_repo_scope_wins_over_global(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """同じ key が両スコープにあれば repo スコープを返す。"""
        repo_id = _repo_id(tmp_path)
        _seed(tmp_path, key="dup", title="global one")
        _seed(tmp_path, key="dup", scope="repo", repo_id=repo_id, title="repo one")

        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["show", "dup"])
        assert "title: repo one" in stdout
        assert f"repo: {repo_id}" in stdout

    def test_empty_body_and_domain_render_as_dash(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """body / domain が空なら ``-`` を出す。"""
        _seed(tmp_path, key="bare", title="bare card")
        stdout, _stderr, _exit = _run_cli(monkeypatch, tmp_path, ["show", "bare"])
        assert "body: -" in stdout
        assert "domain: -" in stdout

    def test_missing_key_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """該当が無ければ stderr 1 行と exit_code=1。"""
        assert _run_cli(monkeypatch, tmp_path, ["show", "nope"]) == ("", "知識が見つかりません: nope\n", 1)


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

        stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["forget", "old", "--superseded-by", "new"])
        assert (stdout, exit_code) == ("forgot: old (superseded by new)\n", 0)
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None
        assert (found.status, found.superseded_by) == ("archived", successor.id)

    def test_unknown_successor_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """置き換え先が無ければエラーにして元の行も変えない。"""
        _seed(tmp_path, key="old", title="old card")

        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["forget", "old", "--superseded-by", "ghost"])
        assert exit_code == 1
        assert "知識が見つかりません: ghost" in stderr
        with Database(tmp_path / "mem.db") as db:
            found = db.get_knowledge_by_key("old")
        assert found is not None and found.status == "active"
