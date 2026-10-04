"""生成した MCP サーバを**実際に起動して**、仕様と設計規則への適合を確認するスモークチェック。

標準ライブラリのみで動く（`mcp` を import すらしない）。サーバを stdio の子プロセス
として起動し、生の JSON-RPC を書き込んで応答を読む。SDK を経由しないので
「SDK が正しいと言っている」ではなく「ワイヤに正しい形が出ている」を確認できる。

検査の対象（ツール名・引数・期待値・上限）は `smoke_config.py` に書く。このファイルと、
サーバを起動しない検査の部品（旧仕様の名前の走査・同梱するファイルの確認）を置いた
`smoke_static.py` は編集しない。

実行方法（**venv の interpreter で実行すること**）:
    .venv/bin/python3 smoke_check.py    # 単体実行（pytest 不要）
    .venv/bin/pytest smoke_check.py     # pytest からも実行できる（ファイル名の明示が必要）

MRTR（確認・追加入力）を持つサーバは `smoke_config.MRTR_CASES` に
**承認と拒否の両分岐**を書く。拒否がエラーになる実装ミスは
1 往復目だけの検査では見つからない。

この検査はサーバを `sys.executable` で子プロセスとして起動する。つまり
**この検査を動かした interpreter がそのままサーバの interpreter になる**。
PATH 上の裸の `python3` で実行すると、検査自体は起動するのにサーバ側だけが
`ModuleNotFoundError: No module named 'mcp'` で死ぬ。

ファイル名を `test_*.py` にしていないのは、置かれたリポジトリの pytest に自動収集
されないため。収集されると `mcp` 未導入の環境で無関係に失敗する。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import unittest
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import smoke_config as config
import smoke_static

# smoke_config.py で設定できる名前。HAPPY_* は必須で、残りは省略すると既定値になる
# （使わない検査の設定は書かない。知らない名前は test_settings_are_known が落とす）。
REQUIRED_SETTINGS = ("HAPPY_TOOL", "HAPPY_ARGS", "HAPPY_EXPECTED_STRUCTURED")
OPTIONAL_SETTINGS: dict[str, Any] = {
    "INVALID_TOOL": "",
    "INVALID_ARGS": {},
    "INVALID_EXPECTED_MESSAGE": "",
    "RESOURCE_URI": "",
    "RESOURCE_EXPECTED_SUBSTRING": "",
    "PROMPT_NAME": "",
    "PROMPT_ARGS": {},
    "PROMPT_EXPECTED_SUBSTRING": "",
    "READ_ONLY_SERVER": True,
    "MAX_TOOLS": 10,
    "MAX_TOOL_NAME_LENGTH": 40,
    "MAX_RESULT_CHARS": 20_000,
    "PAGED_TOOL": "",
    "PAGED_ARGS": {},
    "PAGED_CHANGED_ARGS": {},
    "PAGED_ENV": {},
    "CURSOR_ERROR_SUBSTRING": "cursor",
    "FIELDS_TOOL": "",
    "FIELDS_BAD_ARGS": {},
    "FIELDS_EXPECTED_SUGGESTION": "",
    "CLIENT_CAPABILITIES": {},
    "MRTR_CASES": [],
}
# 有効にした検査（キー）に要る期待値（値）。空のままだと `"" in text` のように検査が必ず通る。
PAIRED_SETTINGS: dict[str, tuple[str, ...]] = {
    "INVALID_TOOL": ("INVALID_EXPECTED_MESSAGE",),
    "RESOURCE_URI": ("RESOURCE_EXPECTED_SUBSTRING",),
    "PROMPT_NAME": ("PROMPT_EXPECTED_SUBSTRING",),
    "FIELDS_TOOL": ("FIELDS_BAD_ARGS", "FIELDS_EXPECTED_SUGGESTION"),
    "PAGED_TOOL": ("PAGED_ARGS", "PAGED_CHANGED_ARGS", "CURSOR_ERROR_SUBSTRING"),
}
MRTR_CASE_KEYS = ("tool", "arguments", "action", "expected_is_error", "expected_text")
DEFINED_SETTINGS = {name: value for name, value in vars(config).items() if name.isupper()}
cfg = SimpleNamespace(**{**OPTIONAL_SETTINGS, **DEFINED_SETTINGS})

SERVER_PATH = Path(__file__).with_name("server.py")
PROTOCOL_VERSION = "2026-07-28"
RESPONSE_TIMEOUT_SECONDS = 30.0
SDK_PROBE_TIMEOUT_SECONDS = 60
UNKNOWN_ARGUMENT = "zz_unknown_argument"
# ホストがツール完全名に許す文字は英数字・`_`・`-` に限られることが多い。`-` も避ける。
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# `description=` を書き忘れると docstring の節がモデル向けの説明へそのまま漏れる
DOCSTRING_SECTION_PATTERN = re.compile(r"^\s*(Args|Returns|Raises|Yields):\s*$", re.MULTILINE)
# 公開する inputSchema に残してはいけないキー（ガードが外す・入れ子のモデルを使わない）
FORBIDDEN_INPUT_SCHEMA_KEYS = ("anyOf", "oneOf", "allOf", "$ref", "$defs", "title")

# 2026-07-28 では各リクエストが自分でプロトコル版と capability を運ぶ。
# `initialize` ハンドシェイクは存在しないので、いきなり本題のリクエストを送る。
# MRTR を使うサーバは smoke_config.py の CLIENT_CAPABILITIES に elicitation を宣言する。
REQUEST_META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "smoke-check", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": cfg.CLIENT_CAPABILITIES,
}


class SmokeWarning(UserWarning):
    """検査は落とさないが、確かめられなかったことがあるという知らせ（main は WARN と表示する）。"""


def _spawn(env: dict[str, str] | None = None) -> subprocess.Popen:
    """サーバを stdio の子プロセスとして起動する。

    Args:
        env: 既存の環境変数に上書きで足す値。

    Returns:
        起動したプロセス。
    """
    return subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, **(env or {})},
    )


def _exchange(requests: list[dict], env: dict[str, str] | None = None) -> dict[int, dict]:
    """サーバを起動してリクエスト列を送り、id をキーにした応答を返す。

    stdin は全応答が揃うまで開いたままにする。先に閉じるとサーバが処理中の
    リクエストを落としたままシャットダウンし、応答が静かに欠ける。

    Args:
        requests: 送信する JSON-RPC リクエストの一覧。各要素は `id` を持つこと。
        env: サーバへ足す環境変数。

    Returns:
        リクエスト id をキー、応答オブジェクトを値とする辞書。

    Raises:
        AssertionError: 制限時間内に全応答が揃わなかったとき。
    """
    process = _spawn(env)
    responses: dict[int, dict] = {}
    complete = threading.Event()

    def _read() -> None:
        """stdout を 1 行ずつ読み、全応答が揃った時点でイベントを立てる。"""
        for line in process.stdout or []:
            stripped = line.strip()
            if not stripped:
                continue
            message = json.loads(stripped)
            if "id" in message:
                responses[message["id"]] = message
            if len(responses) == len(requests):
                complete.set()
                return

    threading.Thread(target=_read, daemon=True).start()
    try:
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        got_all = complete.wait(timeout=RESPONSE_TIMEOUT_SECONDS)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    stderr_tail = (process.stderr.read() if process.stderr else "")[-2000:]
    # 最頻の失敗は「サーバ側の python に mcp が入っていない」。この検査は自分では
    # mcp を import しないので**自分は起動でき**、死ぬのは spawn した子だけ。
    # 素の件数だけ出すと原因が stderr の末尾に埋もれるため、先に名指しする。
    if "ModuleNotFoundError" in stderr_tail and "mcp" in stderr_tail:
        raise AssertionError(
            f"サーバを起動した interpreter に mcp が入っていません: {sys.executable}\n"
            f"venv の python で実行してください（例: .venv/bin/python3 smoke_check.py）。\n"
            f"stderr:\n{stderr_tail}"
        )
    assert got_all, f"応答が {len(responses)}/{len(requests)} 件しか返りませんでした。stderr:\n{stderr_tail}"
    return responses


def _request(request_id: int, method: str, params: dict | None = None) -> dict:
    """`_meta` を載せた JSON-RPC リクエストを組み立てる。

    Args:
        request_id: リクエスト ID。
        method: 呼び出すメソッド名。
        params: `_meta` 以外のパラメータ。

    Returns:
        送信可能な JSON-RPC リクエスト。
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**(params or {}), "_meta": REQUEST_META},
    }


def _call(request_id: int, tool: str, arguments: dict) -> dict:
    """`tools/call` のリクエストを組み立てる。

    Args:
        request_id: リクエスト ID。
        tool: 呼び出すツール名。
        arguments: ツール引数。

    Returns:
        送信可能な JSON-RPC リクエスト。
    """
    return _request(request_id, "tools/call", {"name": tool, "arguments": arguments})


def _text(result: dict) -> str:
    """結果の text ブロックを連結して返す。

    Args:
        result: `tools/call` の result オブジェクト。

    Returns:
        text ブロックの本文を空白でつないだ文字列。
    """
    return " ".join(block.get("text", "") for block in result.get("content", []))


def _require(setting: str) -> None:
    """任意の検査の設定が無ければ、その検査を SKIP にする。

    「実行しなかった」を「合格」に潰さない。pytest でも SKIP として数えられる。

    Args:
        setting: 検査を有効にする設定の名前。

    Raises:
        unittest.SkipTest: 設定が無い（空の）とき。
    """
    if not getattr(cfg, setting):
        raise unittest.SkipTest(f"{setting} 未設定")


def _walk_keys(node: object) -> Iterator[str]:
    """スキーマに現れる全てのキーを列挙する（`properties` の引数名は除く）。

    Args:
        node: スキーマの節点。

    Yields:
        節点が持つキー。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            if key == "properties" and isinstance(value, dict):
                for child in value.values():
                    yield from _walk_keys(child)
            else:
                yield from _walk_keys(value)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_keys(child)


def elicit_round_trip(
    tool: str,
    arguments: dict,
    action: str,
    content: dict | None = None,
    setup: list[tuple[str, dict]] | None = None,
) -> dict:
    """MRTR の 1 往復目と再送を**同一プロセス**で往復し、最終結果を返す。

    `_exchange` は全リクエストを先に書き込むため、1 往復目の `requestState` を
    再送へ渡せない。MRTR を検査するにはこちらを使う。

    削除系のように**承認分岐が事前状態に依存する**ツールは `setup` を使う。
    サーバはリクエストごとに起動し直されるため、何も入っていないストアに対して
    削除を承認しても「対象が無い」エラーになり、MRTR の配線が正しいのか
    壊れているのかを区別できない。

    Args:
        tool: 呼び出すツール名。
        arguments: ツール引数。1 往復目と再送で同一のものを送る。
        action: クライアントの応答。`accept` / `decline` / `cancel`。
        content: `accept` のときに返す内容。
        setup: MRTR 呼び出しの前に同一プロセスへ送るツール呼び出しの一覧
            （`[(ツール名, 引数), ...]`）。事前状態を作るために使う。

    Returns:
        再送に対する最終 result オブジェクト。

    Raises:
        AssertionError: 事前呼び出しが失敗した、または 1 往復目が
            `input_required` を返さなかったとき。
    """
    process = _spawn()
    try:
        next_id = 1
        for setup_tool, setup_args in setup or []:
            process.stdin.write(json.dumps(_call(next_id, setup_tool, setup_args)) + "\n")
            process.stdin.flush()
            setup_result = json.loads(_read_line(process))["result"]
            assert setup_result.get("isError") is not True, (
                f"事前呼び出し {setup_tool} が失敗しました: {setup_result}"
            )
            next_id += 1

        process.stdin.write(json.dumps(_call(next_id, tool, arguments)) + "\n")
        process.stdin.flush()
        first = json.loads(_read_line(process))["result"]
        assert first.get("resultType") == "input_required", (
            f"1 往復目が input_required を返しませんでした: {first}"
        )
        key = next(iter(first["inputRequests"]))

        answer: dict = {"action": action}
        if action == "accept":
            answer["content"] = content or {}
        # 再送は**別の JSON-RPC id** で送る（同じ id は仕様違反）
        retry_params = {
            "name": tool,
            "arguments": arguments,
            "inputResponses": {key: answer},
            "requestState": first.get("requestState"),
        }
        process.stdin.write(json.dumps(_request(next_id + 1, "tools/call", retry_params)) + "\n")
        process.stdin.flush()
        return json.loads(_read_line(process))["result"]
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _read_line(process: subprocess.Popen) -> str:
    """サーバの stdout から応答を 1 行読む。

    Args:
        process: 起動済みのサーバプロセス。

    Returns:
        読み取った JSON 1 行。

    Raises:
        AssertionError: 応答が来ないまま stdout が閉じたとき。
    """
    for line in process.stdout or []:
        if line.strip():
            return line
    raise AssertionError(f"応答が返りませんでした。stderr:\n{(process.stderr.read() if process.stderr else '')[-2000:]}")


def test_protocol_surface() -> None:
    """server/discover・tools/list・tools/call が 2026-07-28 の形で応答すること。"""
    responses = _exchange(
        [
            _request(1, "server/discover"),
            _request(2, "tools/list"),
            _call(3, cfg.HAPPY_TOOL, cfg.HAPPY_ARGS),
            _request(4, "tools/list"),
        ]
    )

    discover = responses[1]["result"]
    assert PROTOCOL_VERSION in discover["supportedVersions"], discover
    assert discover["resultType"] == "complete", discover
    # サーバは接続状態に依存せず、毎回の result で自分の身元を名乗る
    assert discover["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], discover

    tools = responses[2]["result"]
    assert tools["resultType"] == "complete", tools
    assert tools["tools"], "ツールが 1 つも公開されていません"
    published = [tool["name"] for tool in tools["tools"]]
    # 編集し忘れを「未知ツール」として素通りさせず、ここで名指しで落とす
    assert cfg.HAPPY_TOOL in published, f"{cfg.HAPPY_TOOL} が未公開です。公開中: {sorted(published)}"
    if cfg.INVALID_TOOL:
        assert cfg.INVALID_TOOL in published, f"{cfg.INVALID_TOOL} が未公開です。公開中: {sorted(published)}"
    # ttlMs=0（既定）のままだとクライアントは毎回取り直す。意図した値か確認する
    assert tools["ttlMs"] > 0, f"cache_hints が効いていません: ttlMs={tools['ttlMs']}"
    assert tools["cacheScope"] in {"public", "private"}, tools
    # 順序が揺れるとクライアントのキャッシュとプロンプトキャッシュが効かない
    again = [tool["name"] for tool in responses[4]["result"]["tools"]]
    assert published == again, f"tools/list の順序が決定的ではありません: {published} / {again}"

    call = responses[3]["result"]
    assert call["resultType"] == "complete", call
    assert call.get("isError") is not True, call
    assert call["structuredContent"] == cfg.HAPPY_EXPECTED_STRUCTURED, call


def test_tool_definitions_follow_design() -> None:
    """公開するツール定義が設計規則（名前・説明・スキーマ・注釈・数）に従うこと。"""
    tools = _exchange([_request(1, "tools/list")])[1]["result"]["tools"]
    assert len(tools) <= cfg.MAX_TOOLS, (
        f"ツールが {len(tools)} 個あります（上限 {cfg.MAX_TOOLS}）。ツールセットに分けてください"
    )
    for tool in tools:
        name = tool["name"]
        assert TOOL_NAME_PATTERN.match(name), f"{name}: ツール名は英小文字・数字・_ だけで書く"
        assert len(name) <= cfg.MAX_TOOL_NAME_LENGTH, f"{name}: ツール名が長すぎます"
        description = tool.get("description") or ""
        assert description.strip(), f"{name}: description が空です"
        assert not DOCSTRING_SECTION_PATTERN.search(description), (
            f"{name}: docstring がそのまま説明になっています。@mcp.tool(description=...) で書いてください"
        )
        assert isinstance(tool.get("outputSchema"), dict), f"{name}: outputSchema がありません（戻り値の型を書く）"
        schema = tool["inputSchema"]
        assert schema.get("additionalProperties") is False, f"{name}: inputSchema が閉じていません（guard 未登録）"
        leaked = sorted(set(_walk_keys(schema)) & set(FORBIDDEN_INPUT_SCHEMA_KEYS))
        assert not leaked, f"{name}: inputSchema に {leaked} が残っています（入れ子のモデル引数を平坦にする）"
        annotations = tool.get("annotations") or {}
        if cfg.READ_ONLY_SERVER:
            assert annotations.get("readOnlyHint") is True, f"{name}: readOnlyHint=True を付けてください"
        if annotations.get("readOnlyHint") is True:
            misplaced = sorted({"idempotentHint", "destructiveHint"} & set(annotations))
            assert not misplaced, f"{name}: {misplaced} は readOnlyHint=False のときだけ意味を持ちます"


def test_unknown_argument_is_rejected() -> None:
    """未知の引数が黙って捨てられず、`isError` で名指しされること。"""
    arguments = {**cfg.HAPPY_ARGS, UNKNOWN_ARGUMENT: 1}
    result = _exchange([_call(1, cfg.HAPPY_TOOL, arguments)])[1]["result"]
    assert result.get("isError") is True, f"未知の引数が受け付けられました（guard 未登録）: {result}"
    assert UNKNOWN_ARGUMENT in _text(result), result


def test_text_mirrors_structured_content() -> None:
    """text が structuredContent と同じ内容の詰めた JSON で、予算内に収まること。"""
    result = _exchange([_call(1, cfg.HAPPY_TOOL, cfg.HAPPY_ARGS)])[1]["result"]
    expected = json.dumps(result["structuredContent"], ensure_ascii=False, separators=(",", ":"))
    assert _text(result) == expected, f"text が structuredContent の詰めた JSON ではありません: {_text(result)!r}"
    assert len(expected) <= cfg.MAX_RESULT_CHARS, f"結果が {len(expected)} 文字あります（上限 {cfg.MAX_RESULT_CHARS}）"


def test_tool_error_reaches_the_model() -> None:
    """ツール実行エラーが `isError: true` で返り、**メッセージまで届く**こと。

    `isError` だけを見ても不十分。素の例外（`ValueError` など）を投げていると
    SDK はメッセージを伏せて "Error executing tool <name>" しか返さないため、
    `isError` は立つのにモデルは何が悪かったのか分からない。
    モデルが自己修正できるのは `ToolError` で投げた場合だけ。
    """
    _require("INVALID_TOOL")
    responses = _exchange(
        [
            _request(1, "tools/list"),
            _call(2, cfg.INVALID_TOOL, cfg.INVALID_ARGS),
        ]
    )
    published = {tool["name"] for tool in responses[1]["result"]["tools"]}
    # 未知ツールも `isError: true` を返すため、実在確認なしでは本チェックが
    # 「名前を変え忘れただけ」で空振り合格してしまう
    assert cfg.INVALID_TOOL in published, f"{cfg.INVALID_TOOL} が未公開です。公開中: {sorted(published)}"

    message = responses[2]
    assert "error" not in message, f"プロトコルエラーになっています: {message}"
    result = message["result"]
    assert result["isError"] is True, result
    text = _text(result)
    # `INVALID_ARGS` が pydantic の引数検証で弾かれると、SDK が読めるメッセージを
    # 返すため下の検査は通ってしまい、`ToolError` の経路を一度も通らないまま
    # 本チェックが空振り合格する。引数検証ではなく**ツール本体まで到達して失敗する**
    # 引数を選ぶこと（存在しない ID など、型と範囲は正しいが業務的に失敗するもの）。
    assert "validation error" not in text, (
        f"INVALID_ARGS が引数検証で弾かれています。ToolError の経路を通る引数にしてください: {text!r}"
    )
    assert cfg.INVALID_EXPECTED_MESSAGE in text, (
        f"エラーメッセージがモデルに届いていません（素の例外を投げていませんか）: {text!r}"
    )


def test_resource_is_served() -> None:
    """公開したリソースが読め、キャッシュヒントが付いていること。

    `resources/read` も `CacheableMethod` なので、`cache_hints` に
    キーを書き忘れると `ttlMs=0`（毎回取り直し）のままになる。
    ツール側だけ見ていると気づけない。
    """
    _require("RESOURCE_URI")
    responses = _exchange([_request(1, "resources/read", {"uri": cfg.RESOURCE_URI})])
    result = responses[1]["result"]
    assert result["resultType"] == "complete", result
    text = " ".join(block.get("text", "") for block in result.get("contents", []))
    assert cfg.RESOURCE_EXPECTED_SUBSTRING in text, f"リソース本文が期待と違います: {text!r}"
    assert result["ttlMs"] > 0, (
        f"resources/read の cache_hints が未設定です: ttlMs={result['ttlMs']}"
    )


def test_prompt_is_served() -> None:
    """公開したプロンプトが引数を反映した本文を返すこと。

    `prompts/get` は `CacheableMethod` ではないので `ttlMs` は見ない
    （一覧の `prompts/list` にはヒントが載る）。
    """
    _require("PROMPT_NAME")
    responses = _exchange(
        [_request(1, "prompts/get", {"name": cfg.PROMPT_NAME, "arguments": cfg.PROMPT_ARGS})]
    )
    result = responses[1]["result"]
    assert result["resultType"] == "complete", result
    text = " ".join(
        message.get("content", {}).get("text", "") for message in result.get("messages", [])
    )
    assert cfg.PROMPT_EXPECTED_SUBSTRING in text, f"プロンプト本文が期待と違います: {text!r}"


def test_paging_round_trip() -> None:
    """カーソルが別のプロセスでも続きを返し、改ざんと条件の変更を拒むこと。

    1 ページ目と 2 ページ目を**別々のプロセス**で取る。カーソルが自己完結していれば
    （共有の鍵だけで）どのインスタンスでも続きを返せる。
    """
    _require("PAGED_TOOL")
    first = _exchange([_call(1, cfg.PAGED_TOOL, cfg.PAGED_ARGS)], cfg.PAGED_ENV)[1]["result"]
    assert first.get("isError") is not True, first
    page1 = first["structuredContent"]
    cursor = page1.get("next_cursor")
    assert isinstance(cursor, str) and cursor, f"1 ページ目に next_cursor がありません: {page1}"
    assert len(_text(first)) <= cfg.MAX_RESULT_CHARS, "1 ページ目が予算を超えています"

    tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    responses = _exchange(
        [
            _call(1, cfg.PAGED_TOOL, {**cfg.PAGED_ARGS, "cursor": cursor}),
            _call(2, cfg.PAGED_TOOL, {**cfg.PAGED_ARGS, "cursor": tampered}),
            _call(3, cfg.PAGED_TOOL, {**cfg.PAGED_CHANGED_ARGS, "cursor": cursor}),
        ],
        cfg.PAGED_ENV,
    )
    second = responses[1]["result"]
    assert second.get("isError") is not True, f"別のプロセスで続きを取れません: {second}"
    page2_items = second["structuredContent"]["items"]
    assert page2_items, "2 ページ目が空です"
    seen = {json.dumps(item, sort_keys=True) for item in page1["items"]}
    assert not any(json.dumps(item, sort_keys=True) in seen for item in page2_items), "ページが重複しています"
    for request_id, label in ((2, "改ざんしたカーソル"), (3, "条件を変えたカーソル")):
        rejected = responses[request_id]["result"]
        assert rejected.get("isError") is True, f"{label}が受け付けられました: {rejected}"
        assert cfg.CURSOR_ERROR_SUBSTRING in _text(rejected), rejected


def test_unknown_field_gets_suggestion() -> None:
    """未知の項目パスが `isError` になり、近い候補が示されること。"""
    _require("FIELDS_TOOL")
    responses = _exchange([_request(1, "tools/list"), _call(2, cfg.FIELDS_TOOL, cfg.FIELDS_BAD_ARGS)])
    published = {tool["name"] for tool in responses[1]["result"]["tools"]}
    # 未知ツールも `isError: true` を返すため、実在確認なしでは名前の変え忘れで空振り合格する
    assert cfg.FIELDS_TOOL in published, f"{cfg.FIELDS_TOOL} が未公開です。公開中: {sorted(published)}"
    result = responses[2]["result"]
    assert result.get("isError") is True, result
    assert cfg.FIELDS_EXPECTED_SUGGESTION in _text(result), result


def test_mrtr_round_trips() -> None:
    """MRTR の各ケースで、再送の最終結果が期待どおりであること（承認と拒否の両分岐）。"""
    _require("MRTR_CASES")
    for case in cfg.MRTR_CASES:
        result = elicit_round_trip(
            case["tool"], case["arguments"], case["action"], case.get("content"), case.get("setup")
        )
        label = f"{case['tool']} / {case['action']}"
        assert result.get("isError") is case["expected_is_error"], f"{label}: {result}"
        assert case["expected_text"] in _text(result), f"{label}: {result}"


def _missing_expectations() -> list[str]:
    """有効にした検査に、対になる期待値が欠けていないかを調べる。

    Returns:
        足りない設定を説明する文の一覧。
    """
    problems = [
        f"{enabled} を設定したなら {needed} も空でない値で書いてください（空のままでは検査が必ず通ります）"
        for enabled, needed_names in PAIRED_SETTINGS.items()
        if getattr(cfg, enabled)
        for needed in needed_names
        if not getattr(cfg, needed)
    ]
    if cfg.PAGED_TOOL and cfg.PAGED_CHANGED_ARGS and cfg.PAGED_CHANGED_ARGS == cfg.PAGED_ARGS:
        problems.append("PAGED_CHANGED_ARGS は PAGED_ARGS の値を 1 つ変えた条件にしてください（同じでは条件の変更の拒否を確かめられません）")
    for index, case in enumerate(cfg.MRTR_CASES):
        missing = [key for key in MRTR_CASE_KEYS if key not in case] if isinstance(case, dict) else list(MRTR_CASE_KEYS)
        if missing:
            problems.append(f"MRTR_CASES[{index}] に {missing} がありません")
        elif not case["expected_text"]:
            problems.append(f"MRTR_CASES[{index}] の expected_text が空です（空では結果の文面を確かめられません）")
    return problems


def test_settings_are_known() -> None:
    """smoke_config.py に必須の設定があり、知らない名前（綴りの誤り）が無く、有効にした検査の期待値が揃っていること。"""
    missing = [name for name in REQUIRED_SETTINGS if name not in DEFINED_SETTINGS]
    unknown = sorted(set(DEFINED_SETTINGS) - set(REQUIRED_SETTINGS) - set(OPTIONAL_SETTINGS))
    assert not missing, f"smoke_config.py に {missing} がありません"
    assert not unknown, f"smoke_config.py の {unknown} は使われない名前です（綴りを確かめる）。使える名前: {sorted(OPTIONAL_SETTINGS)}"
    problems = _missing_expectations()
    assert not problems, "smoke_config.py の期待値が足りません:\n" + "\n".join(problems)


def test_no_legacy_api() -> None:
    """生成先のファイルに、旧仕様の API 名・機構が残っていないこと。

    説明文に書いただけでも当たる（正規表現は呼び出しと散文を区別しない）。旧 API に
    触れるなら「`Context` の `elicit` メソッド」のように受け手と名前を分けて書く。
    パターンの正本を持つ smoke_static.py と、この smoke_check.py は走査しない。
    走査しなかったファイルは SmokeWarning で 1 件ずつ知らせる（main は WARN と表示する）。
    """
    here = Path(__file__).resolve()
    hits, unscanned = smoke_static.legacy_api_hits(here.parent, skip=(here, Path(smoke_static.__file__)))
    for entry in unscanned:
        warnings.warn(f"旧仕様の名前を走査していません: {entry}", SmokeWarning, stacklevel=2)
    assert not hits, "旧仕様の API 名・機構が残っています:\n" + "\n".join(hits)


def test_packaging_includes_local_modules() -> None:
    """pyproject.toml の only-include が、server.py から import する生成先のモジュールを全て含むこと。

    only-include に無いファイルは wheel に入らず、install したサーバが起動時に
    ModuleNotFoundError で落ちる（生成先のディレクトリで起動するこの検査の他の項目では気づけない）。
    """
    root = Path(__file__).resolve().parent
    if not (root / "pyproject.toml").is_file():
        raise unittest.SkipTest("pyproject.toml が無い")
    gaps = smoke_static.packaging_gaps(root)
    assert not gaps, (
        f"pyproject.toml の [tool.hatch.build.targets.wheel] の only-include に {gaps} がありません。"
        "server.py から import されるので、足さないと install したサーバが ModuleNotFoundError で起動しません"
    )


def sdk_protocol_warning() -> str | None:
    """SDK の最新プロトコル版が、このスケルトンの版と違えば警告文を返す。

    Returns:
        警告文。版が同じとき、または SDK を読めないとき（その場合はサーバの起動の検査が落ちる）は None。
        確かめる子プロセスが時間切れなら、確かめられなかったことを書いた警告文。
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "import mcp.types as t; print(t.LATEST_PROTOCOL_VERSION)"],
            capture_output=True,
            text=True,
            timeout=SDK_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            f"SDK の最新プロトコル版を {SDK_PROBE_TIMEOUT_SECONDS} 秒以内に確かめられませんでした。"
            f"スケルトンの {PROTOCOL_VERSION} と同じかどうかは確かめられません"
        )
    latest = completed.stdout.strip()
    if completed.returncode != 0 or latest == PROTOCOL_VERSION:
        return None
    return f"SDK の最新プロトコル版は {latest} で、スケルトンの {PROTOCOL_VERSION} と違います。SDK の changelog を読み直してください"


# main が順に回す検査（設定の検査は先に単独で回す）。任意の検査は、設定が無ければ自分で SKIP する。
CHECKS: tuple[Callable[[], None], ...] = (
    test_protocol_surface,
    test_tool_definitions_follow_design,
    test_unknown_argument_is_rejected,
    test_text_mirrors_structured_content,
    test_no_legacy_api,
    test_packaging_includes_local_modules,
    test_tool_error_reaches_the_model,
    test_resource_is_served,
    test_prompt_is_served,
    test_paging_round_trip,
    test_unknown_field_gets_suggestion,
    test_mrtr_round_trips,
)


def _run_check(check: Callable[[], None]) -> bool:
    """検査を 1 つ実行し、PASS・FAIL・SKIP と、検査が出した SmokeWarning を WARN として表示する。

    SmokeWarning 以外の警告は、元どおり `warnings.showwarning` に渡す。

    Args:
        check: 実行する検査。

    Returns:
        落ちたら True。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SmokeWarning)
        failed = _execute(check)
    for warning in caught:
        if issubclass(warning.category, SmokeWarning):
            print(f"WARN {check.__name__}: {warning.message}", file=sys.stderr)
        else:
            warnings.showwarning(warning.message, warning.category, warning.filename, warning.lineno, warning.file, warning.line)
    return failed


def _execute(check: Callable[[], None]) -> bool:
    """検査を 1 つ実行し、PASS・FAIL・SKIP を表示する。

    Args:
        check: 実行する検査。

    Returns:
        落ちたら True。
    """
    try:
        check()
    except unittest.SkipTest as skipped:
        print(f"SKIP {check.__name__} ({skipped})", file=sys.stderr)
    except AssertionError as error:
        print(f"FAIL {check.__name__}: {error}", file=sys.stderr)
        return True
    else:
        print(f"PASS {check.__name__}", file=sys.stderr)
    return False


def main() -> int:
    """全チェックを実行し、終了コードを返す。

    Returns:
        全て通れば 0（SKIP は落ちたことにしない）、1 つでも落ちれば 1。
    """
    if _run_check(test_settings_are_known):
        return 1
    failures = sum(_run_check(check) for check in CHECKS)
    warning = sdk_protocol_warning()
    if warning:
        print(f"WARN {warning}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
