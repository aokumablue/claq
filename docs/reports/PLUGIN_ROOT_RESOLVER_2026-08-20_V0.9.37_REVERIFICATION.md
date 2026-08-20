# bluecore v0.9.37 plugin-root resolver reverification

実施日: 2026-08-20<br>
対象: `~/.copilot2/installed-plugins/bluecore/bluecore` v0.9.37<br>
前提報告: `PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.36_REVERIFICATION.md`<br>
判定: **BLOCK -- host選択とGCの前回欠陥は改善されたが、検証済みpointerが
指す任意helperを実行できる。source / artifact乖離もreleaseをblockする。**

## 1. 対象と方法

再インストール後にCopilotが実際に読む installed artifact を対象にした。

```text
~/.copilot2/installed-plugins/bluecore/bluecore
manifest version: 0.9.37
```

`validate_skills`、`validate_commands`、`validate_agents`、`validate_hooks`、
`ruff check src` は installed artifact に対して全てPASSした。

実agent `bluecore:harness-tuner` に、plugin bin を含まない
`PATH=/opt/homebrew/bin:/usr/bin:/bin` を渡して次を同一Bash invocationで実行した。

```sh
. "$HOME/.bluecore/env.sh" || exit 127
bluecore_run bluecore.ci.harness_audit repo --format json \
  --root /Users/tasaki-mamoru/dev/bluecore/plugins/bluecore \
  --target-kind repo
```

`bluecore_run`は解決され、exit 127なしでJSONの
`overall_score/max_score: 47/65` を取得した。終了コード1は未満点のauditに
対する通常結果でありbootstrap failureではない。ファイル・メモリ変更は行っていない。

以下の再現はすべてtemporary HOMEとfake helperを使い、実stateやplugin sourceを
変更していない。

## 2. v0.9.36からの再検証結果

| 前回ID | v0.9.37判定 | 根拠 |
|---|---|---|
| R-01 | **維持して解消** | pointerはtwo-line dataであり、`read -r`で読む。shell codeを含むpointerは実行されずexit 127。 |
| R-02 | **維持して解消** | PATH探索はない。PATH上のfake helperは選ばれない。 |
| R-03 | **維持して解消** | root pathをshellとして直列化しない。特殊文字を含むpathもdataとして扱える。 |
| R-04 | **維持して解消** | env pointerはwriter / resolverとも`$HOME/.bluecore`を使用する。 |
| H-02（v0.9.36） | **解消** | all-agree / recent fallbackを削除し、PPIDと親PIDだけをprocess start time照合する。 |
| M-01（v0.9.36） | **主競合を緩和** | GCはnon-numeric tempを即削除せず1時間のage gateを適用する。64並行process writerでwarning 0件、valid bootstrap生成、temp残留0件を確認。 |

H-02の隔離再現では、current PID名のpointerに偽のstart identityを置いた。

```sh
printf '%s\n%s\n' "$fake_root" 'invalid start identity' \
  > "$HOME/.bluecore/roots/$$"
PATH=/opt/homebrew/bin:/usr/bin:/bin \
  bash --noprofile --norc -c '. "$HOME/.bluecore/env.sh"'
```

結果はexit 127で、fake helperは実行されなかった。これはv0.9.36の
recent root fallback（別host rootを選び得た経路）が閉じたことを示す。

## 3. 残存検出事項

### H-01: verified PID pointer が指す任意helperを無検証でsourceする

対象: `runtime/env-template.sh:37-69,88-94`

v0.9.37はpointerのrootとPID start timeの組を照合する。しかし、rootが正規の
bluecore installationか、helperがwriter時点から改竄されていないか、root /
helperがsymlink・別owner・group writableでないかを照合しない。

```sh
if [ -z "$_bluecore_env_root" ] || \
   [ ! -f "$_bluecore_env_root/runtime/bluecore-helpers.sh" ]; then
  # fail
fi

. "$_bluecore_env_root/runtime/bluecore-helpers.sh"
```

#### 再現

child shellのPPIDは親Bash processになるため、親のC-locale start identityを
pointerの2行目へ記録する。pointer format自体は正しいが、rootはfake helperを
含むdirectoryを指す。

```sh
I="$HOME/.copilot2/installed-plugins/bluecore/bluecore"
D="$(mktemp -d)"
mkdir -p "$D/home/.bluecore/roots" "$D/fake/runtime"
cp "$I/runtime/env-template.sh" "$D/home/.bluecore/env.sh"
printf '%s\n' 'bluecore_run() { printf H01_FAKE_HELPER_EXECUTED; }' \
  > "$D/fake/runtime/bluecore-helpers.sh"

PID=$$
LSTART="$(LC_ALL=C ps -o lstart= -p "$PID")"
printf '%s\n%s\n' "$D/fake" "$LSTART" \
  > "$D/home/.bluecore/roots/$PID"

HOME="$D/home" PATH=/opt/homebrew/bin:/usr/bin:/bin \
  bash --noprofile --norc -c \
  '. "$HOME/.bluecore/env.sh"; bluecore_run'
```

出力:

```text
H01_FAKE_HELPER_EXECUTED
```

pointerをshell codeとして扱う問題（R-01）は解消済みだが、pointerのdataが
helper codeへの参照として権限を持つ問題は残る。0700 / 0600 stateは別OS user
からの変更を防ぐだけであり、同一userで動くagent / pluginがstateを永続化する
cross-session threatを防ぐ保証ではない。

**分類: HIGH。**

#### 修正案

1. pointerを`root + lstart`だけでなく、launcherが発行したverifiable recordに
   する。最低限 `root`、canonical root、plugin id、manifest version、helper
   digest、PID、process start identityを記録する。
2. resolverはrootの`lstat`、expected owner、directory mode、非symlink、
   manifestのplugin id、helper regular file / mode / digestを検証する。
3. recordまたはroot / helperの検証が失敗した場合、`source`せずexit 127にする。
   helperを実行する前のtrust boundaryではfail-openを採らない。
4. pointer stateを作るwriterもdirfd + `O_NOFOLLOW`でstate pathを扱い、
   state file差替えのTOCTOUを閉じる。

受入条件:

- 同じPID/start identityを持っていてもfake root、fake manifest、helper改竄、
  root symlink、helper symlink、別owner / group writable pathではexit 127となる。
- 正規artifactだけがmanifest / digest照合後に`bluecore_run`を定義する。
- 上記fixtureでfake helper markerは1回も出力されない。

### M-02: same-process writerのtemporary file名が衝突しうる

対象: `src/bluecore/lib/env_pointer.py:220-250`

GCによる別process temp削除はage gateで緩和された。しかしwriterのtemporary
file名は`<target>.tmp.<pid>`であり、同一Python process内の複数thread /
reentrant callが同じtargetを同時更新すると衝突する。

```python
tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
# ...
os.replace(tmp_path, path)
```

片方が`O_TRUNC`で他方の書込みを消し、先に`os.replace()`した後、後者の
`os.replace()`が`FileNotFoundError`になることがある。predictable nameは
同一UID環境でのprecreation / symlink raceも不必要に広げる。

前回の64並行**process** stressはwarning 0件だったが、各processのPIDが違うため
このsame-process衝突を検証していない。

**分類: MEDIUM。**

#### 修正案

1. `tempfile.mkstemp(dir=..., prefix=..., suffix=...)`、または
   `O_CREAT | O_EXCL | O_NOFOLLOW`とrandom suffixでtemporary fileを作る。
2. 同一targetへのwriterをprocess内lockで直列化する。複数processを含む
   strict serializationが必要なら、trusted state dir内のadvisory file lockを使う。
3. file fsync後の`os.replace()`に加えdirectory fsyncを行う。
4. GCはtemporary naming namespaceを理解し、active lock / leaseを持つtempを
   回収しない。ageだけを唯一の安全判定にしない。

受入条件:

- barrierで同期した複数threadが同じpointer / env fileを書いても例外が出ず、
  最終fileは完全な有効recordだけとなる。
- same-process / multi-processのreader-writer stressでpartial file、warning、
  stray active tempが0件となる。
- precreated tempまたはsymlink tempをwriterが追従せず安全に失敗する。

## 4. release assurance: sourceとinstalled artifactが乖離している

repository HEADはv0.9.35である一方、installed runtimeはv0.9.37である。
`runtime/env-template.sh`と`src/bluecore/lib/env_pointer.py`は両者で一致しない。
source側にはv0.9.35のPATH探索、sourceされたpointer、latest fallbackが残る。

この乖離により:

- sourceレビュー・CIが実行中v0.9.37の挙動を保証できない。
- sourceから再packageすると修正済みH-02等が再導入される。
- resolverのisolated regression testsをsource repositoryで追跡できない。

#### 修正案

1. v0.9.37のresolver / writer / docs / version metadataをsource repositoryへ
   同期してcommitする。
2. package artifactをそのcommitからのみ生成し、release jobがmanifest versionと
   source commit / digestを照合する。
3. source `tests/` にpointer format、shell-code非実行、fake root拒否、
   PID/start identity、ancestor selection、same-process race、GC、
   HOME contract、clean-shell helper bootstrapの回帰テストを追加する。
4. CIはsource treeでpytest collectionが1件以上であることと、resolver test
   matrixを必須gateにする。配布artifactへtestsを同梱しない方針とは両立する。

**分類: HIGH（release assurance）。**

## 5. 結論

v0.9.37はH-02のglobal fallbackを排除し、PID reuseをstart identityで識別する。
実agent bootstrapも正常であり、前回M-01のprocess-level GC競合も大きく緩和した。

しかしH-01は、正しいPID/start identityを持つpointerのrootをtrust anchorとして
無検証でsourceするため未解消である。さらにM-02とsource / artifact乖離が残り、
修正の継続性を保証できない。

H-01のroot/helper verification、M-02のunique atomic temp、source同期および
source-level regression testsが完了するまで、plugin-root resolverのrelease判定は
**BLOCK** とする。
