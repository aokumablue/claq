"""bluecore.mem.knowledge_input のテスト"""

from __future__ import annotations

import pytest

from bluecore.mem.knowledge_input import (
    DEFAULT_CONFIDENCE,
    KINDS,
    KnowledgeDraft,
    KnowledgeInputError,
    coerce_confidence,
    generate_key,
    optional_str,
    parse_knowledge_payload,
    validate_choice,
)


class TestGenerateKey:
    """key の自動生成規則。"""

    def test_ascii_tokens_survive_japanese_title(self) -> None:
        """日本語まじりでも ASCII トークンだけの読める key になる。"""
        assert generate_key("pytest をパイプする際は set -o pipefail が必須", "pitfall") == "pytest-set-o-pipefail"

    def test_pure_japanese_title_falls_back_to_hash(self) -> None:
        """ASCII 英数字を含まない title は kind + ハッシュの決定的 key になる。"""
        key = generate_key("テーブル定義変更は移行不要", "decision")
        assert key.startswith("decision-")
        assert len(key) == len("decision-") + 8
        assert key == generate_key("テーブル定義変更は移行不要", "decision")


class TestScalarHelpers:
    """列挙値・確信度・任意文字列の変換。"""

    def test_validate_choice_passes_through_allowed_value(self) -> None:
        """許可された値はそのまま返る。"""
        assert validate_choice("kind", "fact", KINDS) == "fact"

    def test_validate_choice_lists_allowed_values_in_message(self) -> None:
        """エラーメッセージに許可値を並べる。"""
        with pytest.raises(KnowledgeInputError, match="kind は .*fact"):
            validate_choice("kind", "rumor", KINDS)

    def test_confidence_defaults_when_missing(self) -> None:
        """None は既定値へ倒す。"""
        assert coerce_confidence(None) == DEFAULT_CONFIDENCE

    def test_confidence_accepts_numeric_string(self) -> None:
        """数値文字列も float へ変換する。"""
        assert coerce_confidence("0.25") == 0.25

    @pytest.mark.parametrize("value", ["high", ["0.5"]])
    def test_confidence_rejects_non_numeric(self, value: object) -> None:
        """数値化できない値は拒否する。"""
        with pytest.raises(KnowledgeInputError, match="confidence は数値"):
            coerce_confidence(value)

    @pytest.mark.parametrize("value", [-0.1, 1.5])
    def test_confidence_rejects_out_of_range(self, value: float) -> None:
        """0.0〜1.0 の外は拒否する。"""
        with pytest.raises(KnowledgeInputError, match="confidence は 0.0〜1.0"):
            coerce_confidence(value)

    @pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ([], None), ("git", "git")])
    def test_optional_str(self, value: object, expected: str | None) -> None:
        """空でない値だけ文字列化する。"""
        assert optional_str(value) == expected


class TestParseKnowledgePayload:
    """ペイロード全体の検証。"""

    def test_minimal_payload_fills_defaults(self) -> None:
        """必須項目だけの入力は既定値で埋まる。"""
        draft = parse_knowledge_payload({"kind": "howto", "title": "run pytest with pipefail"})
        assert draft == KnowledgeDraft(
            key="run-pytest-with-pipefail",
            scope="repo",
            kind="howto",
            title="run pytest with pipefail",
            body="",
            domain=None,
            confidence=DEFAULT_CONFIDENCE,
            status="active",
            source="agent",
            source_ref=None,
        )

    def test_status_override_wins_over_payload(self) -> None:
        """status の上書き（CLI --status）はペイロードより強い。"""
        draft = parse_knowledge_payload(
            {"kind": "fact", "title": "t", "status": "active", "source": "human"},
            status_override="pending",
        )
        assert (draft.status, draft.source) == ("pending", "human")

    def test_explicit_key_and_fields_are_kept(self) -> None:
        """明示された key と任意項目はそのまま残る。"""
        draft = parse_knowledge_payload(
            {
                "key": "pipefail",
                "scope": "global",
                "kind": "pitfall",
                "title": "set -o pipefail",
                "body": "パイプ先の失敗を拾う",
                "domain": "testing",
                "confidence": 0.9,
                "source_ref": "CLAUDE.md",
            }
        )
        assert (draft.key, draft.scope, draft.domain, draft.source_ref) == (
            "pipefail",
            "global",
            "testing",
            "CLAUDE.md",
        )

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"kind": "fact"}, "title は必須です"),
            ({"kind": "fact", "title": "   "}, "title は必須です"),
            ({"title": "t"}, "kind は"),
            ({"kind": "fact", "title": "t", "scope": "team"}, "scope は"),
            ({"kind": "fact", "title": "t", "source": "robot"}, "source は"),
            ({"kind": "fact", "title": "t", "status": "draft"}, "status は"),
        ],
    )
    def test_rejects_invalid_payload(self, payload: dict, expected: str) -> None:
        """CHECK 制約を満たさない入力は KnowledgeInputError。"""
        with pytest.raises(KnowledgeInputError, match=expected):
            parse_knowledge_payload(payload)


class TestToKnowledge:
    """KnowledgeDraft から Knowledge への変換。"""

    @staticmethod
    def _draft(scope: str = "repo") -> KnowledgeDraft:
        """テスト用の下書きを作る。"""
        return parse_knowledge_payload({"kind": "fact", "title": "t", "scope": scope})

    def test_repo_scope_binds_repo_id(self) -> None:
        """repo スコープは repo_id を持つ Knowledge になる。"""
        knowledge = self._draft().to_knowledge("bluecore-dev")
        assert (knowledge.scope, knowledge.repo_id, knowledge.key) == ("repo", "bluecore-dev", "t")
        assert knowledge.created_at == knowledge.updated_at

    def test_global_scope_leaves_repo_id_none(self) -> None:
        """global スコープは repo_id が None。"""
        assert self._draft("global").to_knowledge(None).repo_id is None

    @pytest.mark.parametrize(("scope", "repo_id"), [("repo", None), ("global", "bluecore-dev")])
    def test_rejects_scope_repo_id_mismatch(self, scope: str, repo_id: str | None) -> None:
        """scope と repo_id が矛盾する組み合わせは拒否する。"""
        with pytest.raises(KnowledgeInputError, match="矛盾"):
            self._draft(scope).to_knowledge(repo_id)
