"""監査ログ（upstream-protection-overlay）。

個人情報の項目を返したことと、cursor を拒否したことを残す。主体そのものは残さず、cursor の
署名と同じ鍵の素材に用途の名前を前置した別の鍵（領域分離）で要約して残す。値は残さない。
共有のリモートで個人情報を返すときに重ねる。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable, Sequence

# 監査ログの主体の要約に使う鍵の前置き（cursor の署名と同じ鍵の素材から、別の鍵を作る）
_SUBJECT_DOMAIN = b"audit-subject:"


class AuditLog:
    """監査ログ。個人情報の項目を返したことと、cursor を拒否したことを残す。"""

    def __init__(self, secret: bytes, *, logger: logging.Logger) -> None:
        """鍵の素材と出力先を設定する。

        Args:
            secret: 鍵の素材（cursor の署名と同じ値でよい。用途の名前を前置して別の鍵にする）。
            logger: 監査ログの出力先。
        """
        self._key = _SUBJECT_DOMAIN + secret
        self._logger = logger

    def subject(self, principal: str) -> str:
        """主体を、監査ログに残す鍵付きの要約にする（ログから主体そのものを読ませない）。

        Args:
            principal: 呼び出しの主体。

        Returns:
            16 進数 32 文字の要約（同じ主体なら同じ値）。
        """
        digest = hmac.new(self._key, principal.encode("utf-8", "surrogatepass"), hashlib.sha256)
        return digest.hexdigest()[:32]

    def personal_returned(self, tool: str, paths: Sequence[str], count: int, *, principal: Callable[[], str]) -> None:
        """個人情報（personal）の項目を返したことを残す（値は残さない）。

        Args:
            tool: ツール名。
            paths: 結果に含めた個人情報の項目のパス（`Catalog.personal_paths` で絞ったもの）。
            count: その項目を含めて返したレコードの件数。
            principal: 主体を返す関数（記録するときだけ呼ぶ）。

        Raises:
            FacadeError: 主体を決められないとき（記録できないまま個人情報を返さない）。
        """
        if paths and count:
            self._logger.info(
                "personal fields returned: subject=%s tool=%s paths=%s count=%d",
                self.subject(principal()),
                tool,
                ",".join(paths),
                count,
            )

    def cursor_rejected(self, tool: str, principal: str) -> None:
        """cursor を拒否したことを残す。

        Args:
            tool: ツール名。
            principal: cursor を開こうとした主体。
        """
        self._logger.warning("cursor rejected: subject=%s tool=%s", self.subject(principal), tool)
