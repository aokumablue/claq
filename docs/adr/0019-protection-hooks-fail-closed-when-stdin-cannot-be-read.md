# ADR-0019: 保護フックは stdin を「読めなかった」場合に fail-closed する（「入力が無い」場合は従来どおり素通り）

**日付**: 2026-09-04  **ステータス**: accepted

## コンテキスト

Windows 実機検証（`docs/reports/ple4-release-verify-windows-2026-09-03.md`
の P1-004）で、実体の Python 3.14.7 から `launcher.py` を直接起動し、
リダイレクトした stdin へ UTF-8 JSON を渡した結果が次のとおりだった。

| Payload | 実行結果 |
|---|---|
| `{"tool_name":"Bash","tool_input":{"command":"git status"}}` | exit 0 |
| `{"tool_name":"Bash","tool_input":{"command":"git commit --no-verify -m x"}}` | exit 0 |

後者は `block_no_verify` が deny すべき payload である。allow と deny が
同一の exit 0 になったということは、フックが payload を **1 バイトも読んで
いない**ことを意味する。

原因は `hook_common` の stdin 読み取りガードにあった。`_stdin_ready()` が
`select.select([sys.stdin], [], [], timeout)` で「最初のバイトが届くか」を
判定していたが、Windows の `select` は **socket にしか使えず**、通常の
パイプに対しては `OSError` を投げる。その例外を

```python
except (OSError, ValueError, AttributeError):
    return False        # ← 「入力なし」として扱う
```

と握り潰していたため、読み取りは一度も行われず、`main()` の
`if not raw: return 0`（空入力は非ブロッキングで許可、F-01 の契約）へ
そのまま落ちていた。すなわち**保護フックが構造的に無効**だった。

ADR-0001 は「検査が完了できないときは fail-open」と定めている。この規定を
そのまま当てはめると今回も fail-open が正当に見えるが、ADR-0001 が扱って
いるのは「**受け取ったコマンドを解析し切れない**」場合であり、
「**そもそもコマンドを受け取れていない**」場合ではない。後者を同じ扱いに
すると、入力経路が壊れているだけで保護が全面的に無効化され、しかもその事実が
exit 0 として成功と report される。

## 決定

**stdin の状態を 2 つに分け、保護フックは後者だけを fail-closed にする。**

1. **payload が無い**（`sys.stdin is None` / TTY 接続 / 即 EOF で 0 バイト）
   → 従来どおり空文字列として扱い、保護フックは exit 0 で素通りさせる
   （F-01 の契約を維持）。
2. **payload を読めなかった**（読み取りが例外化した／最初のバイトが
   `STDIN_FIRST_BYTE_TIMEOUT` 以内に届かなかった）
   → `StdinUnavailableError` を送出し、4 つの保護フック
   （`block_no_verify` / `config_protection` / `bash_config_protection` /
   `pre_bash_commit_quality`）は deny（exit 2 + `permissionDecision: deny`）
   に倒す。

あわせて、readiness 判定から `select` を撤去する。ブロッキング read を
daemon スレッドへ隔離し、本スレッドは `queue.Queue.get(timeout=...)` で
待つ。これは 3 プラットフォームで同一の 1 本のロジックであり、OS 判定も
プラットフォーム分岐も持たない（Windows 専用のフォールバック実装ではない）。

非保護経路（`launcher --bg` の stdin 中継、`mem.cli` の payload 読み取り）は
`read_raw_stdin()` を使い、例外を空文字列へ正規化したまま据え置く。これらは
「読めなかった」ときに採れる別の行動が無い（どちらでも記録すべき材料が無い）
ため、区別しても意味がない。

## 根拠

- **1 と 2 は証拠の質が違う。** 「即 EOF」は「ホストが payload を渡して
  いない」と観測上区別できず、payload を渡さないホストで deny に倒すと
  全ツール呼び出しが拒否され、復旧手段（Bash）ごと塞がれる。一方
  「読み取りが例外化した」「最初のバイトが届かない」は、**入力経路が
  存在するのに取り出せなかった**という積極的な証拠であり、判定不能を
  許可へ倒す理由にならない。
- **ADR-0002 の優先順位と整合する。** シェル系フックは「誤検出 > 誤通過」を
  採っている。読めなかった payload を通すのは誤通過そのものである。
- **ADR-0001 と矛盾しない。** ADR-0001 の fail-open は「受け取った入力を
  解析できないとき」の規定であり、本 ADR は「入力を受け取れなかったとき」を
  扱う。両者は排他的な事象で、`parse_json_object` が `None` を返す経路
  （＝受け取ったが読めない JSON）の扱いは従来どおり各フックの判断に委ねる。
- **`select` の撤去自体はプラットフォーム非依存の改善。** 旧実装は
  「readiness を OS に問い合わせる」ために POSIX 固有 API を使っていたが、
  必要なのは「タイムアウト付きで読む」ことだけである。スレッド + キューは
  同じ意味論を標準ライブラリだけで、OS 分岐なしに実現する。

## 検討した代替案

### 代替案 1: 空入力も含めてすべて fail-closed にする

Windows 検証レポートの受入条件表は「空入力、壊れた JSON、Windows pipe
読取例外を許可側へ倒さない」と書いており、空入力も deny を求めている。

- 長所: 受入条件表をそのまま満たす。
- 短所: payload を渡さないホストが 1 つでもあれば、そのホストでは全
  Bash/Edit が拒否され、設定を直す手段ごとセッション内から失われる。
  この失敗モードは実測で確認されておらず、被害はフックの無効化より大きい。
- 却下理由: 「入力が無い」ことは、ホストの仕様なのか経路の故障なのかを
  観測で区別できない。区別できない事象に対して復旧不能な副作用を持つ側へ
  倒すのは、`launcher.py` の未対応 Python 時 fail-open（CLAUDE.md
  「ランタイム前提」）と同じ理由で採らない。

### 代替案 2: Windows だけ overlapped I/O（`_winapi` / ctypes）で読む

レポートの修正案が挙げていた方式。

- 長所: OS ネイティブの非同期 I/O を使える。
- 短所: Windows 専用の分岐が増え、macOS/Linux では 1 行も実行されない
  （カバレッジ 100% の要件に対しても不利）。得られるものは「タイムアウト
  付きの読み取り」だけで、それはスレッド + キューで OS 非依存に実現できる。
- 却下理由: ユーザー要件「なるべくフォールバックのような実装にはせず、
  3 OS 共通のロジックとする」に反する。共通で書ける以上、共通で書く。

### 代替案 3: `select` を残し、例外時だけ別経路にする

- 長所: 変更が小さい。
- 短所: POSIX 経路と Windows 経路で「読めたかどうか」の判定軸が 2 本に
  割れ、片方だけ強化される非対称（`INPUT_CONTAINER_KEYS` で実際に起きた
  失敗と同型）を再生産する。
- 却下理由: 判定軸は 1 本に保つ。

## 影響

- `hook_common.read_raw_stdin_with_truncation()` は
  `StdinUnavailableError` を送出しうる（従来は例外を送出しない契約だった）。
  保護フック 4 つはこれを捕捉し、`stdin_unreadable_message()` の共通文面で
  deny する。
- `hook_common._stdin_ready()` は `_stdin_is_absent()` へ置き換えた。
  TTY / `sys.stdin is None` の判定だけを担い、readiness は判定しない。
- `isatty()` が例外化する stdin は「無い」と決めつけず、読み取り側の判断へ
  委ねる（判定不能を素通りへ倒さないため）。
- `STDIN_FIRST_BYTE_TIMEOUT` を 1.0 秒から 2.0 秒へ広げた。hooks.json の
  保護エントリ timeout も 5 秒から 15 秒へ広げてあり、Windows の wrapper +
  インタプリタ起動の遅さで host 側 timeout（＝ホストによっては
  「操作を通す」fail-open）へ入りにくくする。
