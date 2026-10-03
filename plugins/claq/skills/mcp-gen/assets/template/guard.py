"""ツール呼び出しの入口と出口を整える middleware（全サーバ共通。削除しない）。

SDK（`mcp` 2.3.0 で実測）の既定は、次の 3 点で設計規則と合わない。ここで補正する。

- 未知の引数を黙って捨てる。綴りを誤った絞り込み条件が無視され、誤った結果が
  成功として返る。→ `tools/call` で未知の引数を `isError` で拒否する。
- 公開する inputSchema の root が `additionalProperties` を閉じず、省略可能な引数を
  `anyOf` と `null` の組で表し、全ての項目に `title` を付ける。
  → `tools/list` で root を閉じ、`anyOf` を外し、`title` を除く。
  引数の検証は SDK が元の型で行うため、公開する形を絞っても受け付ける範囲は変わらない。
- structuredContent の写しの text を字下げ付きの JSON にする。→ 詰めた JSON に置き換える。

`MCPServer.middleware` は SDK 2.x で暫定扱いの API である。SDK の版を上げたら、
smoke_check.py でこの 3 点がまだ効いていることを確かめる。

このファイルは標準ライブラリだけで書く（`mcp` を import しない）。SDK の無い環境でも
関数単位で検査できるようにするため。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server import MCPServer


def install(server: MCPServer) -> None:
    """ガードを middleware の末尾に登録する。

    Args:
        server: ガードを掛けるサーバ。
    """

    async def guard(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        """入口で未知の引数を拒否し、出口で一覧と結果の形を整える。

        Args:
            ctx: SDK のリクエスト文脈（`method` と検証前の `params` を持つ）。
            call_next: 後続の処理を呼ぶ関数。

        Returns:
            後続の処理の結果、または未知の引数を拒否する結果。
        """
        if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
            rejected = await _reject_unknown_arguments(server, ctx.params)
            if rejected is not None:
                return rejected
        result = await call_next(ctx)
        if isinstance(result, dict):
            if ctx.method == "tools/list":
                for tool in result.get("tools", []):
                    tool["inputSchema"] = normalize_input_schema(tool.get("inputSchema") or {})
            elif ctx.method == "tools/call":
                compact_text(result)
        return result

    server.middleware.append(guard)


async def _reject_unknown_arguments(server: MCPServer, params: Mapping[str, Any]) -> dict[str, Any] | None:
    """呼ばれたツールに無い引数があれば、拒否する結果を返す。

    Args:
        server: 公開中のツール定義を持つサーバ。
        params: `tools/call` の検証前のパラメータ。

    Returns:
        拒否する結果。未知の引数が無い・ツールが見つからない・引数が object でない
        ときは None（その先の判断は SDK に任せる）。
    """
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        return None
    for tool in await server.list_tools():
        if tool.name != params.get("name"):
            continue
        allowed = sorted((tool.input_schema or {}).get("properties", {}))
        unknown = unknown_arguments(arguments, allowed)
        if not unknown:
            return None
        return rejection(unknown, allowed, server_name=server.name, server_version=server.version)
    return None


def unknown_arguments(arguments: Mapping[str, Any], allowed: list[str]) -> list[str]:
    """許されていない引数名を、名前順で返す。

    Args:
        arguments: 呼び出しの引数。
        allowed: ツールが受け付ける引数名。

    Returns:
        未知の引数名の一覧。
    """
    return sorted(set(arguments) - set(allowed))


def rejection(unknown: list[str], allowed: list[str], *, server_name: str, server_version: str) -> dict[str, Any]:
    """未知の引数を拒否するツール実行エラーを組み立てる。

    Args:
        unknown: 未知の引数名。
        allowed: 受け付ける引数名。
        server_name: 結果の `_meta` に載せるサーバ名。
        server_version: 結果の `_meta` に載せるサーバの版。

    Returns:
        `isError: true` の CallToolResult（JSON の形）。
    """
    names = "、".join(allowed) if allowed else "なし"
    text = f"未知の引数 {'、'.join(unknown)} は受け付けません。使える引数: {names}。綴りを確かめて呼び直してください。"
    return {
        "resultType": "complete",
        "isError": True,
        "content": [{"type": "text", "text": text}],
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": server_name, "version": server_version}},
    }


def normalize_input_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """公開する inputSchema を、ホストを選ばない形に整える。

    root の `additionalProperties` を閉じ（未知の引数は入口で拒否するため、公開する形と
    一致する）、省略可能な引数の `anyOf: [X, null]` を X にし、`title` と
    `default: null` を除く。引数名そのものは変えない。

    Args:
        schema: SDK が生成した inputSchema。

    Returns:
        整えた inputSchema（元の辞書は変更しない）。
    """
    normalized = _normalize_node(copy.deepcopy(dict(schema)))
    normalized.setdefault("additionalProperties", False)
    return normalized


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    """スキーマの 1 節点を整え、子の節点へ再帰する。

    `properties` は「引数名 → スキーマ」の対応なので、名前（キー）には触れず値だけを整える。

    Args:
        node: スキーマの節点。

    Returns:
        整えた節点。
    """
    variants = node.get("anyOf")
    if isinstance(variants, list) and len(variants) == 2 and {"type": "null"} in variants:
        node.pop("anyOf")
        kept = next(variant for variant in variants if variant != {"type": "null"})
        node = {**kept, **node}
        if node.get("default", ...) is None:
            node.pop("default")
    node.pop("title", None)
    for key in ("properties", "$defs"):
        children = node.get(key)
        if isinstance(children, dict):
            node[key] = {name: _normalize_node(child) for name, child in children.items() if isinstance(child, dict)}
    items = node.get("items")
    if isinstance(items, dict):
        node["items"] = _normalize_node(items)
    return node


def compact_text(result: dict[str, Any]) -> None:
    """structuredContent の写しの text を、詰めた JSON に置き換える。

    text が 1 ブロックだけで、その中身が structuredContent と同じ JSON のときに限る。
    ツールが自分で組み立てた text（要約文など）には触れない。

    Args:
        result: `tools/call` の結果（JSON の形）。その場で書き換える。
    """
    structured = result.get("structuredContent")
    content = result.get("content")
    if structured is None or not isinstance(content, list) or len(content) != 1:
        return
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        return
    try:
        mirrored = json.loads(block.get("text", ""))
    except (TypeError, ValueError):
        return
    if mirrored == structured:
        block["text"] = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
