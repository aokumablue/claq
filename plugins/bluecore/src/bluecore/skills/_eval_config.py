"""eval 実行設定の共通パラメータオブジェクト定義。

run_eval / run_loop から共有される frozen dataclass を提供する。
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvalConfig:
    """eval 実行設定をまとめたパラメータオブジェクト。

    Attributes:
        num_workers: 並列ワーカー数。
        timeout: クエリごとのタイムアウト秒数。
        project_root: プロジェクトルートパス。
        runs_per_query: クエリごとの実行回数。
        trigger_threshold: トリガー率のしきい値。
        model: 使用するモデル名。
    """

    num_workers: int
    timeout: int
    project_root: Path
    runs_per_query: int
    trigger_threshold: float
    model: str | None


@dataclass(frozen=True)
class SingleQueryConfig:
    """単一クエリ実行に必要な設定をまとめたパラメータオブジェクト。

    Attributes:
        timeout: クエリごとのタイムアウト秒数。
        project_root: プロジェクトルートパス（文字列）。
        model: 使用するモデル名（None で既定値を使用）。
    """

    timeout: int
    project_root: str
    model: str | None
