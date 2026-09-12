"""``claq_mem_learn`` の引数を ``mem learn`` 用 JSON へ組み立てる。

`runtime/claq-helpers.sh` の `claq_mem_learn` は、値に含まれる引用符・改行・
非 ASCII をシェルでエスケープせずに済ませるため JSON へ marshal してから
`mem.cli learn` へ渡す。以前はこれを helpers 内の ``python3 -c`` で行って
いたが、裸の ``python3`` は Windows で Microsoft Store の App execution alias
に解決されて起動できない（release-verify 2026-09-03 の実測）。インタプリタ
解決は `runtime/claq-hook` の単一責務なので、marshal 処理もランチャ経由で
呼べるモジュールとして切り出し、helpers 側から ``python3`` の直接起動を
無くす。

値は ``CLAQ_LEARN_<FIELD>`` 環境変数で受け取る（引数に載せるとシェルの
語分割・展開を再び通ることになるため）。空文字列のフィールドは出力しない
（`mem.cli learn` 側の既定値を活かす）。``source``/``status`` は扱わない
——docs/adr/untrusted-input-prompt-boundary.md のとおり呼び出し元にこの 2 つを指定する権限は無い。
"""

from __future__ import annotations

import json
import os
import sys

_FIELDS = ("key", "kind", "scope", "title", "body", "domain", "confidence", "source_ref")
_ENV_PREFIX = "CLAQ_LEARN_"


def build_payload(environ: dict[str, str]) -> dict[str, str]:
    """``CLAQ_LEARN_*`` 環境変数から learn 用 payload を組み立てる。

    Args:
        environ: 参照する環境変数のマッピング。

    Returns:
        空でない値だけを持つ payload 辞書。

    Raises:
        例外は発生しません。
    """
    payload = {field: environ.get(_ENV_PREFIX + field.upper(), "") for field in _FIELDS}
    return {key: value for key, value in payload.items() if value}


def main() -> int:
    """payload を JSON として stdout へ書き出す。

    Args:
        引数はありません（``CLAQ_LEARN_*`` 環境変数から読み取る）。

    Returns:
        終了コード 0。

    Raises:
        例外は発生しません。
    """
    json.dump(build_payload(dict(os.environ)), sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
