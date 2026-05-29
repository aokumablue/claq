"""team: build_team_context のフォーマッティングと予算制御のテスト。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from devgear.mem.settings import TeamSettings
from devgear.mem.team_context import build_team_context


@dataclass
class _FakePg:
    """検索結果とチャンクをプリセットする最小スタブ。"""

    ranked: list[tuple[str, float]]
    chunks: dict[str, dict]
    fts_calls: list[dict[str, Any]]
    team_calls: list[dict[str, Any]]
    fetch_calls: list[list[str]]

    def fts_search(self, query: str, limit: int = 20, *, exclude_origin_user=None):  # noqa: ANN001
        self.fts_calls.append(
            {"query": query, "limit": limit, "exclude": exclude_origin_user}
        )
        return self.ranked

    def team_search(
        self, query: str, embedding, limit: int = 20, *, exclude_origin_user=None
    ):  # noqa: ANN001
        self.team_calls.append(
            {
                "query": query,
                "embedding": embedding,
                "limit": limit,
                "exclude": exclude_origin_user,
            }
        )
        return self.ranked

    def fetch_chunks_by_ids(self, chunk_ids: list[str]) -> dict[str, dict]:
        self.fetch_calls.append(list(chunk_ids))
        return {cid: self.chunks[cid] for cid in chunk_ids if cid in self.chunks}


def _make_pg(ranked: list[tuple[str, float]], chunks: dict[str, dict]) -> _FakePg:
    return _FakePg(
        ranked=ranked,
        chunks=chunks,
        fts_calls=[],
        team_calls=[],
        fetch_calls=[],
    )


def _chunk(
    *,
    origin_user: str = "alice",
    project: str = "proj",
    content: str = "hello world",
    user_prompt: str = "do it",
    tools: list[str] | None = None,
    files: list[str] | None = None,
    created: int = 1_700_000_000,
) -> dict:
    return {
        "origin_user": origin_user,
        "project": project,
        "content": content,
        "user_prompt": user_prompt,
        "tool_names": tools or ["Edit"],
        "files_read": [],
        "files_modified": files or ["a.py"],
        "created_at_epoch": created,
    }


def test_build_team_context_fts_mode_outputs_tag_and_header() -> None:
    chunks = {"c-1": _chunk(origin_user="bob", project="x-picflow", content="fix bug")}
    pg = _make_pg([("c-1", 0.9)], chunks)

    out = build_team_context(
        pg,  # type: ignore[arg-type]
        query="x-picflow",
        exclude_origin_user="alice",
        settings=TeamSettings(),
        mode="fts",
    )

    assert out.startswith("<team-context>")
    assert out.endswith("</team-context>")
    assert "## x-picflow (author: bob" in out
    assert "fix bug" in out
    # exclude が FTS に伝搬
    assert pg.fts_calls and pg.fts_calls[0]["exclude"] == "alice"


def test_build_team_context_empty_query_returns_blank() -> None:
    pg = _make_pg([], {})
    assert (
        build_team_context(
            pg,  # type: ignore[arg-type]
            query="   ",
            exclude_origin_user="me",
            settings=TeamSettings(),
            mode="fts",
        )
        == ""
    )


def test_build_team_context_no_results_returns_blank() -> None:
    pg = _make_pg([], {})
    assert (
        build_team_context(
            pg,  # type: ignore[arg-type]
            query="anything",
            exclude_origin_user="me",
            settings=TeamSettings(),
            mode="fts",
        )
        == ""
    )


def test_build_team_context_missing_rows_returns_blank() -> None:
    pg = _make_pg([("missing", 0.9)], {})
    assert (
        build_team_context(
            pg,  # type: ignore[arg-type]
            query="anything",
            exclude_origin_user="me",
            settings=TeamSettings(),
            mode="fts",
        )
        == ""
    )


def test_build_team_context_hybrid_requires_embedding_model() -> None:
    # embedding_model 未指定時は FTS フォールバックして結果を返す
    pg = _make_pg([("c-1", 0.5)], {"c-1": _chunk()})
    out = build_team_context(
        pg,  # type: ignore[arg-type]
        query="q",
        exclude_origin_user="me",
        settings=TeamSettings(),
        mode="hybrid",
    )
    assert out != ""  # FTS フォールバックで結果あり
    assert "<team-context>" in out


def test_build_team_context_hybrid_uses_team_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devgear.mem import team_context as module

    captured = {}

    def _fake_embed(query: str, model: str) -> list[float]:
        captured["query"] = query
        captured["model"] = model
        return [0.1, 0.2]

    # 遅延 import 先をパッチ（team_context.py 内の from ... import を上書き）
    monkeypatch.setattr("devgear.mem.embedding.embed_query", _fake_embed)

    pg = _make_pg([("c-1", 0.8)], {"c-1": _chunk(content="team-knowledge")})
    out = module.build_team_context(
        pg,  # type: ignore[arg-type]
        query="bug fix",
        exclude_origin_user="me",
        settings=TeamSettings(),
        mode="hybrid",
        embedding_model="ruri",
    )

    assert "team-knowledge" in out
    assert pg.team_calls and pg.team_calls[0]["exclude"] == "me"
    assert captured["model"] == "ruri"


def test_build_team_context_token_budget_truncates() -> None:
    chunks = {
        "c-1": _chunk(content="A" * 500),
        "c-2": _chunk(content="B" * 500),
        "c-3": _chunk(content="C" * 500),
    }
    pg = _make_pg([("c-1", 0.9), ("c-2", 0.8), ("c-3", 0.7)], chunks)

    out = build_team_context(
        pg,  # type: ignore[arg-type]
        query="q",
        exclude_origin_user="me",
        settings=TeamSettings(max_tokens=200),  # ≒ 700 文字予算
        mode="fts",
    )

    # 最低 1 件は入り、全 3 件が入りきらない（≒ 1500+ 文字）こと
    assert "AAAA" in out
    assert not ("AAAA" in out and "BBBB" in out and "CCCC" in out)


def test_build_team_context_budget_too_small_returns_blank() -> None:
    pg = _make_pg([("c-1", 0.9)], {"c-1": _chunk(content="A" * 1000)})
    assert (
        build_team_context(
            pg,  # type: ignore[arg-type]
            query="q",
            exclude_origin_user="me",
            settings=TeamSettings(max_tokens=1),
            mode="fts",
        )
        == ""
    )


def test_build_team_context_fetch_exception_returns_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = _make_pg([("c-1", 0.9)], {"c-1": _chunk()})

    def _boom(chunk_ids: list[str]) -> dict[str, dict]:
        raise RuntimeError("db down")

    monkeypatch.setattr(pg, "fetch_chunks_by_ids", _boom)
    out = build_team_context(
        pg,  # type: ignore[arg-type]
        query="q",
        exclude_origin_user="me",
        settings=TeamSettings(),
        mode="fts",
    )
    assert out == ""


def test_build_team_context_search_exception_returns_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = _make_pg([], {})

    def _boom(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("pg down")

    monkeypatch.setattr(pg, "fts_search", _boom)
    out = build_team_context(
        pg,  # type: ignore[arg-type]
        query="q",
        exclude_origin_user="me",
        settings=TeamSettings(),
        mode="fts",
    )
    assert out == ""


def test_format_timestamp_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    from devgear.mem import team_context as module

    class _BadDatetime:
        @staticmethod
        def fromtimestamp(*args, **kwargs):  # noqa: ANN001, ANN002
            raise ValueError("bad")

    monkeypatch.setattr(module, "datetime", _BadDatetime)
    assert module._format_timestamp(0) == "unknown"
    assert module._format_timestamp(1) == "invalid"
