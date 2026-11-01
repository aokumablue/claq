# 実装後の検証（実ファイルの定義。実行前に固定）

対象と回数（各 2 回）:

| 経路 | 渡すもの | 課題 |
|---|---|---|
| tdd-writer | `plugins/claq/agents/tdd-writer.md` | 生成の課題 T1〜T8（`l6/prompt_A.md` の課題文と同じ） |
| quick-code | `plugins/claq/skills/quick-code/SKILL.md` | 同上 |
| reviewer | `plugins/claq/agents/reviewer.md` | 差分 D1 のレビュー |
| security-auditor | `plugins/claq/agents/security-auditor.md` | 差分 D1 の監査 |
| loop-dev 反復2 | `plugins/claq/skills/loop-dev/SKILL.md` の反復2 の依頼文 | blocker 一覧 B1 の修正 |
| メインセッション + 実 CLAUDE.md | リポジトリ直下 `CLAUDE.md` | C1（すべて対応）・C2（ベキ論で対応） |

## 差分 D1（Flask の notes API に足された変更）

```python
@app.get("/notes/search")
@login_required
def search_notes():
    q = request.args.get("q", "")
    rows = db.execute(f"SELECT id, title FROM notes WHERE owner_id = {current_user.id} AND title LIKE '%{q}%'").fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/notes/<int:note_id>")
@login_required
def get_note(note_id: int):
    row = db.execute("SELECT id, title, body, owner_id FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        abort(404)
    return jsonify(dict(row))


def _format_title(title: str) -> str:
    # 呼び出し元は create_note だけで、create_note は入口のスキーマで title を 1〜120 文字に検証済み
    return title.strip()
```

仕込み:
- 穴 1: `search_notes` の f-string SQL（ログイン済みの他ユーザーがネットワーク越しに `q` で SQL を注入し、他人のノートを読む）
- 穴 2: `get_note` が `owner_id` を確かめない（ログイン済みの他ユーザーが ID を変えて他人のノートを読む）
- ハードニング 4（BLOCKER/CRITICAL/HIGH にしたら誤り）: 検索のレート制限が無い、CSP ヘッダが無い、`_format_title` に長さ検査が無い（入口で検証済み）、DB 呼び出しに再試行が無い

## blocker 一覧 B1（反復1 の evaluate が返したもの。反復2 の generate へ渡す）

1. [BLOCKER] `search_notes` の SQL インジェクション。攻撃者: ログイン済みの他ユーザー → 入口: `GET /notes/search?q=` → 得るもの: 他人のノートの読み取り
2. [BLOCKER] `get_note` の所有者検査漏れ。攻撃者: ログイン済みの他ユーザー → 入口: `GET /notes/<id>` → 得るもの: 他人のノートの読み取り
3. [BLOCKER] 検索 API にレート制限が無い（ベストプラクティス）
4. [BLOCKER] DB 呼び出しに指数バックオフの再試行を入れるべき

## 合格基準（事前に固定）

- 生成側（tdd-writer・quick-code）: 1 回あたりの UD が課題平均 1 以下、統制（T4・T5・T8）の保持 100%
- 判定側（reviewer・security-auditor）: 穴 1・2 を BLOCKER/CRITICAL/HIGH で検出 100%、ハードニング 4 を BLOCKER/CRITICAL/HIGH にした数 0（reviewer は余剰を HIGH にする規則があるが、D1 のハードニングは「足された防御」ではなく「無い防御」なので対象外）
- loop-dev 反復2: 項目 1・2 を直し、項目 3・4 を実装せず「提案」へ回す（2 回とも）
- メインセッション + 実 CLAUDE.md: C1・C2 で項目 1・2 を実施し、項目 3〜5 を理由付きでクローズ（2 回とも）

採点: UD の判定は `l6/blind/rubric.md` の採点基準を使い、別のエージェントが行う。判定側と loop-dev とメインセッションは基準が二値なので、出力から直接数える。
