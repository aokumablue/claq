"""生成した MCP サーバを**実際に起動して**プロトコル適合を確認するスモークチェック。

標準ライブラリのみで動く（`mcp` を import すらしない）。サーバを stdio の子プロセス
として起動し、生の JSON-RPC を書き込んで応答を読む。SDK を経由しないので
「SDK が正しいと言っている」ではなく「ワイヤに正しい形が出ている」を確認できる。

実行方法:
    python3 smoke_check.py        # 単体実行（pytest 不要）
    pytest smoke_check.py         # pytest からも実行できる（ファイル名の明示が必要）

ファイル名を `test_*.py` にしていないのは、置かれたリポジトリの pytest に自動収集
されないため。収集されると `mcp` 未導入の環境で無関係に失敗する。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

SERVER_PATH = Path(__file__).with_name("server.py")
PROTOCOL_VERSION = "2026-07-28"
RESPONSE_TIMEOUT_SECONDS = 30.0

# ==== ここを生成したサーバに合わせて編集する ====
# サンプルのツール名のままだと `Unknown tool` で落ちる。
HAPPY_TOOL = "add"
HAPPY_ARGS: dict = {"a": 2, "b": 3}
HAPPY_EXPECTED_STRUCTURED: dict = {"result": 5.0}
# 入力検証で必ず弾かれる引数を渡すツール（実在するツールであること）
INVALID_TOOL = "fetch_items"
INVALID_ARGS: dict = {"count": 0}
# ==== 編集ここまで ====

# 2026-07-28 では各リクエストが自分でプロトコル版と capability を運ぶ。
# `initialize` ハンドシェイクは存在しないので、いきなり本題のリクエストを送る。
REQUEST_META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "smoke-check", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _exchange(requests: list[dict]) -> dict[int, dict]:
    """サーバを起動してリクエスト列を送り、id をキーにした応答を返す。

    stdin は全応答が揃うまで開いたままにする。先に閉じるとサーバが処理中の
    リクエストを落としたままシャットダウンし、応答が静かに欠ける。

    Args:
        requests: 送信する JSON-RPC リクエストの一覧。各要素は `id` を持つこと。

    Returns:
        リクエスト id をキー、応答オブジェクトを値とする辞書。

    Raises:
        AssertionError: 制限時間内に全応答が揃わなかったとき。
    """
    process = subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
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


def test_protocol_surface() -> None:
    """server/discover・tools/list・tools/call が 2026-07-28 の形で応答すること。"""
    responses = _exchange(
        [
            _request(1, "server/discover"),
            _request(2, "tools/list"),
            _request(3, "tools/call", {"name": HAPPY_TOOL, "arguments": HAPPY_ARGS}),
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
    published = {tool["name"] for tool in tools["tools"]}
    # 編集し忘れを「未知ツール」として素通りさせず、ここで名指しで落とす
    assert HAPPY_TOOL in published, f"{HAPPY_TOOL} が未公開です。公開中: {sorted(published)}"
    assert INVALID_TOOL in published, f"{INVALID_TOOL} が未公開です。公開中: {sorted(published)}"
    # ttlMs=0（既定）のままだとクライアントは毎回取り直す。意図した値か確認する
    assert tools["ttlMs"] > 0, f"cache_hints が効いていません: ttlMs={tools['ttlMs']}"
    assert tools["cacheScope"] in {"public", "private"}, tools

    call = responses[3]["result"]
    assert call["resultType"] == "complete", call
    assert call.get("isError") is not True, call
    assert call["structuredContent"] == HAPPY_EXPECTED_STRUCTURED, call


def test_tool_execution_error_is_not_a_protocol_error() -> None:
    """入力検証の失敗が `isError: true` で返り、JSON-RPC error にならないこと。

    モデルが自己修正できるのは前者だけ。ここが逆転していると、
    引数ミスのたびに会話が復帰不能になる。
    """
    responses = _exchange(
        [
            _request(1, "tools/list"),
            _request(2, "tools/call", {"name": INVALID_TOOL, "arguments": INVALID_ARGS}),
        ]
    )
    published = {tool["name"] for tool in responses[1]["result"]["tools"]}
    # 未知ツールも `isError: true` を返すため、実在確認なしでは本チェックが
    # 「名前を変え忘れただけ」で空振り合格してしまう
    assert INVALID_TOOL in published, f"{INVALID_TOOL} が未公開です。公開中: {sorted(published)}"

    message = responses[2]
    assert "error" not in message, f"プロトコルエラーになっています: {message}"
    assert message["result"]["isError"] is True, message


def main() -> int:
    """全チェックを実行し、終了コードを返す。

    Returns:
        全て通れば 0、1 つでも落ちれば 1。
    """
    failures = 0
    for check in (test_protocol_surface, test_tool_execution_error_is_not_a_protocol_error):
        try:
            check()
        except AssertionError as error:
            failures += 1
            print(f"FAIL {check.__name__}: {error}", file=sys.stderr)
        else:
            print(f"PASS {check.__name__}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
