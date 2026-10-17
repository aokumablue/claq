"""shell_tokens のシェル文字列解析（heredoc 本文の除去・改行の区切り化・トークン化）のテスト。"""

from __future__ import annotations

import pytest

from claq.hooks import shell_tokens


class TestHeredocNormalization:
    """heredoc のデータ本文をコマンド解析から外す前段パスのテスト。

    heredoc 本文はどのシェルでもコマンドの語彙に入らないが、shlex は本文行も
    通常のトークンとして返す。そのため散文が実行命令として読まれ、実測で
    3 つの保護フックが `cat > note.md <<'EOF'` の本文に書いた語だけで exit 2
    になった。ただし `bash <<'EOF'` では本文が実際に実行されるため、剥がして
    よいのは本文がデータだと静的に確定できる形だけに限る。
    """

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("sh", True),
            ("bash", True),
            ("/bin/zsh", True),
            ("dash", True),
            ("shell", False),
            ("bashrc", False),
            ("git", False),
            ("", False),
        ],
    )
    def test_is_shell_wrapper_token(self, token: str, expected: bool) -> None:
        """basename 一致であって前方一致ではないこと。"""
        assert shell_tokens.is_shell_wrapper_token(token) is expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("cat <<EOF", [("EOF", False)]),
            ("cat << EOF", [("EOF", False)]),
            ("cat <<-EOF", [("EOF", True)]),
            ("cat <<'E O F'", [("E O F", False)]),
            ('cat <<"EOF"', [("EOF", False)]),
            ("cat <<\\EOF", [("EOF", False)]),
            ("cat <<''", [("", False)]),
            ("grep x <<< 'y'", []),
            ("grep x <<<EOF", []),
            ("echo hi", []),
            ("cat <<A <<B", [("A", False), ("B", False)]),
            # 「幻の演算子」: heredoc を開始しない `<<` を演算子として採らない。
            # 採ると後続の**実行される**行が本文として捨てられ、保護フックが
            # 素通りする。
            ("# <<EOF", []),
            ("  # <<EOF", []),
            ("cat x # <<EOF", []),
            ('echo "<<EOF"', []),
            ("echo '<<EOF'", []),
            (r"echo \<<EOF", []),
            # 陰性対照: 演算子として採るべき形を巻き込んで消していないこと。
            ("echo a#b <<EOF", [("EOF", False)]),
            ("cat <<EOF # note", [("EOF", False)]),
            ("cat <<EOF > out.md", [("EOF", False)]),
            ("echo '#' && cat <<EOF", [("EOF", False)]),
        ],
    )
    def test_heredoc_delimiters(self, line: str, expected: list) -> None:
        """区切り語の抽出。`<<<`（herestring）とコメント内・クォート内は拾わない。"""
        assert shell_tokens._heredoc_delimiters(line, None) == (expected, None)

    @pytest.mark.parametrize(
        ("label", "line", "quote", "expected"),
        [
            (
                "開いたダブルクォートの内側は演算子にしない",
                "still quoted <<EOF",
                '"',
                ([], '"'),
            ),
            (
                "行内でクォートが閉じれば以降は演算子",
                'end" && cat <<EOF',
                '"',
                ([("EOF", False)], None),
            ),
            (
                "開いたシングルクォート内の \\ は literal（エスケープしない）",
                r"a\' <<EOF",
                "'",
                ([("EOF", False)], None),
            ),
            (
                "開いたダブルクォート内の \\ は次の 1 文字を消費する",
                r'a\" <<EOF',
                '"',
                ([], '"'),
            ),
            (
                "閉じないクォートは次行へ持ち越す",
                'echo "open',
                None,
                ([], '"'),
            ),
            (
                "開いたクォート内の # はコメントにならない",
                "a # b\" && cat <<EOF",
                '"',
                ([("EOF", False)], None),
            ),
        ],
    )
    def test_heredoc_delimiters_carries_quote_state(
        self, label: str, line: str, quote: str | None, expected: tuple
    ) -> None:
        """クォート状態は行をまたいで持ち越される（シェルのクォートは改行を含む）。"""
        assert shell_tokens._heredoc_delimiters(line, quote) == expected, label

    @pytest.mark.parametrize(
        ("label", "line", "expected"),
        [
            ("演算子にならない < は候補のまま照合で落ちる", "a < b", ([2], [])),
            ("herestring は候補 3 つすべてが照合で落ちる", "x <<<y", ([2, 3, 4], [])),
            ("同一演算子の 2 文字目は照合済み範囲として飛ばす", "cat <<A", ([4, 5], [("A", False)])),
            ("行末の \\ は次行へ持ち越さない", "cat <<A \\", ([4, 5], [("A", False)])),
        ],
    )
    def test_operator_start_offsets(self, label: str, line: str, expected: tuple) -> None:
        """候補位置の抽出と、そこからの非重複照合。

        候補が挙がっても `_HEREDOC_OPERATOR_RE` が一致しなければ区切り語にならない。
        1 つの演算子の 2 文字目（`<<A` の 2 つ目の `<`）は直前の照合範囲内なので
        `finditer` と同じく飛ばす。
        """
        offsets, quote = shell_tokens._operator_start_offsets(line, None)

        assert (offsets, quote) == (expected[0], None), label
        assert shell_tokens._heredoc_delimiters(line, None) == (expected[1], None), label

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("cat > note.md <<'EOF'", False),
            ("tee note.md <<'EOF'", False),
            ("bash <<'EOF'", True),
            ("sudo bash <<'EOF'", True),
            ("cat <<'EOF' | bash", True),
            ("cat <<'EOF' |", True),
            ("cat <<'EOF' &", True),
            ("cat <<'EOF' \\", True),
            ("echo bash <<'EOF'", True),
        ],
    )
    def test_line_keeps_heredoc_bodies(self, line: str, expected: bool) -> None:
        """本文を残す/剥がすの判定。疑わしい形はすべて残す側へ倒す。"""
        assert shell_tokens._line_keeps_heredoc_bodies(line) is expected

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            ("heredoc 無しは素通り", "echo 'git commit --no-verify'", "echo 'git commit --no-verify'"),
            ("herestring は素通り", "grep x <<< 'git commit'", "grep x <<< 'git commit'"),
            (
                "データ本文は剥がす",
                "cat > note.md <<'EOF'\ngit commit --no-verify\nEOF",
                "cat > note.md <<'EOF'\nEOF",
            ),
            (
                "未クォート区切りでも剥がす",
                "cat <<EOF > note.md\nprintf x > pyproject.toml\nEOF",
                "cat <<EOF > note.md\nEOF",
            ),
            (
                "シェル起動なら残す",
                "bash <<'EOF'\ngit commit --no-verify\nEOF",
                "bash <<'EOF'\ngit commit --no-verify\nEOF",
            ),
            (
                "パイプ先がシェルなら残す",
                "cat <<'EOF' | bash\ngit commit --no-verify\nEOF",
                "cat <<'EOF' | bash\ngit commit --no-verify\nEOF",
            ),
            (
                "継続演算子で終わるなら残す",
                "cat <<'EOF' |\ngit commit --no-verify\nEOF",
                "cat <<'EOF' |\ngit commit --no-verify\nEOF",
            ),
            (
                "未終端なら残す",
                "cat > note.md <<'EOF'\ngit commit --no-verify",
                "cat > note.md <<'EOF'\ngit commit --no-verify",
            ),
            (
                "終端後の後続コマンドは残る",
                "cat > n.md <<'EOF'\nprose\nEOF\ngit commit -m x",
                "cat > n.md <<'EOF'\nEOF\ngit commit -m x",
            ),
            ("同一行の二重 heredoc", "cat <<A <<B\nb1\nA\nb2\nB", "cat <<A <<B\nA\nB"),
            (
                "本文中の <<'INNER' は再検出しない",
                "cat > n.md <<'OUTER'\ncat <<'INNER'\nx\nINNER\nOUTER",
                "cat > n.md <<'OUTER'\nOUTER",
            ),
            (
                "<<- はタブを剥がして終端照合",
                "cat > n.md <<-\tEOF\n\tgit commit --no-verify\n\tEOF",
                "cat > n.md <<-\tEOF\n\t\tEOF".replace("\t\t", "\t"),
            ),
            # ここから下は docs/adr/shell-analysis-boundary.md が「未検証」「単純化されやすい」と名指しした境界。
            # CRLF: 区切り語はクォートの内側から取るため CR を含まず（`EOF`）、行側は
            # `EOF\r` なので一致しない。bash は逆に区切り語自体が CR を吸うため
            # （`<<'EOF'\r` の語は `EOF\r`）実際には終端する。挙動は一致しないが、
            # 剥がさない側は検出側であり docs/adr/shell-analysis-boundary.md の「未終端なら剥がさない」に沿う。
            (
                "CRLF は区切り語と終端行が一致せず未終端側へ倒れる",
                "cat > note.md <<'EOF'\r\ngit commit --no-verify\r\nEOF\r\n",
                "cat > note.md <<'EOF'\r\ngit commit --no-verify\r\nEOF\r\n",
            ),
            (
                "後続に空白のある終端行は終端せず本文として扱う",
                "cat > n.md <<'EOF'\nprose\nEOF \nEOF",
                "cat > n.md <<'EOF'\nEOF",
            ),
            (
                "空白付きの行しか無ければ未終端として剥がさない",
                "cat > n.md <<'EOF'\ngit commit --no-verify\nEOF ",
                "cat > n.md <<'EOF'\ngit commit --no-verify\nEOF ",
            ),
            (
                "継続演算子で改行し次行が bash でも残す",
                "cat <<'EOF' |\nbash\ngit commit --no-verify\nEOF",
                "cat <<'EOF' |\nbash\ngit commit --no-verify\nEOF",
            ),
            # 「幻の演算子」。heredoc が開始しない `<<` を演算子として採ると、
            # 実行される後続行が本文として消え、保護フック 3 種が同時に素通りする。
            (
                "コメント内の << では本文を剥がさない",
                "# <<EOF\ngit commit --no-verify -m x\nEOF",
                "# <<EOF\ngit commit --no-verify -m x\nEOF",
            ),
            (
                "ダブルクォート内の << では本文を剥がさない",
                'echo "<<EOF"\ngit commit --no-verify -m x\nEOF',
                'echo "<<EOF"\ngit commit --no-verify -m x\nEOF',
            ),
            (
                "シングルクォート内の << では本文を剥がさない",
                "echo '<<EOF'\ngit commit --no-verify -m x\nEOF",
                "echo '<<EOF'\ngit commit --no-verify -m x\nEOF",
            ),
            (
                "エスケープされた << では本文を剥がさない",
                "echo \\<<EOF\ngit commit --no-verify -m x\nEOF",
                "echo \\<<EOF\ngit commit --no-verify -m x\nEOF",
            ),
            (
                "行をまたいで開いたクォート内の << も演算子にしない",
                'echo "open\n<<EOF\ngit commit --no-verify\nEOF',
                'echo "open\n<<EOF\ngit commit --no-verify\nEOF',
            ),
            # 陰性対照: 状態追跡が本物の heredoc の本文剥がしを壊していないこと。
            (
                "演算子行の行末コメントは本文剥がしを妨げない",
                "cat > n.md <<'EOF' # note\ngit commit --no-verify\nEOF",
                "cat > n.md <<'EOF' # note\nEOF",
            ),
            (
                "本文の # とアポストロフィは状態を持ち越さない",
                "cat > n.md <<'EOF'\ndon't # note\nEOF\ngit commit --no-verify",
                "cat > n.md <<'EOF'\nEOF\ngit commit --no-verify",
            ),
            (
                "終端行が引用符を含んでも状態を持ち越さない",
                "cat > n.md <<\"E'F\"\nprose\nE'F\ngit commit --no-verify",
                "cat > n.md <<\"E'F\"\nE'F\ngit commit --no-verify",
            ),
        ],
    )
    def test_strip_data_heredoc_bodies(self, label: str, command: str, expected: str) -> None:
        """データ本文だけを落とし、実行されうる形はそのまま残す。

        末尾の境界行は docs/adr/shell-analysis-boundary.md が未検証と名指しした境界を
        固定する。CRLF 行は区切り語と一致せず未終端側へ、空白付きの `EOF ` は終端と
        認めず本文として扱い（後続に厳密一致の行があればそこで終端し、無ければ
        未終端）、継続演算子で終わる行は次行が `bash` でも本文を剥がさない。
        """
        assert shell_tokens.strip_data_heredoc_bodies(command) == expected, label

    @pytest.mark.parametrize(
        "command",
        [
            "cat > note.md <<'EOF'\nprose\nEOF",
            "bash <<'EOF'\ngit commit\nEOF",
            "cat > note.md <<'EOF'\nunterminated",
            "cat > note.md <<'EOF'\nprose\nEOF \nEOF",
            "cat > note.md <<'EOF'\r\nprose\r\nEOF\r\n",
            "echo hi",
            "# <<EOF\ngit commit --no-verify\nEOF",
            'echo "<<EOF"\ngit commit --no-verify\nEOF',
            "cat > n.md <<'EOF'\ndon't # note\nEOF\ngit commit --no-verify",
        ],
    )
    def test_strip_is_idempotent(self, command: str) -> None:
        """終端行を残す設計により再適用しても結果が変わらない。

        フックごとに適用点が異なるため、二重適用が起きても安全であることを固定する。
        """
        once = shell_tokens.strip_data_heredoc_bodies(command)

        assert shell_tokens.strip_data_heredoc_bodies(once) == once


class TestUnquotedNewlineNormalization:
    """クォート外の改行をコマンド区切りとして扱う正規化のテスト。

    改行はシェルにとって `;` と等価だが shlex は whitespace として消費するため
    区切りトークンを出さない。実測では複数行コマンドが 1 セグメントへ融合し、
    `printf x > s.py` 改行 `git add s.py` 改行 `git commit -m x` が exit 0 で通った
    （`;` 区切りの同内容は exit 2）。
    """

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            ("改行なしは素通り", "git commit -m x", "git commit -m x"),
            ("クォート外の改行は ; になる", "git add .\ngit commit -m x", "git add .;git commit -m x"),
            ("シングルクォート内の改行は保つ", "echo 'a\nb'", "echo 'a\nb'"),
            ("ダブルクォート内の改行は保つ", 'echo "a\nb"', 'echo "a\nb"'),
            ("クォートを閉じた後の改行は ; になる", "echo 'a'\ngit commit", "echo 'a';git commit"),
            ("バックスラッシュ継続は区切りにしない", "git add \\\n.", "git add \\\n."),
            ("シングルクォート内のバックスラッシュは継続にしない", "echo 'a\\'\nb", "echo 'a\\';b"),
            ("閉じないシングルクォート内の改行は保つ", "echo 'unterminated\nrm -rf /", "echo 'unterminated\nrm -rf /"),
            ('閉じないダブルクォート内の改行は保つ', 'echo "unterminated\nrm -rf /', 'echo "unterminated\nrm -rf /'),
        ],
    )
    def test_replace_unquoted_newlines(self, label: str, command: str, expected: str) -> None:
        """クォート外の改行だけを区切りへ置き換える。"""
        assert shell_tokens._replace_unquoted_newlines(command) == expected, label

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            (
                "シングルクォートが閉じない",
                "echo 'unterminated\nrm -rf /",
                (["echo", "unterminated", "rm", "-rf", "/"], False),
            ),
            (
                "ダブルクォートが閉じない",
                'echo "unterminated\nrm -rf /',
                (["echo", "unterminated", "rm", "-rf", "/"], False),
            ),
        ],
    )
    def test_tokenize_with_status_unterminated_quote(self, label: str, command: str, expected: tuple) -> None:
        """クォートが閉じない入力では改行を区切りにしないが、後続行の語は走査対象に残る。

        `_replace_unquoted_newlines` は開いたクォートの内側とみなして `;` を挿さない。
        代わりに `shlex` が `ValueError` になり空白分割へフォールバックするため、改行の
        後ろにある語もトークンとして現れる（解析できなかった印として第 2 要素は False。
        呼び出し側はこれを見て生文字列の正規表現へ倒す＝fail-closed 側になる）。
        """
        assert shell_tokens.tokenize_with_status(command) == expected, label

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            ("; が語に密着", "echo $'\\'';git commit", ["echo", "$\\", ";", "git", "commit"]),
            ("&& が語に密着", "echo $'\\''&&git commit", ["echo", "$\\", "&&", "git", "commit"]),
            ("|| が語に密着", "echo $'\\''||git commit", ["echo", "$\\", "||", "git", "commit"]),
            ("| が語に密着", "echo $'\\''|tee ruff.toml", ["echo", "$\\", "|", "tee", "ruff.toml"]),
            ("& が語に密着", "echo $'\\''&git commit", ["echo", "$\\", "&", "git", "commit"]),
            ("括弧が語に密着", "echo $'\\'';(git commit)", ["echo", "$\\", ";", "(", "git", "commit", ")"]),
            ("> が語に密着", "echo $'\\'' >ruff.toml", ["echo", "$\\", ">", "ruff.toml"]),
            (">> は 1 単位", "echo $'\\'' >>ruff.toml", ["echo", "$\\", ">>", "ruff.toml"]),
            (">| は 1 単位", "echo $'\\'' >|ruff.toml", ["echo", "$\\", ">|", "ruff.toml"]),
            ("&> は 1 単位", "echo $'\\'' &>ruff.toml", ["echo", "$\\", "&>", "ruff.toml"]),
            ("&>> は 1 単位", "echo $'\\'' &>>ruff.toml", ["echo", "$\\", "&>>", "ruff.toml"]),
            ("2> は 1 単位", "echo $'\\'' 2>ruff.toml", ["echo", "$\\", "2>", "ruff.toml"]),
            ("2>> は 1 単位", "echo $'\\'' 2>>ruff.toml", ["echo", "$\\", "2>>", "ruff.toml"]),
            ("< が語に密着", "echo $'\\'' <in.txt", ["echo", "$\\", "<", "in.txt"]),
            ("語中の 2 は割らない", "echo $'\\'' a2>x", ["echo", "$\\", "a2", ">", "x"]),
            ("1> は 1 単位", "echo $'\\'' 1>ruff.toml", ["echo", "$\\", "1>", "ruff.toml"]),
            ("1>> は 1 単位", "echo $'\\'' 1>>ruff.toml", ["echo", "$\\", "1>>", "ruff.toml"]),
            # `&` は区切りだが、`>&` / `<&` の一部としては区切りでない。`2>&1` を `2> & 1` に
            # 割ると、後続の語がセグメントごと切り離される。
            ("2>&1 は区切りを作らない", "echo $'\\'' 2>&1 x", ["echo", "$\\", "2", ">&", "1", "x"]),
            (">&2 は区切りを作らない", "echo $'\\'' >&2 x", ["echo", "$\\", ">&", "2", "x"]),
            ("<&3 は区切りを作らない", "echo $'\\'' <&3 x", ["echo", "$\\", "<&", "3", "x"]),
            ("区切りが無ければ空白分割のまま", "echo $'\\'' a b", ["echo", "$\\", "a", "b"]),
        ],
    )
    def test_tokenize_with_status_fallback_splits_glued_operators(
        self, label: str, command: str, expected: list[str]
    ) -> None:
        """空白分割へ倒れた入力でも、語に密着した区切り演算子を独立トークンにする。

        ``echo $'\\'';git commit`` は bash では有効だが `shlex` は ``ValueError`` にする。
        単純な空白分割では ``$'\\'';git`` が 1 語になり、`git` 起動が見えなくなる。
        演算子は最長一致の 1 単位で割る（``>>ruff.toml`` を ``> > ruff.toml`` にすると
        リダイレクト先の判定がずれる）。
        """
        tokens, parsed_cleanly = shell_tokens.tokenize_with_status(command)

        assert parsed_cleanly is False, label
        assert tokens == expected, label

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            ("&& 密着", "git add -A&&git commit -m x", ["git", "add", "-A", "&&", "git", "commit", "-m", "x"]),
            ("; 密着", "echo a;rm b", ["echo", "a", ";", "rm", "b"]),
            (">> 密着", "echo a>>b", ["echo", "a", ">>", "b"]),
            ("2> は数字が別トークン", "echo a 2>b", ["echo", "a", "2", ">", "b"]),
            ("&> 密着", "echo a &>b", ["echo", "a", "&>", "b"]),
            (">| 密着", "echo a>|b", ["echo", "a", ">|", "b"]),
            ("引用符内の ; は割らない", "echo 'a;b' \"c&&d\"", ["echo", "a;b", "c&&d"]),
            ("引用符内の > は割らない", "grep 'a>b' ruff.toml", ["grep", "a>b", "ruff.toml"]),
        ],
    )
    def test_tokenize_with_status_parsed_path_is_unchanged(
        self, label: str, command: str, expected: list[str]
    ) -> None:
        """`shlex` で解析できた入力のトークン列は、フォールバックの演算子分割に影響されない。"""
        assert shell_tokens.tokenize_with_status(command) == (expected, True), label

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            ("二重引用符の語", "echo $'\\''; rm \"ruff.toml\"", ["echo", "$\\", ";", "rm", "ruff.toml"]),
            ("単一引用符の語", "echo $'\\''; rm 'ruff.toml'", ["echo", "$\\", ";", "rm", "ruff.toml"]),
            ("引用符で挟まれた語頭", "echo $'\\''; \"git\" commit", ["echo", "$\\", ";", "git", "commit"]),
            ("語中の連結引用符", "echo $'\\''; g''it commit", ["echo", "$\\", ";", "git", "commit"]),
            ("リダイレクト先の引用符", "echo $'\\'' >\"ruff.toml\"", ["echo", "$\\", ">", "ruff.toml"]),
            ("閉じない引用符", "grep \"a ruff.toml", ["grep", "a", "ruff.toml"]),
            # 空文字列の引用符対も 1 トークンとして位置を保つ（個数を変えない）。
            ("空文字列の引用符対", "echo $'\\''; rm \"\" x", ["echo", "$\\", ";", "rm", "", "x"]),
        ],
    )
    def test_tokenize_with_status_fallback_strips_quote_characters(
        self, label: str, command: str, expected: list[str]
    ) -> None:
        """空白分割へ倒れた入力では、各トークンから ``"`` と ``'`` を取り除く（個数と順序は保つ）。

        ``shlex`` が解析できないとトークンに引用符が残り、保護名の照合（basename 比較）が
        ``"ruff.toml"`` に一致しない。位置ベースの書き込み先判定を保つため、取り除くのは
        文字だけでトークンの結合・分割はしない。
        """
        tokens, parsed_cleanly = shell_tokens.tokenize_with_status(command)

        assert parsed_cleanly is False, label
        assert tokens == expected, label

    def test_tokenize_with_status_parsed_path_keeps_quote_characters_inside_words(self) -> None:
        """解析できた入力では、語の中の引用符文字を取り除かない（フォールバック経路のみの処理）。"""
        assert shell_tokens.tokenize_with_status("echo \"it's\" 'say \"hi\"'") == (["echo", "it's", 'say "hi"'], True)

    def test_multiline_command_splits_into_segments(self) -> None:
        """複数行コマンドが行ごとのセグメントへ分かれること。"""
        tokens = shell_tokens.tokenize("printf x > s.py\ngit add s.py\ngit commit -m m")

        assert shell_tokens.split_segments(tokens) == [
            ["printf", "x", ">", "s.py"],
            ["git", "add", "s.py"],
            ["git", "commit", "-m", "m"],
        ]

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            (
                "エスケープした # は語の一部",
                r"echo \# && git commit --no-verify",
                ["echo", "#", "&&", "git", "commit", "--no-verify"],
            ),
            (
                "シングルクォート内の \\ は literal",
                r"echo 'a\b' # 注釈",
                ["echo", r"a\b"],
            ),
            (
                "ダブルクォート内の # はコメントにならない",
                'echo "a # b" && git commit --no-verify',
                ["echo", "a # b", "&&", "git", "commit", "--no-verify"],
            ),
        ],
    )
    def test_line_comment_stripping_respects_quotes_and_escapes(
        self, label: str, command: str, expected: list[str]
    ) -> None:
        """`#` の読み捨ては行末までで、クォート内・エスケープ後には及ばないこと。

        `shlex` のコメント処理に任せると `#` 以降が入力**末尾**まで捨てられ、
        2 行目の `git commit --no-verify` が丸ごと未検査になる。
        """
        assert shell_tokens.tokenize(command) == expected, label
