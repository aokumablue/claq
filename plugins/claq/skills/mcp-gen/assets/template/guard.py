"""ツール呼び出しの入口と出口を整える middleware（全サーバ共通。削除しない）。

SDK（`mcp` 2.3.0 で実測）の既定は、次の 3 点で設計規則と合わない。ここで補正する。

- 未知の引数を黙って捨てる。綴りを誤った絞り込み条件が無視され、誤った結果が
  成功として返る。→ `tools/call` で未知の引数を `isError` で拒否する。
- 公開する inputSchema の root が `additionalProperties` を閉じず、省略可能な引数を
  `anyOf` と `null` の組で表し、全ての項目に `title` を付ける。
  → `tools/list` で root を閉じ、`anyOf` を外し、`title` を除く。
  引数の検証は SDK が元の型で行うため、公開する形を絞っても受け付ける範囲は変わらない。
- structuredContent の写しの text を字下げ付きの JSON にする。→ 詰めた JSON に置き換える。

これとは別に、受信側のレート制限をここで掛ける（仕様上サーバの義務）。
`tools/call` を主体 × ツールで数え（`CallLimiter`）、上限を超えた呼び出しはツールを
呼ばずに `isError` で返し、間隔を空けるか条件を絞るよう促す。主体は server.py が渡す
関数で決める（ツール引数から取らない）。

`MCPServer.middleware` は SDK 2.x で暫定扱いの API である。SDK の版を上げたら、
smoke_check.py でこの 3 点がまだ効いていることを確かめる。

このファイルは標準ライブラリだけで書く（`mcp` を import しない）。SDK の無い環境でも
関数単位で検査できるようにするため。
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server import MCPServer

# 拒否の文面で名指しする未知の引数の数と、1 つの名前の長さの上限（要求の文字列をそのまま返さない）
ECHOED_NAMES = 5
ECHOED_NAME_MAX_CHARS = 64


class CallLimiter:
    """主体 × ツールの呼び出しの回数を、全キー共通の固定窓で数える。

    窓は `clock() // window_seconds` で区切り、窓が変わったら記録を丸ごと消す（記録は今の窓に
    呼んだ主体 × ツールの数で頭打ちになる）。固定窓なので、窓の境目をまたぐと短い間に
    最大で `calls` の 2 倍まで通る。それを許せない上限なら `calls` を半分にして渡す。

    Attributes:
        calls: 1 つの窓で主体 × ツールごとに通す回数（拒否の文面にも使う）。
        window_seconds: 窓の長さ（秒。拒否の文面にも使う）。
    """

    def __init__(self, calls: int, window_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        """上限と窓の長さを設定する。

        Args:
            calls: 1 つの窓で主体 × ツールごとに通す回数。
            window_seconds: 窓の長さ（秒）。
            clock: 単調増加する現在時刻を返す関数。
        """
        self.calls = calls
        self.window_seconds = window_seconds
        self._clock = clock
        self._window = int(clock() // window_seconds)
        self._counts: dict[tuple[str, str], int] = {}

    def admit(self, principal: str, tool: str) -> bool:
        """主体 principal がツール tool を今呼んでよいかを判定し、通すなら 1 回と数える。

        断った呼び出しは数えない（断られ続けても、次の窓には通る）。

        Args:
            principal: 呼び出しの主体。
            tool: 呼ばれたツールの名前。

        Returns:
            通すなら True。
        """
        window = int(self._clock() // self.window_seconds)
        if window != self._window:
            self._window = window
            self._counts.clear()
        used = self._counts.get((principal, tool), 0)
        if used >= self.calls:
            return False
        self._counts[(principal, tool)] = used + 1
        return True


def install_guard(
    server: MCPServer,
    *,
    limiter: CallLimiter,
    principal: Callable[[], str],
    principal_errors: tuple[type[Exception], ...] = (),
) -> None:
    """ガードを middleware の末尾に登録する。

    Args:
        server: ガードを掛けるサーバ。
        limiter: 主体 × ツールの呼び出しの制限。
        principal: 呼び出しの主体を返す関数（ツール引数から取らない）。
        principal_errors: principal が主体を決められないときに投げる型。これらはその文面の
            `isError` で返す。それ以外の例外はそのまま投げる。
    """

    async def guard(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        """入口で未知の引数と上限を超えた呼び出しを断り、出口で一覧と結果の形を整える。

        Args:
            ctx: SDK のリクエスト文脈（`method` と検証前の `params` を持つ）。
            call_next: 後続の処理を呼ぶ関数。

        Returns:
            後続の処理の結果、または呼び出しを断る結果。
        """
        if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
            refused = await _refuse_call(
                server, ctx.params, limiter=limiter, principal=principal, principal_errors=principal_errors
            )
            if refused is not None:
                return refused
        result = await call_next(ctx)
        # tools/list と tools/call の結果は dict（camelCase のキー）で届く（SDK で実測）
        if ctx.method == "tools/list":
            for tool in result["tools"]:
                tool["inputSchema"] = normalize_input_schema(tool["inputSchema"])
        elif ctx.method == "tools/call":
            compact_text(result)
        return result

    server.middleware.append(guard)


async def _refuse_call(
    server: MCPServer,
    params: Mapping[str, Any],
    *,
    limiter: CallLimiter,
    principal: Callable[[], str],
    principal_errors: tuple[type[Exception], ...],
) -> dict[str, Any] | None:
    """呼び出しを後続へ渡さずに断るなら、その結果を返す。

    順に、未知の引数を拒否し（数えない）、主体を決め、主体 × ツールの上限を超えていれば断る。
    ツールが見つからない・引数が object でない呼び出しは、数えずに SDK に任せる。

    Args:
        server: 公開中のツール定義を持つサーバ。
        params: `tools/call` の検証前のパラメータ。
        limiter: 主体 × ツールの呼び出しの制限。
        principal: 呼び出しの主体を返す関数。
        principal_errors: principal が主体を決められないときに投げる型。

    Returns:
        断る結果。後続へ渡すなら None。
    """
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        return None
    tool = next((tool for tool in await server.list_tools() if tool.name == params.get("name")), None)
    if tool is None:
        return None
    allowed = sorted(tool.input_schema.get("properties", {}))
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        return rejection(unknown, allowed, server_name=server.name, server_version=server.version)
    try:
        caller = principal()
    except principal_errors as error:
        return tool_error(str(error), server_name=server.name, server_version=server.version)
    if limiter.admit(caller, tool.name):
        return None
    return limited(
        tool.name, limiter.calls, limiter.window_seconds, server_name=server.name, server_version=server.version
    )


def _echoed(names: list[str]) -> str:
    """拒否の文面で名指しする未知の引数を、先頭の数件と切り詰めた名前にする。

    Args:
        names: 未知の引数名。

    Returns:
        読点でつないだ名前（残りがあれば件数を添える）。
    """
    shown = [
        name if len(name) <= ECHOED_NAME_MAX_CHARS else name[:ECHOED_NAME_MAX_CHARS] + "…"
        for name in names[:ECHOED_NAMES]
    ]
    rest = len(names) - len(shown)
    return "、".join(shown) + (f" ほか {rest} 個" if rest else "")


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
    text = f"未知の引数 {_echoed(unknown)} は受け付けません。使える引数: {names}。綴りを確かめて呼び直してください。"
    return tool_error(text, server_name=server_name, server_version=server_version)


def limited(tool: str, calls: int, window_seconds: float, *, server_name: str, server_version: str) -> dict[str, Any]:
    """主体 × ツールの呼び出しの上限を超えたことを伝えるツール実行エラーを組み立てる。

    Args:
        tool: 呼ばれたツールの名前。
        calls: 1 つの窓で通す回数。
        window_seconds: 窓の長さ（秒）。
        server_name: 結果の `_meta` に載せるサーバ名。
        server_version: 結果の `_meta` に載せるサーバの版。

    Returns:
        `isError: true` の CallToolResult（JSON の形）。
    """
    text = (
        f"{tool} の呼び出しが多すぎます（{window_seconds:g} 秒に {calls} 回まで）。"
        "間隔を空けて呼び直すか、条件を絞って呼び出しの回数を減らしてください。"
    )
    return tool_error(text, server_name=server_name, server_version=server_version)


def tool_error(text: str, *, server_name: str, server_version: str) -> dict[str, Any]:
    """モデルに読ませる文面 text のツール実行エラーを組み立てる。

    Args:
        text: 何が悪いかと、次に何をすればよいかを書いた文面。
        server_name: 結果の `_meta` に載せるサーバ名。
        server_version: 結果の `_meta` に載せるサーバの版。

    Returns:
        `isError: true` の CallToolResult（JSON の形）。
    """
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
    ツールが自分で組み立てた text（要約文など）と、JSON として読めない text（入れ子が
    深すぎるものを含む）には触れない。

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
    except (TypeError, ValueError, RecursionError):
        return
    if mirrored == structured:
        block["text"] = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
