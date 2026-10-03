"""ファサードの部品が投げる、モデルに読ませる失敗。"""

from __future__ import annotations


class FacadeError(Exception):
    """モデルに読ませる失敗。

    文面には「何が悪いか」と「次に何をすればよいか」を書く。内部の情報（上流の URL・
    スタックトレース・秘密・他の主体のデータの有無）は含めない。server.py が
    `ToolError` に変換して `isError: true` の結果として返す。
    """
