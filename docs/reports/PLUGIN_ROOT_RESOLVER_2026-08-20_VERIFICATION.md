# bluecore plugin-root resolver runtime audit

実施日: 2026-08-20<br>
対象: bluecore v0.9.35 source および
`~/.copilot2/installed-plugins/bluecore/bluecore`<br>
判定: **BLOCK -- 正常系は動作するが、roots pointer を shell code として
実行し、host を正確に識別できない fallback を持つ。**

## 1. 対象と方法

v0.9.35 は、agents / commands / skills から各ベンダのインストールパスを
取り除くため、`~/.bluecore/env.sh` を共通 bootstrap として追加した。
`launcher.py` は hook 起動時に `~/.bluecore/roots/<ppid>.sh` と
`latest.sh` へ plugin root を記録し、`env.sh` は次の順で root を見つける。

1. `PATH` の各エントリの親にある `runtime/bluecore-helpers.sh`
2. `~/.bluecore/roots/$PPID.sh`
3. `~/.bluecore/roots/latest.sh`

source と installed `.copilot2` artifact が v0.9.35 で一致することを
確認した。installed artifact 上で `validate_skills`、`validate_commands`、
`validate_agents`、`validate_hooks`、および `ruff check src` はすべて成功した。

隔離した temporary HOME で、shell resolver と `write_env_pointer()` を直接
実行した。実ユーザーの `.bluecore` state や plugin source は変更していない。
加えて、実際の `bluecore:harness-tuner` agent に plugin の bin を含まない
`PATH=/opt/homebrew/bin:/usr/bin:/bin` で bootstrap させ、
`harness_audit` JSON（47/65）を得た。従って、**正常系の helper 解決は動作する**。

## 2. 検出事項

### R-01: roots pointer を source すると任意の shell code が実行される

対象: `plugins/bluecore/runtime/env-template.sh:39-46`

resolver は roots file をデータではなく shell script として読み込む。

```sh
. "$HOME/.bluecore/roots/$PPID.sh"
. "$HOME/.bluecore/roots/latest.sh"
```

隔離 HOME に次の `latest.sh` を置き、PPID pointer を置かずに
`env.sh` を source した。

```sh
printf '%s\n' ROOT_FILE_EXECUTED >&2
BLUECORE_ROOT="/path/to/plugin"
```

stderr に `ROOT_FILE_EXECUTED` が出力され、source は exit 0 で終了した。
つまり pointer file に記録された root 以外の任意 command が、将来の agent
Bash invocation で実行される。

`~/.bluecore` は通常 0700 であり別 OS user からの書込みは防ぐが、agent が
処理した外部入力、同一 user で動く別 plugin、または state の破損からの
cross-session persistence を防ぐ境界にはならない。root resolver は
「どの plugin runtime を実行するか」を決める前に shell を実行してはならない。

**分類: CRITICAL。**

### R-02: PATH の任意 helper を検証せず source する

対象: `plugins/bluecore/runtime/env-template.sh:28-37,55`

PATH の全要素について、親 directory に
`runtime/bluecore-helpers.sh` があるだけで候補を採用し、55行目で source
する。所有者、mode、manifest、bluecore version、launcher が記録した root
であることを検証しない。

```sh
PATH=/untrusted/bin:/usr/bin:/bin
. "$HOME/.bluecore/env.sh"
```

`/untrusted/../runtime/bluecore-helpers.sh` が存在すれば、それが最優先で
実行される。これは root pointer を使う目的（現在の host が選んだ plugin
runtime の特定）とも矛盾する。

**分類: CRITICAL。**

### R-03: plugin root の shell 直列化が安全でない

対象: `plugins/bluecore/src/bluecore/lib/env_pointer.py:80-82`

writer は root を二重引用符付き shell assignment としてそのまま書く。

```python
root_line = f'BLUECORE_ROOT="{plugin_root}"\n'
```

隔離環境で `$variable` を含む plugin root を `write_env_pointer()` に渡すと、
生成された file は次のとおりだった。

```sh
BLUECORE_ROOT="/tmp/.../root$variable"
```

この pointer を source すると `$variable` が展開され、実在する root を失って
resolver は exit 127 になった。空白以外にも `"`, backtick, `$()`、改行を含む
有効な filesystem path が shell 構文や command substitution を作る。特に
`$()` を含む root は source 時に任意 command execution へつながる。

**分類: CRITICAL。**

### R-04: `BLUECORE_HOME` の writer / resolver 契約が一致しない

対象: `plugins/bluecore/src/bluecore/lib/core_utils.py:22-31`、
`plugins/bluecore/src/bluecore/lib/env_pointer.py:75-86`、
`plugins/bluecore/runtime/env-template.sh:39-45`

writer は `get_home_dir()` に従い `BLUECORE_HOME` を HOME より優先するため、
`$BLUECORE_HOME/.bluecore/` へ pointer を書く。一方 resolver は常に
`$HOME/.bluecore/` を読む。

再現:

```sh
BLUECORE_HOME=/tmp/custom HOME=/tmp/home \
  PYTHONPATH=src python3 -c \
  'from pathlib import Path; from bluecore.lib.env_pointer import write_env_pointer;
   write_env_pointer(Path("/path/to/plugin"))'

BLUECORE_HOME=/tmp/custom HOME=/tmp/home PATH=/usr/bin:/bin \
  sh -c '. "$HOME/.bluecore/env.sh"'
```

writer は `/tmp/custom/.bluecore/env.sh` を作るが、resolver は
`/tmp/home/.bluecore/env.sh` を探して exit 1 になった。

**分類: HIGH。**

### R-05: `latest.sh` fallback は host / agent の識別を保証しない

対象: `plugins/bluecore/runtime/env-template.sh:44-47`、
`plugins/bluecore/src/bluecore/lib/env_pointer.py:81-82`

PPID pointer が存在しない場合、global な `latest.sh` を使用する。
隔離環境で PPID pointer を置かず、valid root のみを書いた `latest.sh` を
用意すると resolver は exit 0 で helper を読み込んだ。これは availability
には有効だが、同時に動く Copilot / Claude / Grok、異なる plugin version、
中間 shell、PID 再利用を区別しない。

ADR-0008 も `PATH` 不一致、PPID 不一致、複数 host 稼働時に意図しない install
を source するリスクを記載している。今回の要件である「実行中の各社 agent を
判断して対応 root を使う」ことは、この fallback では達成できない。

7日間保存する PID file は PID の生存・開始時刻を確認しないため、PID 再利用時も
誤 root を正規 `$PPID.sh` として採用しうる。

**分類: HIGH。**

### R-06: pointer 更新は非原子的で、失敗が観測不能

対象: `plugins/bluecore/src/bluecore/lib/env_pointer.py:57-60,75-86`

`write_text()` は truncate と write の間に reader が partial file を source
できる。並列 hook、plugin update、別 host の起動が重なると `env.sh`、PPID
pointer、`latest.sh` の内容が空または途中まで見える。

さらに launcher は全 hook 起動時に writer を呼ぶが、writer は全ての
`OSError` を無記録で捨てる。失敗後も古い pointer を source するか、原因不明の
exit 127 になる。

**分類: HIGH（integrity / availability）。**

## 3. 正常に確認できた事項

| 項目 | 結果 |
|---|---|
| installed v0.9.35 と source の resolver files | 一致 |
| `$PPID.sh` が `latest.sh` より優先される | 隔離 shell で確認 |
| `latest.sh` fallback | PPID pointer 不在時に helper を解決することを確認 |
| clean shell bootstrap | plugin bin を PATH から外し、Python 3.14 を残す条件で実 agent が 47/65 JSON を取得 |
| static validation | 4 validators と Ruff が PASS |

これらは正常系の可用性を示すだけであり、R-01〜R-06 の trust boundary /
correctness の問題を解消しない。

## 4. 修正方針

### Phase 1: roots を shell script ではなく data file にする

1. `roots/<host-pid>` と `latest` の `.sh` suffix を廃止し、例えば
   `roots/<host-pid>.root` という data format にする。
2. 内容は UTF-8 の absolute path 1行だけとする。`NUL`、CR、LF を含む path は
   writer が拒否する。shell quoting は不要であり、`$`、空白、`"`, backtick は
   data としてそのまま許可できる。
3. template は `.` / `source` を一切使わず、
   `IFS= read -r _bluecore_env_root < "$pointer"` で1行を取得する。余分な行、
   read error、空値は fail-closed とする。
4. `BLUECORE_ROOT` は内部 local variable として扱い、pointer 読込み前の環境値を
   継承しない。

受入条件:

- pointer に `printf ...` や `$(...)` を書いても実行されず、resolver は明示的に
  exit 127 となる。
- root path に空白、`$`、`"`, backtick を含めても、その path が実在するなら
  helper を解決できる。
- 改行・NUL を含む root は writer が拒否し、既存の有効 pointer を壊さない。

### Phase 2: trusted root のみを選択する

1. PATH 探索を削除する。PATH は executable lookup のための入力であり、runtime
  helper の trust anchor にしてはならない。
2. resolver は `$PPID` から始め、必要なら process parent chain を上限付きで
   たどり、現存する ancestor PID の pointer だけを候補にする。これにより中間
   shell を許容しつつ global fallback を不要にする。
3. pointer record には writer が観測した host PID と process start identity を
   保存し、resolver が実際の ancestor process と照合する。実装対象 OS ごとの差は
   共通 Python helper に隔離する。
4. 照合できない場合は `latest` に落ちず、`bluecore runtime root is unavailable
   for this host` を stderr に出して exit 127 とする。次の hook 起動で
   pointer が作られることを案内する。

受入条件:

- 同時に異なる root を記録した2 host の child shell はそれぞれ自分の ancestor
  pointer を選ぶ。
- PPID / ancestor pointer が無い shell は別 host の latest root を使用せず
  exit 127 となる。
- stale PID record と PID reuse の fixture は reject される。

### Phase 3: state directory と更新を安全にする

1. `~/.bluecore`、`roots`、pointer file は `lstat` で regular file /
   expected owner / non-group-writable / non-world-writable を検証する。symlink、
   ownership / mode 不正、unexpected type は fail-closed とし source しない。
2. writer は trusted directory descriptor を開き、`O_NOFOLLOW` を使う。
   `<name>.tmp.<pid>.<random>` に mode 0600 で書き、`fsync` 後に同一 directory
   の `os.replace()` で公開する。`env.sh`、host pointer、latest metadata は
   reader に partial content を見せない。
3. writer failure は hook の本処理を止めない契約を維持してよいが、stderr または
   plugin log へ「pointer update failed」を記録する。resolver は古い /
   検証不能 state を採用せず exit 127 にする。
4. `get_bluecore_dir()` と template は同じ state root resolver を使用する。
   現行契約に従うなら `BLUECORE_HOME` は HOME directory override として
   `$BLUECORE_HOME/.bluecore` を優先し、未設定時のみ `$HOME/.bluecore` を使う。

受入条件:

- concurrent writers / reader の stress test で、resolver は完全な旧recordか
  完全な新recordだけを読み、syntax error や partial root を見ない。
- symlink、group-writable pointer、別 owner pointer を reject し、shell codeを
  実行しない。
- `BLUECORE_HOME` を設定した clean shell が writer と同じ state directory を
  読む。
- writer I/O failure は記録され、hook の本来の target invocation は従来どおり
  続行する。

## 5. テストと release gate

source tree に次の unit / isolated runtime tests を追加する。

| テスト | 検証内容 |
|---|---|
| data pointer parsing | 1行、空、複数行、NUL、special characters |
| no shell execution | malicious pointer / helper fixture が実行されない |
| host selection | direct PPID、intermediate shell ancestor、並列 host、PPID不一致 |
| stale records | PID reuse / start identity mismatch の拒否 |
| state security | symlink、mode、owner、regular file 検証 |
| update atomicity | 並列 writer / reader でpartial readがない |
| state-root override | `BLUECORE_HOME` と HOME fallback の一貫性 |
| real helper bootstrap | clean shell から正しい host root を使い `bluecore_run` を実行 |

`pytest --cov=bluecore`、Ruff、4 validators に加え、上記 isolated runtime test
を次回release gateにする。配布artifactにtestsを含めない方針は ADR-0006 と
両立する。テストは source tree で実行する。

## 6. 結論

v0.9.35 の roots resolver は M-02 の正常系（helper 未定義による exit 127）を
解消した。しかし、host root を選ぶ前に PATH / roots state から任意 shell code を
source し、PPID不一致時に別hostの global latest root を選べるため、
「現在実行中の agent / host を判別して対応 runtime を使う」実装としては不十分で
ある。

Phase 1〜3 と対応テストを完了するまで、この機構を security / host-isolation
boundary として扱わず、release 判定は **BLOCK** とする。
