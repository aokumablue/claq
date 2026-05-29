# フック

フックは、ツール実行の前後に起動されるイベント駆動の自動化機能です。コード品質を高め、ミスを早期に見つけ、反復的なチェックを自動化します。

## フックの仕組み

```plain
ユーザーリクエスト → Claude がツールを選択 → PreToolUse フック実行 → ツール実行 → PostToolUse フック実行
```

- **PreToolUse** フックはツール実行前に実行されます。**ブロック**（終了コード 2）または **警告**（ブロックなしで stderr）できます。
- **PostToolUse** フックはツール完了後に実行されます。出力を分析できますがブロックはできません。
- **Stop** フックは各 Claude レスポンス後に実行されます。
- **SessionStart/SessionEnd** フックはセッションライフサイクルの境界で実行されます。
- **PreCompact** フックはコンテキスト圧縮前に実行され、状態の保存に便利です。

### ライフサイクルフック

| フック | イベント | 実行内容 |
|------|-------|-------------|
| **セッション開始** | `SessionStart` | 前のコンテキストをロードし、パッケージマネージャーを検出 |
| **プリコンパクト** | `PreCompact` | コンテキスト圧縮前に状態を保存 |
| **セッションサマリー** | `Stop` | トランスクリプトパスが利用可能な場合にセッション状態を永続化 |
| **パターン抽出** | `Stop` | 抽出可能なパターンのセッション評価（継続的学習） |
| **コストトラッカー** | `Stop` | 軽量の実行コストテレメトリーマーカーを発行 |
| **デスクトップ通知** | `Stop` | タスクサマリーの macOS デスクトップ通知を送信（standard+） |
| **セッション終了マーカー** | `SessionEnd` | ライフサイクルマーカーとクリーンアップログ |

`SessionStart` の `session_install` は `.venv` symlink の検査・修復に加えて、`~/.devgear/plugin_installed_version` と `plugin.json` の version が異なるときに `install.sh` を自動実行します。ONNX モデルビルド（5〜10 分）はバックグラウンドで走るため、SessionStart 自体はブロックしません。ビルド中は mem 機能が一時的に利用不可となり、stderr に `onnx building...` が出ます。完了後は自動的に復旧します。ビルドログは `~/.devgear/logs/modelbuild.log` を参照してください。

## フックのカスタマイズ

### フックの無効化

`hooks.json` のフックエントリを削除またはコメントアウトします。プラグインとしてインストールされている場合は、`~/.claude/settings.json` で上書きします:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write",
        "hooks": [],
        "description": "オーバーライド: すべての .md ファイル作成を許可"
      }
    ]
  }
}
```

### ランタイムフック制御（推奨）

フックは常に strict として動作します。`hooks.json` を編集せずに個別フックを止めたい場合は、`DEVGEAR_DISABLED_HOOKS` を使います。

```bash
# カンマ区切りでフック ID を指定
export DEVGEAR_DISABLED_HOOKS="post:edit:typecheck"
```

### quality-gate のコマンド定義

`settings.json` はこのプラグイン全体の設定ファイルです。`hooks` / `skills` / `commands` に分けて、どの機能の設定かを一目で分かるようにしています。

`hooks.quality-gate` は `post-edit` で `extensions` と `tool_names` を使って対象を絞り、`bash` に配列でコマンドを書きます。`tool_names` は空配列なら絞り込みなしです。同梱サンプルは `.py` 編集時に `ruff check plugins/devgear/src tests` を 1 本実行するだけです。

この quality-gate は、プロジェクト側の言語ランタイムに依存せず、検出できた主要言語ごとのプリセットを使います。未対応言語や、対応コマンドが PATH にない場合は安全にスキップされます。

```json
{
  "hooks": {
    "quality-gate": {
      "post-edit": {
        "extensions": [".py"],
        "tool_names": [],
        "bash": [["ruff", "check", "plugins/devgear/src", "tests"]]
      }
    }
  }
}
```

`commands` セクションも同じ考え方で、`commands.<name>.tools.bash` に argv 配列を書きます。複数の解析コマンドを並べる場合は、`bash` 配列に 1 コマンドずつ追加します。

`DEVGEAR_QUALITY_GATE_CONFIG` で明示的に指した設定だけを追加読み込みします。必要なら `extensions` や `tool_names` で絞り込み、`bash` に配列でコマンドを並べます。

### 独自フックの作成

このリポジトリのフック本体は、`devgear.hooks.hook_common` のヘルパーを使う Python モジュールが基本です。`stdin` で受け取った JSON を見て、警告は `stderr`、通常時は元の入力を `stdout` に返します。

**基本構造:**

```python
#!/usr/bin/env python3

from devgear.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr, write_stdout


def main() -> int:
    raw = read_raw_stdin()
    data = parse_json_object(raw)

    if data:
        tool_input = data.get("tool_input") or {}
        file_path = str(tool_input.get("file_path") or "")
        command = str(tool_input.get("command") or "")

        if file_path and file_path.endswith(("TODO.md", "WIP.txt")):
            write_stderr("[Hook] WARNING: ad-hoc documentation filename detected\n")

        if "--no-verify" in command:
            write_stderr("[Hook] BLOCKED: git hook bypass flags are not allowed\n")
            return 2

    write_stdout(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**終了コード:**

- `0` — 成功（実行を継続）
- `2` — ツール呼び出しをブロック（PreToolUse のみ）
- その他の非ゼロ — エラー（ログに記録されるがブロックしない）

### よく使う入力キー

```python
payload = {
    "tool_name": "Bash",
    "tool_input": {"command": "git commit --no-verify"},
}
```

- `tool_name`: `Bash` / `Write` / `Edit`
- `tool_input.file_path`: ファイル系ツールの対象パス
- `tool_input.command`: Bash のコマンド

`SessionStart` だけは、追加コンテキストを JSON で返します。

```python
import json

print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "...",
            }
        }
    ),
    end="",
)
```

### 非同期フック

メインフローをブロックすべきでないフック（例: バックグラウンド分析）の場合:

```json
{
  "type": "command",
  "command": "python3 -m devgear.hooks.session_end_marker",
  "async": true,
  "timeout": 30
}
```

非同期フックはバックグラウンドで実行されます。ツール実行を妨げることはできません。
