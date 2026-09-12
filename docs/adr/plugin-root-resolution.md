# plugin root の解決と、同一 UID 脅威モデルの限界

md からベンダ固有パスを排除したまま、どの plugin install を実行するかをどう決めるか。および
その解決結果をどこまで検証するか（＝検証しても意味がない範囲はどこか）。

## 共通原則

**同一 UID の攻撃者に対しては、resolver が読める入力の真正性を検証できない。** pointer file も
root ディレクトリも helper も manifest も digest も、すべて攻撃者が自分の権限で用意できる。どんな
検証を足しても、まさに防ぎたい攻撃者がその検証を満たす。これは実装コストの問題ではなく原理的な
限界であり、同一 OS ユーザーの敵対的回避は元々このプロジェクトの脅威モデルの対象外である。

一方で**推測 fallback は別**である。「祖先であることを証明できないまま、たまたま一致する候補を
採る」構造には「証明できる候補だけを使う」というより厳格な代替が存在するため、これは受容せず
排除する。区別の軸は「より厳格な選択肢が存在するか」であり、存在しないものへの非対応は「リスクの
許容」ではなく「防御不能な対象への対処の見送り」である。

**`roots/` の汚染は enforcement hook には及ばない。** `hooks.json` の全エントリは
`runtime/claq-hook`（PowerShell ホストでは同エントリの `powershell` が `runtime/claq-hook.cmd`）を
呼ぶだけで `env.sh` を source しない。resolver は保護 hook の実行経路に一切登場せず、汚染で保護
hook を無効化することはできない。影響は md/agents/skills から呼ばれる `claq_run` /
`claq_mem_learn` の汚染に限定される。

## plugin root は `~/.claq/env.sh` ポインタで解決し、md にベンダ固有パスを書かない

Bash tool の環境変数に `CLAUDE_PLUGIN_ROOT` は乗らない。`hooks.json` 内の `${CLAUDE_PLUGIN_ROOT}`
はホストが hook 起動コマンド文字列を展開する際にのみ使われ、Bash tool の子プロセス環境には伝播
しない。bootstrap を持たない md は `claq_run: command not found` になる。ホストの識別用環境変数を
Bash tool へ注入する保証も無い。したがって `.claude` / `.copilot` / `.grok` 等のベンダ固有
インストールパスを md から排除し、host-dependent なロジックは実装ファイルへ隔離する。

**plugin root の解決を `~/.claq/env.sh`（launcher が全 hook 起動時に書き出す固定住所）へ一元化
し、md にはベンダ名を一切書かない。** ホストの判別は環境変数ではなく祖先プロセスの PID + `lstart`
（起動時刻）を鍵にする。roots pointer は shell コードではなく単なるデータ（root + lstart の
2 行）として扱う。祖先で解決できない場合に推測する fallback は持たない。writer が書き込む祖先の
範囲は host インスタンス専有の PID（最大 2 段）に限定し、他 host と共有されうる祖先には一切
書き込まない。

構成:

1. `write_env_pointer(plugin_root)` は `os.getppid()` とその親（最大 2 段）にポインタを書く。
   1 回の `ps -eo pid=,ppid=,lstart=` でチェーンと各 PID の起動時刻を同時に取得する。`ps` の実行
   に失敗した場合はその回は何も書かない（検査対象が確定していない環境故障として fail-open にし、
   次回 hook 起動で自己修復する）
2. 各ポインタは `root\nlstart\n` の 2 行。`.`/`source` はせず 2 行取り出すだけ。`lstart` は
   「今生きているその PID は、記録時と同一のプロセスか」を照合する識別子
3. 深さを**2 段に絞る**のは、共有されうる祖先へ書き込むこと自体をやめるため。`os.getppid()`
   （bash tool の shell）とその親（host バイナリ）はいずれもこの host インスタンス専有の PID
   である。より上位の祖先（login shell・terminal app）は別々の host 間で共有されうるが、writer
   はそこへ一切書かない。同一 PID に別の root が観測されるのは「同一 host がプラグインを
   アップグレードした」ケースのみであり、上書きが正しいので単純に上書きする（衝突検知は行わない）
4. resolver は自分の `$PPID` とその親（最大 2 段）を辿り、pointer が 2 行とも揃っていて、かつ
   現在の lstart が記録値と一致する場合のみ採用する。空・欠落・不一致（PID 再利用）はスキップ
   して次の祖先へ進む。**祖先チェーンで解決できなければ、推測せず常に `exit 127`**
5. pointer の書き込みは一時ファイル経由の原子的置換で行う。置き場所は `$HOME` 固定とする
   （md の bootstrap 行が `$HOME` 固定である以上、writer 側もそれに合わせる）
6. `launcher.py:main()` が全 hook 起動のたびに `write_env_pointer()` を呼ぶ。書き込み失敗は
   握り潰すが、プロセスごと 1 回だけ stderr へ JSON 警告を出す
7. `write_env_pointer()` は 1 時間に 1 回だけ `roots/` と一時ファイルの GC を実行する。生存 PID
   かつ TTL 内のポインタは誤って削除しない
8. `LC_ALL=C` を writer・resolver 双方で明示する。`ps -o lstart=` の出力はロケール依存
9. agents/commands/skills の md は `. "$HOME/.claq/env.sh" || exit 127` の 1 行だけを書く
10. helper が返す root は、env.sh が pointer から選んで source した root と一致させる。ambient
    `CLAUDE_PLUGIN_ROOT` で自己申告を上書きしない

単一ファイルに root を直接埋め込む方式は、別ターミナルで複数ホストを並行起動すると後に
SessionStart が走ったほうの root が先のセッションのポインタを上書きし、先に開いていたセッションが
別ホストの install を source する事故を起こすため採らない。ホストの識別用環境変数でポインタを
分岐する方式も、Copilot CLI・Grok CLI が bash tool の環境へ自身の識別変数を注入する保証がなく、
実測・文書で裏付けが取れない前提に設計を依存させることになるため採らない。ランタイム実装を
`~/.claq` へ集約インストールする方式は、ランタイムが venv も install.sh も持たないという設計
原則と衝突し、プラグインとランタイムの版が分離して契約不整合が発生しうるため採らない。祖先で
解決できない場合に「`roots/` の有効な候補が全て同じ root 値に合意しているか」で推測する方式、
祖先チェーン全体に書いて対立する root を poison する方式は、いずれもリスクがあるとみなせる推測
fallback であり、祖先チェーンへの書き込み + `lstart` 個体識別という推測なしの解決策が既にある
以上採らない。

`ps -o lstart=` の出力はロケール依存であり、`ps` 実装が `LC_ALL` を無視する将来のプラットフォーム
では常に 127 になりうる。祖先チェーンの深さは 2 段に固定しているため、host の起動構成がこの前提
と食い違う場合（bash tool の shell の直接の親が host バイナリではなく、さらに使い捨ての中間
shell を挟む構成）でも 127 になりうる。127 は自己修復可能なので、この trade-off は受け入れる。

## roots pointer resolver は owner/mode 検証を行わない

roots pointer は読まれるデータであり、pointer file への書き込み権限を持つ攻撃者が得られるのは
任意コード実行ではなく、誤った root path を resolver に読ませることまでである。誤った root path
の結果は「存在しないパスなら 127」「`runtime/claq-helpers.sh` を欠くパスなら 127」「別の
（同一ユーザーが書き込み可能な）claq install を source する」のいずれかである。`~/.claq` は
ディレクトリとして 0700 に締めており、同一 OS ユーザー内の他プロセスからの書き込みは元々脅威
モデルの範囲外である。PID 再利用は前節の `lstart` 照合で検出する。

**pointer file の owner/mode 検証は実装しない。** resolver が読める入力は、すべて同一 UID の
攻撃者が自由に用意できる。`test -O`/`-h`/`-G` でも `stat` ベースの検証でも、manifest/digest
方式でも変わらない。検証手段の実装コストの問題ではなく、検証すべき対象が「攻撃者と正規 writer が
区別できない」という原理的な限界である。owner・symlink 判定は `test -O` / `test -h` で実装
できるが、mode ビット判定は POSIX test に group/world-writable を判定する演算子が無く `stat` の
機種依存パースが必要になる——いずれにせよ非実装の主論拠は実効性であり、移植性ではない。

owner/mode 非検証は「推測 fallback を許容しない」の対象ではない。推測 fallback には「証明できる
候補だけを使う」というより厳格な代替が存在するが、owner/mode 検証にはこの構造が無い。したがって
非対応は「リスクの許容」ではなく「防御不能な対象への対処の見送り」である。

`~/.claq` の権限が 0700 から緩んだ場合、同一 OS ユーザー内の他プロセスが pointer を書き換え
られる。これは脅威モデル外だが、書き込みのたびに `chmod(0o700)` で締め直す形で緩和している
（能動的な検証ではなく再強制）。Windows では DACL を変えない。
