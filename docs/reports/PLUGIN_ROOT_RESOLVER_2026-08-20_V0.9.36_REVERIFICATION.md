# bluecore v0.9.36 plugin-root resolver reverification

実施日: 2026-08-20<br>
対象: `~/.copilot2/installed-plugins/bluecore/bluecore` v0.9.36<br>
前提報告: `PLUGIN_ROOT_RESOLVER_2026-08-20_VERIFICATION.md`<br>
判定: **BLOCK -- R-01〜R-04 の主要修正と正常系は確認できたが、pointer が
指す helper の信頼性、host 未一致時の fallback、writer / GC 競合が残る。**

## 1. 対象と方法

本報告は source checkout ではなく、再インストール後に実際の Copilot runtime が
読む installed artifact を対象とする。

```text
~/.copilot2/installed-plugins/bluecore/bluecore
manifest version: 0.9.36
```

現時点の source checkout は v0.9.35 のままであり、resolver 実装は installed
artifact と一致しない。このため、以下の動作検証はすべて installed artifact の
`runtime/env-template.sh` および `src/bluecore/lib/env_pointer.py` に
`PYTHONPATH` を通して行った。

実施した検証:

- `validate_skills`、`validate_commands`、`validate_agents`、
  `validate_hooks`、`ruff check src`
- 隔離 HOME での pointer data parsing、special-character root、HOME 契約、
  conflict / fallback、fake PATH、fake helper、GC を直接実行
- 実 `bluecore:harness-tuner` agent に、plugin bin を含まない
  `PATH=/opt/homebrew/bin:/usr/bin:/bin` を渡した bootstrap / re-audit

静的 validators と Ruff はすべて PASS した。実agentは
`. "$HOME/.bluecore/env.sh"` 後に `bluecore_run` を解決し、
`harness_audit` JSON の `overall_score/max_score: 47/65` を取得した。
exit 127 は発生しなかった（終了コード 1 は監査スコア未満点に対する通常結果）。

## 2. 前回指摘の再検証

| ID | v0.9.36 判定 | 根拠 |
|---|---|---|
| R-01 | **解消（pointer 内容の直接実行）** | pointer は `source` されず、`IFS= read -r` で1行のdataとして取得する。shell code を置いた隔離 pointer はmarkerを出力せずexit 127になった。 |
| R-02 | **解消（PATH 探索）** | templateからPATH探索を削除。PATH上のfake helperは採用されず、recorded rootのhelperを読み込んだ。 |
| R-03 | **解消** | writerはshell assignmentをやめ、raw path 1行を書き出す。`$`を含むroot pathを指すpointerでhelperを解決できた。 |
| R-04 | **解消** | writer / resolverとも `$HOME/.bluecore` を固定住所として使う。`BLUECORE_HOME`を別pathにしてもwriter・resolverが同じHOME stateを使うことを確認した。 |
| R-05 | **部分解消** | `latest.sh`は廃止され、ancestor pointerを優先する。ただしancestor不一致時のall-agree / recent-candidate fallbackが別host rootを採用しうる。 |
| R-06 | **部分解消** | `fsync` + `os.replace`で公開pointerのpartial readは防ぐ。ただしGCが別writerのactive temporary fileを削除できる。 |

## 3. 残存検出事項

### H-01: pointer が指す任意 root の helper を無検証で source する

対象:
`runtime/env-template.sh:52-63,125-131`

pointer自身はdataとして安全に読むようになった。しかしresolverはpointerのpathを
absolute path / expected owner / known installation / manifest / runtime version と
照合せず、存在する `runtime/bluecore-helpers.sh` をsourceする。

```sh
if [ -z "$_bluecore_env_root" ] || \
   [ ! -f "$_bluecore_env_root/runtime/bluecore-helpers.sh" ]; then
  # ...
fi

. "$_bluecore_env_root/runtime/bluecore-helpers.sh"
```

#### 再現

以下は隔離 HOME だけを変更し、fake helper は `bluecore_run` 関数がmarkerを出す
だけの無害なfixtureである。

```sh
I="$HOME/.copilot2/installed-plugins/bluecore/bluecore"
D="$(mktemp -d)"
mkdir -p "$D/home/.bluecore/roots" "$D/fake/runtime"
cp "$I/runtime/env-template.sh" "$D/home/.bluecore/env.sh"
printf '%s\n' 'bluecore_run() { printf POINTER_HELPER_EXECUTED; }' \
  > "$D/fake/runtime/bluecore-helpers.sh"
printf '%s\n' "$D/fake" > "$D/home/.bluecore/roots/999999"

HOME="$D/home" PATH=/opt/homebrew/bin:/usr/bin:/bin \
  bash --noprofile --norc -c \
  '. "$HOME/.bluecore/env.sh"; bluecore_run'
```

出力:

```text
POINTER_HELPER_EXECUTED
```

つまりpointer fileをshellとして実行しなくても、pointerを変更できる経路が
あるだけで、次のbootstrap時に任意helperが実行される。`~/.bluecore`が0700 /
pointerが0600であることは別OS userを防ぐが、同一userで稼働するagent /
plugin由来のcross-session persistenceを防ぐ保証にはならない。

**分類: HIGH。**

#### 修正案

1. pointer recordを単なるroot pathから、launcherが発行する署名付きまたは
   capability付きmetadataへ変更する。少なくとも `root`、manifest version、
   helper file hash、writer PID / process identityを記録する。
2. resolverはrecordのformat、regular file、owner、modeを検証し、rootが
   absolute / non-symlink directoryであること、manifestのplugin idが
   `bluecore`であること、helper hashがrecordと一致することを確認する。
3. root / helper / metadataのどれかが検証不能ならsourceせずexit 127にする。
   bootstrapがagentに実行される以上、security-sensitiveなhelper sourceに
   fail-openを使わない。
4. writerはstate directoryを安全に開く（dirfd、`O_NOFOLLOW`、expected UID /
   mode）ことで、state file差替えのTOCTOUも防ぐ。

受入条件:

- fake root、fake manifest、改竄helper、symlink root、group/world writable
  recordのいずれもhelperを実行せずexit 127になる。
- 正規installed artifactのrecordだけが、hash / manifest照合後に
  `bluecore_run`を定義できる。

### H-02: ancestor不一致時のrecent fallbackが別host rootを採用する

対象:
`runtime/env-template.sh:80-123`

`latest.sh`は除去されたが、ancestor pointerが無い場合に全recordをscanする。
異なるrootがあれば、直近5分のrecordだけで再度合意を取り、1つのrecent rootが
残れば採用する。

```sh
_bluecore_scan_agreement "$_BLUECORE_RECENT_MINUTES"
if [ -n "$_bluecore_scan_root" ] && [ "$_bluecore_scan_conflict" -eq 0 ]; then
  _bluecore_env_root="$_bluecore_scan_root"
fi
```

これは「今回のshellがどのhostのchildか」を証明しない。別host Aのold pointerと
別host Bのrecent pointerが残る状態で、どちらのancestorにも属さないshellは
Bを選ぶ。

#### 再現

```sh
I="$HOME/.copilot2/installed-plugins/bluecore/bluecore"
D="$(mktemp -d)"
mkdir -p "$D/home/.bluecore/roots" "$D/a/runtime" "$D/b/runtime"
cp "$I/runtime/env-template.sh" "$D/home/.bluecore/env.sh"
printf '%s\n' 'bluecore_run() { printf ROOT_A; }' \
  > "$D/a/runtime/bluecore-helpers.sh"
printf '%s\n' 'bluecore_run() { printf ROOT_B; }' \
  > "$D/b/runtime/bluecore-helpers.sh"
printf '%s\n' "$D/a" > "$D/home/.bluecore/roots/999998"
printf '%s\n' "$D/b" > "$D/home/.bluecore/roots/999999"
touch -t 202608201200 "$D/home/.bluecore/roots/999998"

HOME="$D/home" PATH=/opt/homebrew/bin:/usr/bin:/bin \
  bash --noprofile --norc -c \
  '. "$HOME/.bluecore/env.sh"; bluecore_run'
```

出力:

```text
ROOT_B
```

**分類: HIGH。** これは「各社agentを判別して対応runtimeを選ぶ」という本機構の
要件に反する。異version hostを同時利用する場合、Bのruntimeが意図しないshellへ
注入される。

#### 修正案

1. global agreement / recent-candidate fallbackを廃止する。
2. `$PPID`からancestor chainをたどり、recorded process identityと一致する
   pointerだけを採用する。最大探索深度はhost wrapperの実測に基づいて定める。
3. recordにはPIDだけでなくprocess start time / platform固有process identityを
   書く。PID reuseやold recordが一致したように見えるケースをrejectする。
4. ancestorに検証可能なrecordが無い場合は、別hostのrootを推測せずexit 127にする。
   次のhost hook起動でrecordを更新するよう明示的に案内する。

受入条件:

- 異なる2 host rootが存在し、ancestorに一致しないshellはexit 127になる。
- direct parentとintermediate shell経由の正しいancestor pointerは採用される。
- PID reuse、start identity不一致、expired recordはrejectされる。

### M-01: GCが別writerのtemporary fileを削除できる

対象:
`src/bluecore/lib/env_pointer.py:197-204,319-388`

writerは`<target>.tmp.<pid>`を作ってから`os.replace()`する。GCはnumeric filename
以外を無条件で削除する。

```python
tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
# ...
os.replace(tmp_path, path)
```

```python
if not entry.name.isdigit():
    entry.unlink()
```

別writerのGCが、まだ`os.replace()`していないwriter Aの
`env.sh.tmp.<pid>`または`<pid>.tmp.<pid>`を削除できる。Aはその後の
`os.replace()`で`FileNotFoundError`になり、launcherはwrite failure warningを
出して古いstateを残す。

#### 再現

実writerの一時file名と同じ形式のfixtureを作り、GCを直接呼ぶ。

```sh
I="$HOME/.copilot2/installed-plugins/bluecore/bluecore"
D="$(mktemp -d)"
mkdir "$D/roots"
printf 'x\n' > "$D/roots/env.sh.tmp.12345"

PYTHONPATH="$I/src" python3 -c \
  "from pathlib import Path
from bluecore.lib.env_pointer import _gc_roots
_gc_roots(Path('$D/roots'), keep_pid=None)"

test -e "$D/roots/env.sh.tmp.12345" && echo preserved || echo deleted
```

出力:

```text
deleted
```

32並行writerの通常stressではfailure warning 0件でvalid `env.sh`を生成した。
ただしこれは競合が発生しなかったことを示すだけで、上記の削除可能なrace windowを
閉じない。

**分類: MEDIUM（availability / reliability）。**

#### 修正案

1. GCがactive temporary fileを削除しないよう、temp namingを明示的に識別し、
   fresh tempはGC対象外にする。より確実にはwriterとGCの間にlockを置く。
2. temp fileは`O_CREAT | O_EXCL | O_NOFOLLOW`、cryptographically random suffix、
   mode 0600で作る。予測可能なPIDだけの名前は使用しない。
3. `fsync`後のrenameに加え、directory fsyncを行う。GCも同じtrusted dirfd内で
   実行する。
4. race testをdeterministicにする。writer Aをtemp生成後にbarrierで停止し、
   writer BのGCを実行してもAのtempが残り、Aのreplaceが成功することを確認する。

受入条件:

- 上記barrier raceでAの`os.replace()`が成功する。
- 多数並行writer / readerのstressでwarning 0件、partial env / pointer 0件。
- stale tempだけが安全に回収される。

## 4. release assurance: source / installed artifactの乖離

このrepositoryのHEADはv0.9.35で、`runtime/env-template.sh`と
`env_pointer.py`は前回R-01〜R-06を含む旧実装のままである。一方、実行artifactは
v0.9.36で、本報告の修正が入っている。

この状態では:

- source treeを読んで実装レビューしても、実行runtimeをレビューできない。
- source-level test / CIがv0.9.36のreproductionを持てない。
- 次のpackage / installでv0.9.35の旧resolverへ戻る危険がある。

v0.9.36の実装、関連ADR、isolated regression testをsource repositoryへ反映し、
同一commitからartifactを生成・version照合することをrelease gateにする。

## 5. 結論

v0.9.36はpointerのshell code実行、PATH helper探索、shell直列化、
`BLUECORE_HOME`不一致を解消し、M-02の実agent bootstrapも正常に動作した。

しかしH-01のuntrusted root helper sourceとH-02のhost未一致fallbackは、
agent / host isolationの根本要件を満たさない。またM-01のwriter / GC競合と
source / installed乖離により、修正の信頼性・再現性も不足する。

H-01、H-02、M-01、およびsource同期と対応回帰テストが完了するまで、plugin-root
resolverのrelease判定は **BLOCK** とする。
