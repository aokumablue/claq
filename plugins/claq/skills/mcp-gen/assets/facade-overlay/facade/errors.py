"""ファサードの部品が投げる、モデルに読ませる失敗。"""

from __future__ import annotations

CURSOR_REJECTED = "この cursor は使えません。cursor を外して、同じ条件で検索し直してください。"


class FacadeError(Exception):
    """モデルに読ませる失敗。

    文面には「何が悪いか」と「次に何をすればよいか」を書く。内部の情報（上流の URL・
    スタックトレース・秘密・他の主体のデータの有無）は含めない。server.py が
    `ToolError` に変換して `isError: true` の結果として返す。
    """


class CursorError(FacadeError):
    """使えない cursor（改ざん・形式の不正・別の用途や主体や検索条件・版違い・位置の不正・期限切れ）。

    理由ごとに文面を変えない（他の主体の cursor かどうかを漏らさない）。期限切れだけは
    署名を確かめた後に分かるので、別の文面にしてよい。
    """

    def __init__(self, message: str = CURSOR_REJECTED) -> None:
        """文面を設定する。

        Args:
            message: モデルに読ませる文面。既定は理由を区別しない拒否の文面。
        """
        super().__init__(message)
