"""CI 検証モジュール（validate_agents / validate_skills / validate_commands）のテスト。"""

from __future__ import annotations

import runpy
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from claq.ci import validate_agents, validate_commands, validate_skills
from claq.ci.ci_common import extract_frontmatter

from ._support import _write_files


def _raise_oserror_on_read(monkeypatch: pytest.MonkeyPatch, broken_file: Path) -> None:
    """指定パスの Path.read_text だけ OSError にする。"""
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        """broken_file だけ読み取りに失敗させる。"""
        if self == broken_file:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def _run_commands(root: Path, **kwargs: bool) -> int:
    """root 直下の commands / agents / skills を対象に validate_commands を呼ぶ。"""
    return validate_commands.validate_commands(root, root / "commands", root / "agents", root / "skills", **kwargs)


@pytest.mark.parametrize(
    ("run", "label"),
    [
        pytest.param(lambda root, **kw: validate_agents.validate_agents(root / "agents", **kw), "agents", id="agents"),
        pytest.param(lambda root, **kw: validate_skills.validate_skills(root / "skills", **kw), "skills", id="skills"),
        pytest.param(_run_commands, "commands", id="commands"),
    ],
)
def test_missing_dir_fails_unless_optional(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], run: Callable[..., int], label: str
) -> None:
    """対象ディレクトリの欠落は既定で失敗し、optional=True のときだけスキップして 0 を返すこと。

    無条件スキップで exit 0 にすると、ディレクトリを消す・改名するだけで
    検証チェーン全体が「成功」を報告できてしまう。
    """
    assert run(tmp_path) == 1
    assert f"{label} ディレクトリが見つかりません" in capsys.readouterr().err
    assert run(tmp_path, optional=True) == 0
    assert "--optional 指定のため検証をスキップします" in capsys.readouterr().out


# --- validate_agents ---------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("---\nname: planner\ndescription: d\nmodel: sonnet\ntools: Read\n---\n# Planner\n", id="basic"),
        # tools の値自体は allowlist で縛らない。宣言の有無だけを見る。
        pytest.param("---\nname: agent\ndescription: d\ntools: SomeCustomTool\n---\n", id="custom-tool"),
        pytest.param(
            "﻿---\r\nname: planner\r\ndescription: d\r\nmodel: opus\r\ntools: Read\r\n---\r\n# Planner\r\n",
            id="bom-crlf",
        ),
        # model は任意: 未指定でも inherit でも通る。
        pytest.param("---\nname: agent\ndescription: d\ntools: Read\n---\n", id="no-model"),
        pytest.param("---\nname: agent\ndescription: d\nmodel: inherit\ntools: Read\n---\n", id="model-inherit"),
        pytest.param('---\nname: a\ndescription: d\nmodel: "sonnet"\ntools: Read\n---\n', id="model-double-quoted"),
        pytest.param("---\nname: a\ndescription: d\nmodel: 'opus'\ntools: Read\n---\n", id="model-single-quoted"),
        pytest.param('---\nname: a\ndescription: d\nmodel: " sonnet"\ntools: Read\n---\n', id="model-leading-space"),
        pytest.param('---\nname: a\ndescription: d\nmodel: "opus "\ntools: Read\n---\n', id="model-trailing-space"),
        pytest.param("---\nname: a\ndescription: d\nmodel: ' haiku '\ntools: Read\n---\n", id="model-both-spaces"),
    ],
)
def test_validate_agents_accepts(tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str) -> None:
    """name / description / tools を宣言したエージェントは通ること。"""
    _write_files(tmp_path, {"agents/agent.md": content})

    assert validate_agents.validate_agents(tmp_path / "agents") == 0
    assert "1 個のエージェントファイルを検証しました" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        # tools 未宣言は暗黙の全権継承になるため必須。
        pytest.param("name: agent\ndescription: d", "tools が宣言されていません", id="no-tools"),
        # validate_skills と同じ 2 項目を必須にする（docs/adr/definition-and-subagent-design.md）。
        pytest.param("description: d\ntools: Read", "name が宣言されていません", id="no-name"),
        pytest.param("name: agent\ntools: Read", "description が宣言されていません", id="no-description"),
    ],
)
def test_validate_agents_rejects_missing_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], frontmatter: str, message: str
) -> None:
    """必須の frontmatter 項目が欠けたエージェントは失敗すること。"""
    _write_files(tmp_path, {"agents/partial.md": f"---\n{frontmatter}\n---\n# Partial\n"})

    assert validate_agents.validate_agents(tmp_path / "agents") == 1
    assert message in capsys.readouterr().err


def test_validate_agents_reports_every_broken_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """frontmatter 欠落と読み取り失敗を短絡せずに両方報告すること。"""
    _write_files(tmp_path, {"agents/missing_frontmatter.md": "plain text", "agents/broken.md": "---\n---\n"})
    _raise_oserror_on_read(monkeypatch, tmp_path / "agents" / "broken.md")

    assert validate_agents.validate_agents(tmp_path / "agents") == 1
    stderr = capsys.readouterr().err
    assert "フロントマターがありません" in stderr
    assert "ファイルの読み取りに失敗しました" in stderr


def test_extract_frontmatter_lenient_parsing() -> None:
    """コロンの無い行は読み飛ばし、トップレベルが辞書でなければ None を返すこと。"""
    assert extract_frontmatter("---\nname: x\nnocolon\n---\nbody") == {"name": "x"}
    assert extract_frontmatter("---\n- item\n---\nbody") is None


# --- validate_skills ---------------------------------------------------------


@pytest.mark.parametrize(
    "frontmatter",
    [
        pytest.param("name: planner\ndescription: いつ呼ぶかの説明", id="basic"),
        # 引用符なしの true / false は通る（正しい書き方を落とさない）。
        pytest.param(
            "name: planner\ndescription: 説明\nuser-invocable: true\ndisable-model-invocation: false",
            id="real-booleans",
        ),
    ],
)
def test_validate_skills_accepts(tmp_path: Path, capsys: pytest.CaptureFixture[str], frontmatter: str) -> None:
    """正しい frontmatter のスキルディレクトリを全件検証して通ること。"""
    skill_md = f"---\n{frontmatter}\n---\n\n# Skill\n"
    _write_files(tmp_path, {"skills/alpha/SKILL.md": skill_md, "skills/beta/SKILL.md": skill_md})

    assert validate_skills.validate_skills(tmp_path / "skills") == 0
    assert "2 個のスキルディレクトリを検証しました" in capsys.readouterr().out


_SKILL_HEAD = "---\nname: planner\ndescription: 説明\n"


@pytest.mark.parametrize(
    ("skill_md", "message"),
    [
        pytest.param(None, "broken/ - SKILL.md が見つかりません", id="no-skill-md"),
        # agents と同じく frontmatter を検査する（「存在する・読める・空でない」だけでは非対称）。
        pytest.param("# heading only\n", "フロントマターがありません", id="no-frontmatter"),
        pytest.param("---\nname: half\n---\n\n# Half\n", "description がありません", id="no-description"),
        # ホストは真の bool しか見ない。"true" / yes / 1 は宣言したつもりで効かず、
        # disable-model-invocation が効かなければスラッシュ起動専用の skill が自動発火へ戻る。
        pytest.param(f'{_SKILL_HEAD}disable-model-invocation: "true"\n---\n', "真偽値でない", id="quoted-true"),
        pytest.param(f"{_SKILL_HEAD}disable-model-invocation: yes\n---\n", "真偽値でない", id="yes"),
        pytest.param(f"{_SKILL_HEAD}disable-model-invocation: 1\n---\n", "真偽値でない", id="number"),
        # 綴り違いは自前パーサが「別のキー」として黙って受理し、宣言が丸ごと無視される。
        pytest.param(
            f"{_SKILL_HEAD}disable_model_invocation: true\n---\n",
            "disable_model_invocation は disable-model-invocation の綴り違い",
            id="underscore",
        ),
    ],
)
def test_validate_skills_rejects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], skill_md: str | None, message: str
) -> None:
    """SKILL.md の欠落・frontmatter 欠落・必須項目欠落・真偽値の型違い・綴り違いを失敗にすること。"""
    skill_dir = tmp_path / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    if skill_md is not None:
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    assert validate_skills.validate_skills(tmp_path / "skills") == 1
    assert message in capsys.readouterr().err


def test_validate_skills_reports_every_broken_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """空の SKILL.md と読み取り失敗を短絡せずに両方報告すること。"""
    _write_files(tmp_path, {"skills/empty/SKILL.md": "", "skills/broken/SKILL.md": "broken"})
    _raise_oserror_on_read(monkeypatch, tmp_path / "skills" / "broken" / "SKILL.md")

    assert validate_skills.validate_skills(tmp_path / "skills") == 1
    stderr = capsys.readouterr().err
    assert "SKILL.md - ファイルが空です" in stderr
    assert "SKILL.md - ファイルの読み取りに失敗しました" in stderr


def test_quick_skills_stay_slash_only() -> None:
    """quick 系が `disable-model-invocation: true` を宣言し続けること。

    この宣言が落ちると skill は黙って自動発火へ戻る。人間が実行を担うと決めた
    ときだけ入るモードなので、モデルの推測で入ってはならない。型検査は
    test_validate_skills_rejects が持つので、ここは**宣言が実在すること**だけを見る。
    """
    skills = Path(__file__).resolve().parents[2] / "skills"
    quick = sorted(skills.glob("quick-*/SKILL.md"))
    assert len(quick) >= 5, f"quick 系が {len(quick)} 件しか見つからない"
    lapsed = [
        p.parent.name
        for p in quick
        if (extract_frontmatter(p.read_text(encoding="utf-8")) or {}).get("disable-model-invocation") is not True
    ]
    assert lapsed == [], f"自動発火へ戻っている quick 系: {lapsed}"


# --- validate_commands -------------------------------------------------------


def test_list_markdown_files(tmp_path: Path) -> None:
    """無いディレクトリは空リスト、ある場合は直下の .md だけを返すこと。"""
    assert validate_commands._list_markdown_files(tmp_path / "missing") == []
    _write_files(tmp_path, {"commands/keep.md": "ok\n", "commands/ignore.txt": "ignore\n"})
    assert [path.name for path in validate_commands._list_markdown_files(tmp_path / "commands")] == ["keep.md"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`/plain`", ["plain"]),
        ("`/with-hyphen`", ["with-hyphen"]),
        ("`/instinct promote`", ["instinct"]),
        ("`/instinct promote <key>`", ["instinct"]),
        ("`/first arg` と `/second`", ["first", "second"]),
        # 引数部は空白始まりに限るので、パス表記は一致しない。
        ("`/usr/bin/env python`", []),
        ("`/tmp/x`", []),
    ],
)
def test_command_reference_pattern_catches_arguments(text: str, expected: list[str]) -> None:
    """引数付きのコマンド参照も名前を取り出せること。

    閉じバッククォートが名前の直後に来る形だけを見ると、
    ``/does-not-exist arg`` が素通りし、同じ行の ``/also-missing`` だけが
    エラーになる。
    """
    assert [m.group(1) for m in validate_commands._COMMAND_REFERENCE_PATTERN.finditer(text)] == expected


def test_validate_commands_warns_on_missing_skill_but_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """creates: 行とコードブロック内の参照は検査せず、ローカルに無い skill 参照は警告だけで通ること。"""
    (tmp_path / "skills" / "clean").mkdir(parents=True)
    _write_files(
        tmp_path,
        {
            "commands/clean.md": "Clean command.\n",
            "agents/clean.md": "Agent.\n",
            "agents/reviewer.md": "Agent.\n",
            "commands/build.md": "Use `/clean` and agents/clean.md.\n"
            "clean -> reviewer\n"
            "creates: `/missing`\n"
            "skills/clean/docs\n"
            "skills/missing/docs\n"
            "```bash\n/missing-inside-code\nagents/missing.md\n```\n",
        },
    )

    assert _run_commands(tmp_path) == 0
    assert "1 件の警告" in capsys.readouterr().out


def test_validate_commands_reports_errors_and_io_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """空ファイル・読み取り失敗・存在しないコマンド（引数付き含む）/エージェント/ワークフロー参照を全て報告すること。"""
    (tmp_path / "skills").mkdir()
    _write_files(
        tmp_path,
        {
            "commands/existing.md": "Existing command.\n",
            "commands/empty.md": "",
            "commands/broken.md": "Broken command.\n",
            "agents/existing.md": "Agent.\n",
            "commands/bad.md": "Use `/missing` and agents/missing.md.\n"
            "existing -> missing\n"
            "Run `/does-not-exist arg` first.\n",
        },
    )
    _raise_oserror_on_read(monkeypatch, tmp_path / "commands" / "broken.md")

    assert _run_commands(tmp_path) == 1
    stderr = capsys.readouterr().err
    assert "コマンドファイルが空です" in stderr
    assert "ファイルの読み取りに失敗しました" in stderr
    assert "存在しないコマンド /missing" in stderr
    assert "存在しないコマンド /does-not-exist" in stderr
    assert "存在しないエージェント agents/missing.md" in stderr
    assert "存在しないエージェント \"missing\"" in stderr


def test_validate_commands_resolves_relative_dirs_via_root_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """相対指定のディレクトリは root_dir 基準で解決すること。"""
    (tmp_path / "agents").mkdir()
    (tmp_path / "skills").mkdir()
    _write_files(tmp_path, {"commands/test.md": "Test command.\n"})

    assert validate_commands.validate_commands(tmp_path, "commands", "agents", "skills") == 0
    assert "1 個のコマンドファイルを検証しました" in capsys.readouterr().out


def test_validate_commands_main_accepts_optional_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI からも `--optional` でスキップできること。"""
    argv = ["--optional", "--root-dir", str(tmp_path)]
    for name in ("commands", "agents", "skills"):
        argv += [f"--{name}-dir", str(tmp_path / name)]

    assert validate_commands.main(argv) == 0
    assert "--optional 指定のため検証をスキップします" in capsys.readouterr().out


# --- CLI エントリポイント -----------------------------------------------------


@pytest.mark.parametrize(
    ("module", "dirs", "noun"),
    [
        pytest.param(validate_skills, ("skills",), "スキルディレクトリ", id="skills"),
        pytest.param(validate_agents, ("agents",), "エージェントファイル", id="agents"),
        pytest.param(validate_commands, ("root", "commands", "agents", "skills"), "コマンドファイル", id="commands"),
    ],
)
def test_main_and_module_entrypoint_succeed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    dirs: tuple[str, ...],
    noun: str,
) -> None:
    """main() と `python -m` 実行の両方が正しいツリーで 0 を返すこと。"""
    _write_files(
        tmp_path,
        {
            "skills/alpha/SKILL.md": "---\nname: alpha\ndescription: いつ呼ぶかの説明\n---\n\n# Skill\n",
            "agents/alpha.md": "---\nname: alpha\ndescription: d\nmodel: sonnet\ntools: Read\n---\n",
            "commands/alpha.md": "Use `/alpha`.\n",
        },
    )
    argv = [arg for name in dirs for arg in (f"--{name}-dir", str(tmp_path if name == "root" else tmp_path / name))]

    assert module.main(argv) == 0
    assert f"1 個の{noun}を検証しました" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module(module.__name__, run_name="__main__")
    assert excinfo.value.code == 0
