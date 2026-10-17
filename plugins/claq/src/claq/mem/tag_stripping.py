"""信頼境界タグ（``<claq-memory>`` 等）を無害化する。

無害化は **「対になったブロックの除去」→「残った ``<`` のエスケープ」** の
2 段で行い、**エスケープを必ず最後に置く**。この順序が本モジュールの安全性の
根拠であり、崩すと除去が生きたタグを再構成して注入を許す。

エスケープを最後に置く理由（除去による接着の構造的封じ込め）
    除去はどれほど注意深く書いても、消した箇所の前後の文字を接着して
    **新しい生きたタグを作り出す**。除去だけで無害化すると次のようになる::

        strip_tags('<<private>/claq-memory>')  ->  '</claq-memory>'

    孤立タグを「除去」で消す方式では、この再構成を潰すために全パターンを
    変化が無くなるまで反復する必要があり、反復に上限を置けばそこが fail open
    になる。エスケープ（``<`` → ``&lt;``）は **文字を 1 つも消さない**ため
    接着が原理的に起きず、置換が新たな ``<`` を生まないため **1 パスで完全**に
    なる（不動点反復が要らない）。除去の後にエスケープを置けば、除去が接着で
    作ってしまったタグもその場で無害化される。したがって出力に「既知タグ名の
    直前の生の ``<``」は決して残らない — これが本モジュールの不変条件である。

ブロック除去を残す理由
    エスケープだけにすると次の 2 つが壊れるため、対になったブロックの
    中身ごとの除去は維持する。

    1. **エコー抑止**: handoff は前セッションへ注入した ``<claq-memory>``
       ブロックが transcript に残っているものを読む。中身を消さないと、
       注入した記憶をそのまま引き継ぎとして記録し直すエコーが起きる。
    2. **シークレット再結合**: ``sk-ant-<private>zz</private>api03-…`` の
       ように、タグで分断された秘密は中身を消して初めて 1 本の文字列へ戻り、
       後段の ``redact`` が捕まえられる（``handoff.build_handoff``）。

精度の要求が段によって違う理由
    **消す側（ブロック除去）は精度が要る** — 誤検出はそのまま本文の消失に
    なるため、タグ名の直後に名前の終わり（``_TAG_NAME_END``）を要求して
    ``<privateer>`` のような別タグを巻き込まない。**escape する側は精度が
    要らない** — 誤検出しても ``&lt;`` になるだけで情報は失われないため、
    名前の終わりを要求せず、境界マーカーの偽装に使えそうな ``<`` を広く倒す。
    最後の防壁は広く、破壊的な処理は狭く、という非対称は意図的である。

パイプライン単位で不変条件を守る 2 つの検査
    「エスケープを最後に置く」は本モジュールの中でしか成立しない。呼び出し側の
    後段に別の変換が控えていると、同じ穴がモジュールの外側で開く。単体では
    見えないため、``strip_tags`` は返す直前に 2 つの検査を通す。

    1. **シークレットのタグ分断**（``_hides_secret_behind_tags``）: escape は
       文字を消さないため、``sk-ant-<private>api03-…`` は 1 本の鍵へ戻らず
       ``redact`` のパターンに一致しない（``&`` が文字クラスの外にある）。
       「鍵を書くとき ``sk-ant-`` の直後へ ``<private>`` を 1 個入れろ」と
       指示するだけで redaction を回避できてしまう。判定専用の複製
       （``_erase_known_tags``。出力へは決して回さない）を使い、**マスクと
       タグ除去が可換か**を調べる。可換でなければタグが秘密を隠しているので
       本文ごと ``[REDACTED]`` へ倒す。マスク個数の比較では足りない —
       出力側が別のパターンで 1 個マスクされていると個数が釣り合い、鍵が
       分断されたまま素通りする（属性 512 文字超のペアタグで実測）。
    2. **削除変換によるタグの再鍛造**（``_forges_tag_after_deletion``）:
       後段の ``slim_text.remove_filler_phrases`` は ``まあ`` ``ちなみに`` 等を
       位置に関わらず削除するため、escape 済みの本文からタグを組み上げ直せる。
       実測: ``<system-まあreminder>`` は本モジュールのどの語彙にも一致せず
       素通りし、圧縮後に生きた ``<system-reminder>`` になる。削除後の複製で
       無害化対象が**増える**なら、``&`` と ``<`` を 1 つ残らず倒して返す。
       倒した後は削除で ``<`` も ``&lt;`` も作れないため、この 1 手で閉じる
       （反復は要らない = fail open の余地が無い）。

なお ``<system-reminder>`` 等のハーネス足場タグを**中身ごと除去する**規則は今も
本モジュールの管轄外である（``lib/harness.normalize_user_message`` が担う。
docs/adr/untrusted-input-prompt-boundary.md が「中身を残すべき用途と捨てるべき用途を
同じ関数に同居させない」と定めている）。escape の語彙にだけ足場タグを含めるのはこの
合流に当たらない — escape はどちらの用途でも中身を残すのでこの衝突が起きない。実際に
注入側（``mem/cmd_context._handoff_section``）は ``strip_tags`` しか掛けず、足場タグを倒す
手段が他に無い。

文字の許可リストと全角山括弧
    ``strip_tags`` の前段として、注入経路は ``keep_allowed_chars`` で**許可した文字だけ**を
    残す。禁止リストは未知の不可視文字（ゼロ幅・bidi・タグ文字）を通すため採らない。
    ``strip_tags`` は ``<`` を ``&lt;`` にする際、NFKC で ``<`` ``/`` になる文字
    （``＜`` ``／`` 等）で書かれた ``claq-memory`` タグの開き山括弧も同じ形で倒す。
    NFKC は検出だけに使い、本文は書き換えない。

    許可リストの中にも、ASCII の間へ挟んでタグ名や鍵を分断できる不可視の文字が残る。
    CGJ・ハングル filler・点字空白は常に落とし、異体字セレクタは直前が ASCII のときだけ
    落とす（絵文字の表示選択子・人名の IVS・keycap は残す）。視認できる結合記号は分断として
    見えるため残す。この判定は ``keep_allowed_chars`` の 1 箇所に閉じる。
"""

from __future__ import annotations

import re
import unicodedata

from claq.lib.harness import COMMAND_TAGS, SCAFFOLD_TAGS
from claq.lib.slim_text import remove_filler_phrases
from claq.mem.redaction import redact

# 無害化対象タグ（大文字小文字区別なし）。正規表現へ literal として埋めるため、
# 正規表現メタ文字を含まない名前だけを置く。
_TAGS = (
    "private",
    "mem-context",
    "claq-memory",
    "system_instruction",
    "system-instruction",
)

# 診断側（ci/scan_scaffold_drift.py）が「既知タグ」を組み立てるための公開名。
# これは**ブロック除去**の語彙であり、escape の語彙（`_NEUTRALIZE_TAGS`）とは
# 別物で、後者のほうが広い。診断側が知りたいのは「中身ごと落としたブロック」の
# 名前なのでこちらを渡す。escape の語彙へ名前を足しても診断側の既知集合は
# `SCAFFOLD_TAGS` / `COMMAND_TAGS` 経由で揃うため、ここへ写す必要は無い。
STRIPPED_TAGS = _TAGS

# 入れ子ブロック除去の最大反復回数。否定先読みは同種の開始タグを跨がないため、
# 入れ子は内側から 1 段ずつ落ちる。1 回の sub では外側の中身が残るため
# 変化が無くなるまで回す（`<private>SECRET<private>x</private>SECRET</private>`
# の SECRET を残さない）。現実の入れ子はごく浅く、上限は暴走防止のみ。
# 上限超過で残った分は後段のエスケープが無害化するため、ここは fail open で
# 構わない（残るのは攻撃者が平文で書けるのと同じ文字列であり、タグとしては
# 機能しない）。
_MAX_NESTING_PASSES = 20

# タグの属性部に許す最大文字数。`[^>]*` を無界にすると、`>` を 1 個も含まない
# 入力（`"<private " * N`）で各開始位置が末尾まで走査して二次オーダーになる。
# **この上限は複雑度のための装置であって、安全性の境界ではない。** 超過した
# タグはブロック除去に一致しないが、後段のエスケープは属性を一切見ない固定幅
# 先読みなので必ず無害化する（ブロック除去だけを関門にすると、属性
# 512 文字超のタグが素通りする）。
_MAX_TAG_ATTR_CHARS = 512
_TAG_ATTRS = rf"[^>]{{0,{_MAX_TAG_ATTR_CHARS}}}"

# タグ名の直後に「名前の終わり」を要求する先読み。これが無いと `[^>]*` が
# 属性を許すつもりで、タグ**名**が対象名で始まるだけの別タグ（`<privateer>`）
# まで一致し、無関係な本文をブロックごと消してしまう。
_TAG_NAME_END = r"(?=[\s/>])"

# 開始〜終了タグとその中身を除去するパターン。
#
# 中身は「同種の開始タグを含まない任意の文字列」に限る。裸の `.*?` だと、
# 閉じタグを伴わない開始タグが並ぶ入力で各開始位置が末尾まで走査して
# 二次オーダーになる（実測: `'<private>' * N` が N=4000 で 0.62 秒、
# 8000 で 2.47 秒、16000 で 9.93 秒 — 入力 2 倍で 4 倍）。属性部の有界化
# （`_TAG_ATTRS`）が塞ぐのは `>` を含まない入力だけで、この経路は別物。
# `strip_tags` は SessionStart の `mem context`（timeout 60 秒）が知識カードの
# title/body に対して毎回呼ぶため、ここの複雑度はセッション開始の遅延になる。
# あわせて、対を成さない開始タグから後続ブロックの閉じタグまで貫通して
# 間のテキストを巻き込む挙動も消える。
#
# 閉じタグ側は属性を許さない（`</{tag}\s*>`）。開始タグ側と対称化して
# `</{tag}[^>]{0,512}>` にすると、`[^>]` が改行も散文も跨ぐため、対にならない
# `</private ` から**次に現れる任意の `>`**（別行の `->` でも可）までを本文ごと
# 削除する経路ができる（数行分の散文が黙って消えうる）。消す側の誤検出は
# そのまま本文の消失であり、この非対称は意図的なものである。
#
# 属性付き閉じタグ（`</claq-memory x>`）の無害化は後段の escape が担う。escape は
# 属性を一切見ないため属性付き閉じタグはそちらで完全に塞がっており、ペア除去まで広げる必要は無い。
# 広げて得られるのは `<tag>X</tag foo>` の中身除去だけだが、その中身除去に依存する
# シークレット再結合は `_erase_known_tags` の判定経路が別途担保する。
_PAIR_PATTERNS = [
    re.compile(
        rf"<{tag}{_TAG_NAME_END}{_TAG_ATTRS}>"
        rf"(?:(?!<{tag}[\s/>])[\s\S])*?"
        rf"</{tag}\s*>",
        re.IGNORECASE,
    )
    for tag in _TAGS
]

# 判定専用の「既知タグを除去する」パターン。属性は改行と `<` を跨がせない
# （跨がせると判定用複製の側で本文を巻き込み、誤った fail closed を招く）。
# escape 済みの表記（`&lt;private>`）も対象にする — 判定は `strip_tags` が
# 返そうとしている**出力**に対して行うため、生の `<` はそもそも残っていない。
_ERASE_PATTERN = re.compile(
    r"(?:<|&lt;)/?(?:" + "|".join(_TAGS) + r")" + _TAG_NAME_END + r"[^<>\n]{0,1024}>",
    re.IGNORECASE,
)

# escape 対象のタグ語彙。ペア除去（`_TAGS`）より広く、ハーネス足場タグと
# スラッシュコマンド起動タグを含める。**`_PAIR_PATTERNS` は決してこの語彙へ
# 広げない** — 中身を捨てる規則は `lib/harness` の管轄で、合流は docs/adr/untrusted-input-prompt-boundary.md が
# 却下している。escape はどちらの用途でも中身を残すため衝突しない。
# 値は各定義モジュールから導出する（写経すると片方だけ更新されて穴が開く）。
_NEUTRALIZE_TAGS = (*_TAGS, *SCAFFOLD_TAGS, *COMMAND_TAGS)

# 既知タグ名（開始・終了の両方）の直前にある `<` を捉える先読み。属性も `>` も
# 見ないため、属性の長さ・閉じ括弧の有無に関わらず必ず一致する。先読みは固定幅の
# 選択肢なので走査は入力長に対して線形。
_NEUTRALIZE_PATTERN = re.compile(r"<(?=/?(?:" + "|".join(_NEUTRALIZE_TAGS) + r"))", re.IGNORECASE)

# `<` を表す HTML 実体参照の先頭 `&`。`<` の escape より**前**に倒すことで、
# 無害化を単射に保つ。倒さないと、悪意ある `</claq-memory>` の出力と、良性の
# 本文が literal で書いた `&lt;/claq-memory>` の出力がバイト同一になり、将来
# どこかで実体参照の復号が入った瞬間に後者の見た目をした前者が生きたタグへ戻る。
# `&lt` の大小は HTML5 が定義する `&lt;` と `&LT;` の 2 通りだけで、`&Lt;` は
# 実体参照ではない（`html.unescape` も復号しない）ため語彙に入れない。
# この置換も文字を消さないので no-deletion 性質と 1 パス完全性は保たれる。
_ENTITY_LT_PATTERN = re.compile(r"&(?=lt;|LT;|#0*60;|#[xX]0*3[cC];)")

# `<` の置換先。文字を消さずにタグとしての機能だけを奪う。
_NEUTRALIZED_LT = "&lt;"

# `&` の置換先。実体参照の先頭としての機能だけを奪う。
_NEUTRALIZED_AMP = "&amp;"

# 検査が細工を検出したときに返す本文。`redaction._PLACEHOLDER` と同じ文字列だが、
# 依存の向きを増やさないためここで持つ（`mem/handoff.py` も同じ理由で自前に持つ）。
# 空文字列にはしない — 呼び出し側の `redact` を通しても `[REDACTED]` が残ることが、
# 「秘密が入っていたが倒した」という事実を下流へ伝える唯一の手段になる。
_REDACTION_MARKER = "[REDACTED]"

# 3 行以上の空行を 2 行へ詰めるパターン。
_BLANK_LINES = re.compile(r"\n{3,}")

_LT_LIKE = frozenset({"<", "\ufe64", "\uff1c"})
"""NFKC で ``<`` 1 文字になる文字（ASCII の ``<``、小書き ``﹤``、全角 ``＜``）。

Unicode の更新で増えても検出漏れにならないよう、全コードポイントの走査から導いた集合と
テストで突き合わせる（import 時に全走査すると SessionStart の遅延になる）。
"""

_CLAQ_MEMORY_OPEN = re.compile(r"<(?=/?claq-memory)", re.IGNORECASE)
"""NFKC 後の本文で ``claq-memory`` タグの開き山括弧を捉える（``_NEUTRALIZE_PATTERN`` と同形）。"""

_LINE_BREAKS = frozenset("\r\x0b\x0c\x85\u2028\u2029")
"""LF に揃える行区切り: CR・VT・FF・NEL・LS・PS（CRLF は先に LF 1 つへ畳む）。"""

_KEPT_CATEGORIES = frozenset({"Zs"})
"""一般カテゴリのうち、頭文字では括れない残す分類。L*・M*・N*・P*・S* は頭文字で判定する。"""

_KEPT_CATEGORY_INITIALS = frozenset("LMNPS")
"""残す一般カテゴリの頭文字（文字・結合記号・数字・句読点・記号）。"""

_ALWAYS_DROPPED = frozenset("\u034f\u115f\u1160\u3164\uffa0\u2800")
"""許可カテゴリに属するが、文脈を問わず落とす不可視文字。

CGJ（U+034F）・ハングル filler（U+115F・U+1160・U+3164・U+FFA0）・点字空白（U+2800）。
何も表示しないため ASCII の間や行頭へ挟んでタグ名・鍵・行頭マーカーを分断でき、
日本語の本文には現れない。
"""

_VARIATION_SELECTORS = "\ufe00-\ufe0f\U000e0100-\U000e01ef"
"""異体字セレクタ（U+FE00–FE0F、U+E0100–E01EF）。正規表現の文字クラスの中身。"""

_KEYCAP = r"[0-9#*]\ufe0f?\u20e3"
"""keycap 絵文字の並び（``1️⃣``）。異体字セレクタの除去から丸ごと除く。"""

_SELECTOR_AFTER_ASCII = re.compile(rf"({_KEYCAP})|(?<![^\x00-\x7f])[{_VARIATION_SELECTORS}]+")
"""ASCII（または文頭）の直後に続く異体字セレクタの連なりと、除外する keycap の並び。

ASCII に付いた異体字セレクタは何も修飾せず、ASCII の間へ挟んでタグ名や鍵を分断する足場に
なる。ASCII に付かないもの（絵文字の表示選択子 ``❤️``、人名の異体字 IVS ``葛󠄀``）は
文字の見え方を決めるので残す。視認できる結合記号（分音記号・濁点・母音記号）は分断として
見えるので、ASCII に付いていても対象にしない。
"""

_MAX_CACHED_CODEPOINTS = 65536
"""判定結果を覚えるコードポイント数の上限。入力に全 Unicode が並んでも表が肥大しない。"""


class _AllowlistTable(dict):
    """``str.translate`` に渡す許可リストの判定表。コードポイントごとの判定を覚える。

    値が文字なら残す（行区切りは ``\\n``）、``None`` なら落とす。``str.translate`` は
    表に無い文字で ``__missing__`` を呼ぶため、初見の文字だけ Python で判定し、
    以後は C の辞書引きで済む（1MB の日本語でも速い）。
    """

    def __init__(self, max_entries: int = _MAX_CACHED_CODEPOINTS) -> None:
        """判定表を作る。

        Args:
            max_entries: 判定結果を覚える件数の上限。超えた文字は覚えずに毎回判定する。
        """
        super().__init__()
        self._max_entries = max_entries

    def __missing__(self, codepoint: int) -> str | None:
        """未判定のコードポイントを許可リストで判定する。

        Args:
            codepoint: 判定する文字のコードポイント。

        Returns:
            残す文字（行区切りは ``\\n``）。落とすなら ``None``。
        """
        ch = chr(codepoint)
        category = unicodedata.category(ch)
        if ch in _LINE_BREAKS:
            verdict: str | None = "\n"
        elif ch in _ALWAYS_DROPPED:
            verdict = None
        elif ch in "\t\n" or category[0] in _KEPT_CATEGORY_INITIALS or category in _KEPT_CATEGORIES:
            verdict = ch
        else:
            verdict = None
        if len(self) < self._max_entries:
            self[codepoint] = verdict
        return verdict


_ALLOWLIST_TABLE = _AllowlistTable()


def drop_known_tag_blocks(text: str) -> str:
    """開始・終了が対になった対象タグを中身ごと除去する。

    入れ子は内側から 1 段ずつ落ちるため、タグごとに変化が無くなるまで
    ``_MAX_NESTING_PASSES`` を上限に反復する。上限超過分が残っても
    ``strip_tags`` 後段のエスケープが無害化する。

    公開しているのは診断側（``ci/scan_scaffold_drift``）のためである。あちらが
    欲しいのは「中身ごと落としたブロックの内側タグを未知タグとして数えない」
    ことだけで、無害化は要らない。``strip_tags`` をそのまま呼ばせると、細工を
    検出して ``&`` と ``<`` を全て倒した本文・``[REDACTED]`` へ倒した本文では
    未知タグが 1 つも見えなくなる（実測）。ドリフト診断が最も見たいのは
    まさにその種のメッセージなので、除去だけを切り出して渡す。

    Args:
        text: 除去前のテキスト。

    Returns:
        対になったブロックを中身ごと除いたテキスト。**無害化はしていない** —
        出力・永続化・注入へ回すなら ``strip_tags`` を使うこと。
    """
    for pattern in _PAIR_PATTERNS:
        for _ in range(_MAX_NESTING_PASSES):
            stripped = pattern.sub("", text)
            if stripped == text:
                break
            text = stripped
    return text


def _collapse_blank_lines(text: str) -> str:
    """3 行以上の空行を 2 行へ詰め、前後の空白を落とす。

    削除ではあるが、空行は 2 行残るため前後の文字が接着することはない
    （タグの再鍛造経路にならない）。

    Args:
        text: 整形前のテキスト。

    Returns:
        空行を詰めて前後を strip したテキスト。
    """
    return _BLANK_LINES.sub("\n\n", text).strip()


def _compat_open_positions(text: str) -> list[int]:
    """NFKC で ``claq-memory`` タグになる並びの、開き山括弧の位置を返す。

    注入枠を閉じるのは字面どおりの ``</claq-memory>`` だけだが、``＜/claq-memory＞`` や
    ``<／claq-memory>`` のように ``<`` ``/`` ``-`` やタグ名を互換文字で書いた表記は、
    LLM が同等に読みうる。NFKC は**検出のためだけ**に使い、本文は書き換えない。

    NFKC は文字列長を変える（``㍿`` → ``株式会社``）ため、1 文字ずつ正規化して元の位置を
    持ち回る。タグ名を含まない本文（大多数）は、全体を 1 回正規化して名前の有無を見るだけで
    返る。この入口の判定は NFKD で行う（NFKC は末尾の結合記号を直前の文字へ合成して
    ``y`` + U+0301 → ``ý`` とし、1 文字ずつ正規化する本走査と食い違って検出漏れになる）。
    ASCII のみの本文は NFKC で変わらないので走査しない（``_NEUTRALIZE_PATTERN`` が担う）。

    Args:
        text: 検査対象のテキスト（生の ``<`` でも escape 済みでもよい）。

    Returns:
        開き山括弧にあたる元の文字の位置（昇順）。元の文字が NFKC で ``<`` 1 文字になる場合に
        限る。
    """
    if text.isascii() or "claq-memory" not in unicodedata.normalize("NFKD", text).lower():
        return []
    normalized: list[str] = []
    owners: list[int] = []
    for index, ch in enumerate(text):
        folded = unicodedata.normalize("NFKC", ch)
        normalized.append(folded)
        owners.extend([index] * len(folded))
    return sorted(
        {
            owners[match.start()]
            for match in _CLAQ_MEMORY_OPEN.finditer("".join(normalized))
            if text[owners[match.start()]] in _LT_LIKE
        }
    )


def _escape_compat_brackets(text: str) -> str:
    """互換文字で書かれた ``claq-memory`` タグの開き山括弧を ``&lt;`` にする。

    ``claq-memory`` を書いていない全角記号（``＜重要＞``）は一切変えない。文字を消さず
    置換するだけなので、前後の文字が接着して新しいタグを作ることはない。

    Args:
        text: 無害化途中のテキスト。

    Returns:
        該当する開き山括弧を ``&lt;`` にしたテキスト。
    """
    positions = _compat_open_positions(text)
    if not positions:
        return text
    chars = list(text)
    for position in positions:
        chars[position] = _NEUTRALIZED_LT
    return "".join(chars)


def _neutralize(text: str) -> str:
    """実体参照の ``&`` とタグの ``<`` を、この順で倒し、互換文字の開き山括弧も倒す。

    順序は入れ替えられない。``<`` を先に倒すと自分で書いた ``&lt;`` の ``&`` を
    次の段が ``&amp;lt;`` へ二重エスケープしてしまう。互換文字の開き山括弧は、
    書き込む ``&lt;`` を ``&`` の段が再エスケープしないよう、最後に倒す。

    Args:
        text: 無害化前のテキスト。

    Returns:
        既知タグ名の直前に生の ``<`` を持たず、``<`` を表す実体参照も倒した
        テキスト。``claq-memory`` を互換文字で書いた開き山括弧も倒してある。
    """
    escaped = _NEUTRALIZE_PATTERN.sub(_NEUTRALIZED_LT, _ENTITY_LT_PATTERN.sub(_NEUTRALIZED_AMP, text))
    return _escape_compat_brackets(escaped)


def _neutralization_targets(text: str) -> int:
    """無害化対象（倒すべき ``&`` と ``<``、互換文字の開き山括弧）の個数を数える。

    削除変換がタグを組み上げ直していないかの判定に使う。埋め草表現は
    非 ASCII なのでタグ名や実体参照の一部にはならず、削除は対象を**増やす**
    ことしかできない。したがって個数の増加は「削除で新しい無害化対象が
    生まれた」ことと同値になる。

    Args:
        text: 判定対象のテキスト。

    Returns:
        実体参照・ASCII の ``<``・互換文字の開き山括弧の位置の数の合計。
    """
    ascii_targets = {match.start() for match in _NEUTRALIZE_PATTERN.finditer(text)}
    return len(_ENTITY_LT_PATTERN.findall(text)) + len(ascii_targets | set(_compat_open_positions(text)))


def _erase_known_tags(text: str) -> str:
    """既知タグの表記を**除去**した判定専用の複製を返す。

    **戻り値を出力・永続化・注入へ回してはならない。** 除去は消した箇所の前後を
    接着して新しい生きたタグを作るため（モジュール docstring 参照）、この関数の
    結果は「もしタグが書かれていなければ何が見えたか」を調べるためだけに使う。

    Args:
        text: 判定対象のテキスト（生の ``<`` でも escape 済みの ``&lt;`` でもよい）。

    Returns:
        既知タグの表記を取り除いたテキスト。**出力には使わないこと。**
    """
    return _ERASE_PATTERN.sub("", text)


def _hides_secret_behind_tags(text: str) -> bool:
    """タグ表記が秘密を分断して ``redact`` から隠していないかを判定する。

    ``redact`` とタグ除去が**可換か**を見る。可換であれば、タグを外しても
    新しく見えるものは無い。可換でなければ、タグ表記が秘密の途中に割り込んで
    ``redact`` のパターンを分断している。

    マスクの個数比較では足りない。属性 512 文字超のペアタグで分断した鍵は、
    出力側でも属性そのものが ``base64_long`` で 1 個マスクされるため個数が
    釣り合い、鍵が分断されたまま素通りする（実測）。個数ではなく結果そのものを
    突き合わせる。

    消すタグが 1 つも無ければ両辺は同じ式になるため、``redact`` を走らせずに
    False を返す。``redact`` は 16 本の正規表現を通す重い処理で、``strip_tags``
    は SessionStart の同期フックが知識カードごとに呼ぶ。タグを含まない入力が
    大多数であり、そこへ 2 パス増やすと 1MB の入力で 1.2 秒（早期脱出後は
    0.03 秒）かかる。

    Args:
        text: ``strip_tags`` が返そうとしている無害化済みテキスト。

    Returns:
        タグ除去とマスクが可換でなければ True。
    """
    erased = _erase_known_tags(text)
    if erased == text:
        return False
    return _erase_known_tags(redact(text)) != redact(erased)


def _forges_tag_after_deletion(text: str) -> bool:
    """後段の削除変換がタグ・実体参照を組み上げ直せるかを判定する。

    ``slim_text.remove_filler_phrases`` は本パイプラインで唯一「行の途中から
    文字を消す」変換であり、escape の後段に置かれる（``mem/handoff`` の
    ``compact_line``）。削除で無害化対象が増えるなら、出力はそのままでは
    渡せない。

    削除するものが無ければ両辺が同じ文字列になるため、数える前に False を
    返す（``_hides_secret_behind_tags`` と同じ早期脱出）。こちらの節約は
    ミリ秒未満で、``strip_tags`` の実測コストを支配しているのは判定用複製の
    ``_ERASE_PATTERN`` である。それでも置くのは、脱出条件が同じ形だと
    「検査は入力に対象が在るときだけ走る」と 1 つの規則で読めるため。

    Args:
        text: ``strip_tags`` が返そうとしている無害化済みテキスト。

    Returns:
        埋め草削除で無害化対象が増えるなら True。
    """
    deleted = remove_filler_phrases(text)
    if deleted == text:
        return False
    return _neutralization_targets(deleted) > _neutralization_targets(text)


def _escape_every_bracket(text: str) -> str:
    """``&`` と、``<`` と見なせる文字（ASCII・全角・小書き）を 1 つ残らず倒す。

    削除変換がタグを組み上げ直せると分かった本文に使う。倒した後は削除で ``<`` も
    ``&lt;`` も作れない。互換文字の山括弧も全て倒すのは、``＜まあ/claq-memory＞`` の
    ように削除で初めて ``claq-memory`` タグになる並びを、先に壊しておくため。文字は消さない。

    Args:
        text: 無害化前のテキスト。

    Returns:
        ``&`` と全ての ``<`` 相当の文字を実体参照にしたテキスト。
    """
    escaped = text.replace("&", _NEUTRALIZED_AMP)
    for bracket in _LT_LIKE:
        escaped = escaped.replace(bracket, _NEUTRALIZED_LT)
    return escaped


def keep_allowed_chars(text: str) -> str:
    """許可した文字だけを残し、行区切りを LF に揃える。

    許可するのは Unicode 一般カテゴリの L*（文字）・M*（結合記号。結合濁点・母音記号・
    ASCII に付かない異体字セレクタを落とさない）・N*（数字）・P*（句読点）・S*（記号）・Zs（空白）と、
    タブ・LF。Cf（ZWSP・ZWJ・ZWNJ・bidi・タグ文字・BOM・ソフトハイフン）・Cc（C0・DEL・C1）・
    Co・Cn・Cs・Zl・Zp は落とす。不可視文字は鍵やタグを分断して ``redact`` / ``strip_tags``
    をすり抜ける足場になるうえ、タグ文字（U+E0000–E007F）は不可視の ASCII を混ぜられる。
    ZWJ 絵文字（👨‍👩‍👧）は ZWJ だけが落ち、構成要素の絵文字は残る。

    許可カテゴリのうち、何も表示せず ASCII の間へ挟んでタグ名や鍵を分断できる文字も落とす。
    文脈を問わず落とすのは CGJ（U+034F）・ハングル filler（U+115F・U+1160・U+3164・U+FFA0）・
    点字空白（U+2800）。異体字セレクタ（U+FE00–FE0F・U+E0100–E01EF）は、**除去後の並びで**直前が
    ASCII（文頭を含む）のときだけ落とす。``❤️``（FE0F）と ``葛󠄀``（IVS）は直前が非 ASCII なので
    残り、keycap（``[0-9#*]`` + FE0F? + U+20E3）は丸ごと残す。視認できる結合記号（U+0301・濁点・
    母音記号）は分断として見えるため対象にしない。

    行区切り（CRLF・CR・NEL・LS・PS・VT・FF）は先に LF へ揃える。``\\n`` 分割しかしない
    後段（handoff の行頭マーカー判定）が、CR や NEL の後ろの行を見落とさないため。

    **NFKC は本文の書き換えに使わない。** 全角の括弧・英数字が半角に変わり、既存カードの
    表示が変わるため。

    注入経路（handoff 節・知識カード・detach 失敗通知・learn の書き込み）は同じ信頼境界に
    あるため、除去をこの 1 関数に閉じる。呼び出し側は **``normalize_user_message`` /
    ``strip_tags`` / ``redact`` より前** に呼ぶ。後ろに置くと、不可視文字で分断した
    ``sk-ant-…`` や ``</claq-memory>`` が除去で 1 本へ再結合し、どの無害化も見ていない形で
    注入される。

    Args:
        text: 無害化前のテキスト。

    Returns:
        許可した文字だけからなるテキスト。行区切りは LF のみ。
    """
    allowed = text.replace("\r\n", "\n").translate(_ALLOWLIST_TABLE)
    return _SELECTOR_AFTER_ASCII.sub(lambda match: match.group(1) or "", allowed)


def keep_allowed_chars_for_injection(text: str) -> str:
    """注入の読み出し側で許可した文字だけを残し、再結合した鍵を伏せる。

    読み出し側は ``redact`` を掛けない（書き込み時に伏せた前提）。しかし DB へ直接書かれた行や
    書き込み時の ``redact`` を通っていない行には、ZWSP 等で分断された鍵が残りうる。分断されている
    間は ``redact`` に一致せず、除去が 1 本へ戻した瞬間に鍵が平文で注入される。そこで
    ``keep_allowed_chars`` と ``redact`` が**可換か**を調べ（``_hides_secret_behind_tags`` と
    同じ考え方）、可換でなければ除去後の本文を ``redact`` して返す。

    除く文字が無ければ ``redact`` を走らせない。通常のカードは出力が変わらず、
    ``redact`` の base64 検出が長い文字列を伏せることもない。

    Args:
        text: DB から読んだ無害化前のテキスト。

    Returns:
        許可した文字だけからなるテキスト。除去で鍵が再結合する場合は鍵を ``[REDACTED]`` にしたもの。
    """
    cleaned = keep_allowed_chars(text)
    if cleaned == text:
        return cleaned
    if keep_allowed_chars(redact(text)) == redact(cleaned):
        return cleaned
    return redact(cleaned)


def strip_tags(text: str) -> str:
    """信頼境界タグを無害化し、連続空行を詰める。

    開始・終了が対になったタグはその中身ごと除去する。除去後に残った
    対象タグ（片側だけのタグ、属性が長すぎるタグ、除去が前後の文字を接着して
    作ってしまったタグ）は、``<`` を ``&lt;`` へ置換して無害化する。除去では
    なくエスケープにすることで、無害化そのものが新しいタグを組み立てて
    しまう経路（``'<<private>/claq-memory>'`` → ``'</claq-memory>'``）を
    構造的に塞ぐ。理由の詳細はモジュール docstring を参照。

    返す直前に 2 つの検査を通し、いずれかが立てば fail closed に倒す。

    - 秘密がタグで分断されている: 本文ごと ``[REDACTED]`` にする。断片を救う
      利得より、未マスクの鍵が ``sessions.handoff`` へ永続化され、あるいは
      知識カードとして以後の全 SessionStart へ注入される損失のほうが大きい
      （``mem/cmd_context._format_injected_item`` は ``strip_tags`` の後に ``redact``
      を掛けない。書き込み時の ``redact_knowledge_text`` も分断された鍵は
      見えないため、ここが最後の関門になる）。
    - 後段の削除変換でタグを組み上げ直せる: ``&`` と ``<`` を 1 つ残らず倒す。
      文字は消えないので本文は失われない。

    Args:
        text: 無害化前のテキスト。

    Returns:
        既知タグ名の直前に生の ``<`` を含まないテキスト。前後の空白と
        3 行以上の空行は詰める。細工を検出した場合は ``[REDACTED]``、または
        ``&`` と ``<`` を全て倒した本文。
    """
    if not text:
        return text

    dropped = drop_known_tag_blocks(text)
    neutralized = _collapse_blank_lines(_neutralize(dropped))
    if _hides_secret_behind_tags(neutralized):
        return _REDACTION_MARKER
    if _forges_tag_after_deletion(neutralized):
        return _collapse_blank_lines(_escape_every_bracket(dropped))
    return neutralized
