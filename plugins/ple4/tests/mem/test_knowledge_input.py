"""ple4.mem.knowledge_input のテスト"""

from __future__ import annotations

import pytest

from ple4.mem.knowledge_input import (
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

    def test_generate_key_falls_back_to_hash_when_slug_too_short(self) -> None:
        """ASCII が 1〜2 文字しかない title は slug ではなくハッシュ由来 key になる。

        ``n`` や ``md`` のような極端に短い key は、次に ASCII を 1〜2 文字だけ
        含む別の title と衝突し、upsert で既存カードを上書きしてしまう。
        """
        short_ascii = generate_key("指示文書の列挙を『上記 N 種』と個数で参照しない", "convention")
        assert short_ascii.startswith("convention-")
        assert len(short_ascii) == len("convention-") + 8

        two_chars = generate_key("md の構造テストで見出しを収集する", "pitfall")
        assert two_chars.startswith("pitfall-")

        # 衝突しないこと（従来はどちらも "n" に潰れていた）
        assert short_ascii != generate_key("結論は N 件に絞る", "convention")

        # 3 文字以上の slug は従来どおり slug のまま
        assert generate_key("use ci for checks", "convention") == "use-ci-for-checks"

    def test_pure_japanese_title_falls_back_to_hash(self) -> None:
        """ASCII 英数字を含まない title は kind + ハッシュの決定的 key になる。"""
        key = generate_key("テーブル定義変更は移行不要", "decision")
        assert key.startswith("decision-")
        assert len(key) == len("decision-") + 8
        assert key == generate_key("テーブル定義変更は移行不要", "decision")

    def test_bracketed_redacted_title_yields_stable_slug(self) -> None:
        """redact 後の title に残る角括弧混じりの `[REDACTED]` も安定した
        スラッグ（redacted を含む形）に落ちる（A-03 の確認事項）。"""
        key = generate_key("Keep [REDACTED] out of titles", "pitfall")
        assert "redacted" in key
        assert key == generate_key("Keep [REDACTED] out of titles", "pitfall")


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
        """必須項目だけの入力は既定値で埋まる（source 既定 agent → status 既定 pending、A-03）。"""
        draft = parse_knowledge_payload({"kind": "howto", "title": "run pytest with pipefail"})
        assert draft == KnowledgeDraft(
            key="run-pytest-with-pipefail",
            scope="repo",
            kind="howto",
            title="run pytest with pipefail",
            body="",
            domain=None,
            confidence=DEFAULT_CONFIDENCE,
            status="pending",
            source="agent",
            source_ref=None,
        )

    def test_explicit_default_source_and_status_are_kept(self) -> None:
        """既定値と同じ値の明示（source=agent, status=pending）は無害なので許可する。"""
        draft = parse_knowledge_payload(
            {"kind": "fact", "title": "t", "source": "agent", "status": "pending"}
        )
        assert (draft.source, draft.status) == ("agent", "pending")

    @pytest.mark.parametrize("source", ["human", "observer"])
    def test_non_default_source_is_rejected(self, source: str) -> None:
        """H-01: generic learn は source=agent 固定。human/observer の自己申告は拒否する。

        caller（agent・外部入力を処理した agent 含む）が JSON に
        ``source: "human"`` と書くだけで人間承認を偽装できていた
        （ADR-0007）。
        """
        with pytest.raises(KnowledgeInputError, match="source"):
            parse_knowledge_payload({"kind": "fact", "title": "t", "source": source})

    @pytest.mark.parametrize("status", ["active", "archived"])
    def test_non_default_status_is_rejected(self, status: str) -> None:
        """H-01: generic learn は status=pending 固定。active/archived の自己申告は拒否する。

        caller が JSON に ``status: "active"`` と書くだけで、人間承認
        （``promote <key>``）を経ずに永続 SessionStart context への注入を
        自己承認できていた（ADR-0007）。
        """
        with pytest.raises(KnowledgeInputError, match="status"):
            parse_knowledge_payload({"kind": "fact", "title": "t", "status": status})

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


class TestSecretRedaction:
    """parse_knowledge_payload の title/body/source_ref redaction（R-04）。

    注意: シークレット様のリテラルは文字列連結で組み立てる。
    commit_quality_scanner の自己スキャン（test_repo_wide_self_scan_
    has_zero_secret_issues）が本テストファイル自体を secret として
    検出しないようにするため（test_redaction.py の既存パターンと同じ回避）。
    """

    _GITHUB_PAT = "ghp_" + "1234567890abcdef1234567890abcdef1234"
    _ANTHROPIC_KEY = "sk-ant-" + "abcdefghijklmnopqrstuvwxyz0123456789"

    def test_secret_in_body_is_redacted(self) -> None:
        """body に含まれる既知プレフィックス系シークレットはマスクされる。"""
        draft = parse_knowledge_payload(
            {
                "kind": "pitfall",
                "title": "secret probe",
                "body": f"credential {self._GITHUB_PAT}",
            }
        )
        assert "ghp_" not in draft.body
        assert "[REDACTED]" in draft.body

    def test_secret_in_source_ref_is_redacted(self) -> None:
        """source_ref も title/body と同様に検査対象。"""
        draft = parse_knowledge_payload(
            {
                "kind": "fact",
                "title": "token leak",
                "source_ref": f"token={self._ANTHROPIC_KEY}",
            }
        )
        assert "sk-ant-" not in (draft.source_ref or "")
        assert "[REDACTED]" in (draft.source_ref or "")

    def test_key_is_stable_regardless_of_secret_in_body(self) -> None:
        """body に secret が有る/無いで key が変わらない（key は生 title から生成）。"""
        without_secret = parse_knowledge_payload({"kind": "fact", "title": "same title", "body": "plain"})
        with_secret = parse_knowledge_payload(
            {
                "kind": "fact",
                "title": "same title",
                "body": f"credential {self._GITHUB_PAT}",
            }
        )
        assert without_secret.key == with_secret.key

    def test_key_generation_uses_redacted_title_when_title_itself_is_a_secret(self) -> None:
        """title 自体がシークレット様の文字列でも、key は redact 後の title から
        生成され、生のシークレット断片を含まない（A-03 対応）。"""
        payload = {"kind": "fact", "title": "token=hunter2hunter2hunter2secret!!"}
        draft = parse_knowledge_payload(dict(payload))
        assert "hunter2" not in draft.key
        assert draft.key == generate_key(payload["title"], "fact")

    def test_same_secret_title_learned_twice_yields_same_key(self) -> None:
        """同じ secret 入り title を 2 度 learn しても同一 key に落ちる
        （upsert され重複カードを作らない。A-03 の確認事項）。"""
        payload = {"kind": "pitfall", "title": f"Keep {self._GITHUB_PAT} out of titles"}
        first = parse_knowledge_payload(dict(payload))
        second = parse_knowledge_payload(dict(payload))
        assert first.key == second.key
        assert "ghp_" not in first.key

    def test_different_secrets_with_same_surrounding_text_get_different_keys(self) -> None:
        """redact 後に同一テキストへ潰れる 2 枚（異なる secret）は、生 title 由来の
        衝突回避サフィックスで別々の key になる（A-03: key 衝突による情報混同の防止）。"""
        other_pat = "ghp_" + "9876543210fedcba9876543210fedcba9876"
        draft_a = parse_knowledge_payload({"kind": "pitfall", "title": f"Keep {self._GITHUB_PAT} safe"})
        draft_b = parse_knowledge_payload({"kind": "pitfall", "title": f"Keep {other_pat} safe"})
        assert draft_a.key != draft_b.key
        assert "ghp_" not in draft_a.key
        assert "ghp_" not in draft_b.key

    def test_explicit_key_with_secret_is_redacted(self) -> None:
        """payload の明示 ``key`` もシークレット断片を素通りさせない（A-03 対応）。"""
        draft = parse_knowledge_payload({"kind": "fact", "title": "t", "key": self._GITHUB_PAT})
        assert "ghp_" not in draft.key

    def test_domain_is_redacted(self) -> None:
        """domain もシークレットを持ち込みうるため redact する（A-03 対応）。"""
        draft = parse_knowledge_payload(
            {"kind": "fact", "title": "t", "domain": f"leak {self._ANTHROPIC_KEY}"}
        )
        assert "sk-ant-" not in (draft.domain or "")
        assert "[REDACTED]" in (draft.domain or "")

    def test_commit_sha_in_body_is_not_redacted(self) -> None:
        """40 文字の commit SHA のような長い16進/base64様文字列は汎用エントロピー

        パターン（hex_secret/base64_long）の対象外のため保持される
        （handoff.py がパスへの redact 適用を避けているのと同じ理由）。
        """
        sha = "a" * 40  # 40 桁の16進文字列（commit SHA 相当）
        draft = parse_knowledge_payload({"kind": "fact", "title": "commit ref", "body": f"see commit {sha}"})
        assert sha in draft.body
        assert "[REDACTED]" not in draft.body

    def test_email_in_body_is_still_redacted(self) -> None:
        """縮小版でも既知パターン（email 等）は従来どおり適用される。"""
        draft = parse_knowledge_payload({"kind": "fact", "title": "contact", "body": "reach me at a@example.com"})
        assert "a@example.com" not in draft.body
        assert "[REDACTED]" in draft.body


class TestToKnowledge:
    """KnowledgeDraft から Knowledge への変換。"""

    @staticmethod
    def _draft(scope: str = "repo") -> KnowledgeDraft:
        """テスト用の下書きを作る。"""
        return parse_knowledge_payload({"kind": "fact", "title": "t", "scope": scope})

    def test_repo_scope_binds_repo_id(self) -> None:
        """repo スコープは repo_id を持つ Knowledge になる。"""
        draft = self._draft()
        knowledge = draft.to_knowledge("ple4-dev")
        assert (knowledge.scope, knowledge.repo_id, knowledge.key) == ("repo", "ple4-dev", draft.key)
        assert knowledge.created_at == knowledge.updated_at

    def test_global_scope_leaves_repo_id_none(self) -> None:
        """global スコープは repo_id が None。"""
        assert self._draft("global").to_knowledge(None).repo_id is None

    @pytest.mark.parametrize(("scope", "repo_id"), [("repo", None), ("global", "ple4-dev")])
    def test_rejects_scope_repo_id_mismatch(self, scope: str, repo_id: str | None) -> None:
        """scope と repo_id が矛盾する組み合わせは拒否する。"""
        with pytest.raises(KnowledgeInputError, match="矛盾"):
            self._draft(scope).to_knowledge(repo_id)


class TestExplicitKeyHashFallback:
    """F-24: ASCII を含まない明示 key が同じ slug へ潰れて上書きし合う退行を防ぐ。"""

    @staticmethod
    def _key_for(explicit_key: str) -> str:
        """明示 key を渡した learn payload から確定 key を取り出す。"""
        draft = parse_knowledge_payload(
            {"title": "タイトル", "kind": "fact", "key": explicit_key, "body": "本文"}
        )
        return draft.key

    def test_non_ascii_explicit_keys_do_not_collide(self) -> None:
        """異なる非 ASCII 明示 key が別々の key に落ちること。

        以前は slugify の FALLBACK_SLUG により全て `repo` へ潰れ、upsert で
        別の知識カードを黙って上書きしていた。
        """
        keys = [self._key_for(k) for k in ("日本語", "別", "中文キー", "한국어")]

        assert len(set(keys)) == len(keys), keys
        assert all(key != "repo" for key in keys), keys

    def test_same_explicit_key_stays_stable(self) -> None:
        """同じ明示 key からは常に同じ key が出ること（意図的な upsert を壊さない）。"""
        assert self._key_for("日本語") == self._key_for("日本語")

    def test_ascii_explicit_key_is_unchanged(self) -> None:
        """十分な ASCII を含む明示 key はそのまま slug 化されること。"""
        assert self._key_for("runtime-no-venv") == "runtime-no-venv"
