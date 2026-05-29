"""
リポジトリ内の言語とフレームワークを検出します。
ファイル名、依存関係、ファイル内容を組み合わせて判定し、テストやビルドの既定コマンドも推定します。
プロジェクト種別に応じた後続処理の分岐に使います。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LanguageRule:
    """プログラミング言語を検出するためのルール。"""

    name: str
    extensions: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)


@dataclass
class FrameworkRule:
    """フレームワークを検出するためのルール。"""

    name: str
    language: str
    files: list[str] = field(default_factory=list)
    file_patterns: list[str] = field(default_factory=list)
    file_contents: list[dict[str, str]] = field(default_factory=list)
    package_json: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    cargo_toml: list[str] = field(default_factory=list)
    go_mod: list[str] = field(default_factory=list)
    gemfile: list[str] = field(default_factory=list)
    composer_json: list[str] = field(default_factory=list)
    pubspec: list[str] = field(default_factory=list)
    pom_xml: list[str] = field(default_factory=list)
    gradle: list[str] = field(default_factory=list)
    csproj: list[str] = field(default_factory=list)


# 言語検出ルール
LANGUAGE_RULES: list[LanguageRule] = [
    LanguageRule(
        name="javascript",
        extensions=[".js", ".mjs", ".cjs"],
        files=["package.json", ".eslintrc", ".eslintrc.js", ".eslintrc.json"],
    ),
    LanguageRule(
        name="typescript",
        extensions=[".ts", ".tsx"],
        files=["tsconfig.json", "tsconfig.base.json"],
    ),
    LanguageRule(
        name="python",
        extensions=[".py", ".pyi"],
        files=[
            "requirements.txt",
            "setup.py",
            "pyproject.toml",
            "Pipfile",
            "poetry.lock",
        ],
    ),
    LanguageRule(
        name="ruby",
        extensions=[".rb", ".rake"],
        files=["Gemfile", "Rakefile", ".ruby-version"],
    ),
    LanguageRule(
        name="go",
        extensions=[".go"],
        files=["go.mod", "go.sum"],
    ),
    LanguageRule(
        name="rust",
        extensions=[".rs"],
        files=["Cargo.toml", "Cargo.lock"],
    ),
    LanguageRule(
        name="java",
        extensions=[".java"],
        files=["pom.xml", "build.gradle", "build.gradle.kts"],
    ),
    LanguageRule(
        name="kotlin",
        extensions=[".kt", ".kts"],
        files=["build.gradle.kts"],
    ),
    LanguageRule(
        name="swift",
        extensions=[".swift"],
        files=["Package.swift", "*.xcodeproj", "*.xcworkspace"],
    ),
    LanguageRule(
        name="c",
        extensions=[".c", ".h"],
        files=["Makefile", "CMakeLists.txt"],
    ),
    LanguageRule(
        name="cpp",
        extensions=[".cpp", ".cxx", ".cc", ".hpp", ".hxx"],
        files=["CMakeLists.txt", "Makefile"],
    ),
    LanguageRule(
        name="csharp",
        extensions=[".cs"],
        files=["*.csproj", "*.sln"],
    ),
    LanguageRule(
        name="php",
        extensions=[".php"],
        files=["composer.json", "composer.lock"],
    ),
    LanguageRule(
        name="dart",
        extensions=[".dart"],
        files=["pubspec.yaml", "pubspec.lock"],
    ),
    LanguageRule(
        name="elixir",
        extensions=[".ex", ".exs"],
        files=["mix.exs", "mix.lock"],
    ),
    LanguageRule(
        name="scala",
        extensions=[".scala", ".sc"],
        files=["build.sbt", "build.sc"],
    ),
    LanguageRule(
        name="haskell",
        extensions=[".hs", ".lhs"],
        files=["*.cabal", "stack.yaml", "cabal.project"],
    ),
    LanguageRule(
        name="ocaml",
        extensions=[".ml", ".mli"],
        files=["dune", "dune-project", "*.opam"],
    ),
    LanguageRule(
        name="lua",
        extensions=[".lua"],
        files=["*.rockspec", ".luacheckrc"],
    ),
    LanguageRule(
        name="perl",
        extensions=[".pl", ".pm"],
        files=["Makefile.PL", "cpanfile"],
    ),
    LanguageRule(
        name="r",
        extensions=[".R", ".r", ".Rmd"],
        files=["DESCRIPTION", ".Rprofile"],
    ),
    LanguageRule(
        name="shell",
        extensions=[".sh", ".bash", ".zsh"],
        files=[".bashrc", ".zshrc"],
    ),
    LanguageRule(
        name="powershell",
        extensions=[".ps1", ".psm1", ".psd1"],
        files=[],
    ),
]

# フレームワーク検出ルール
FRAMEWORK_RULES: list[FrameworkRule] = [
    # JavaScript/TypeScript フレームワーク
    FrameworkRule(
        name="react",
        language="javascript",
        package_json=["react", "react-dom"],
        files=["src/App.jsx", "src/App.tsx"],
    ),
    FrameworkRule(
        name="next.js",
        language="javascript",
        package_json=["next"],
        files=["next.config.js", "next.config.mjs", "pages/_app.js", "app/layout.tsx"],
    ),
    FrameworkRule(
        name="vue",
        language="javascript",
        package_json=["vue"],
        files=["vue.config.js", "vite.config.ts"],
    ),
    FrameworkRule(
        name="nuxt",
        language="javascript",
        package_json=["nuxt"],
        files=["nuxt.config.js", "nuxt.config.ts"],
    ),
    FrameworkRule(
        name="angular",
        language="javascript",
        package_json=["@angular/core"],
        files=["angular.json", ".angular.json"],
    ),
    FrameworkRule(
        name="svelte",
        language="javascript",
        package_json=["svelte"],
        files=["svelte.config.js"],
    ),
    FrameworkRule(
        name="express",
        language="javascript",
        package_json=["express"],
    ),
    FrameworkRule(
        name="nestjs",
        language="javascript",
        package_json=["@nestjs/core"],
        files=["nest-cli.json"],
    ),
    FrameworkRule(
        name="electron",
        language="javascript",
        package_json=["electron"],
        files=["electron.js", "main.js"],
    ),
    FrameworkRule(
        name="remix",
        language="javascript",
        package_json=["@remix-run/react"],
        files=["remix.config.js"],
    ),
    FrameworkRule(
        name="gatsby",
        language="javascript",
        package_json=["gatsby"],
        files=["gatsby-config.js"],
    ),
    FrameworkRule(
        name="astro",
        language="javascript",
        package_json=["astro"],
        files=["astro.config.mjs"],
    ),
    FrameworkRule(
        name="jest",
        language="javascript",
        package_json=["jest"],
        files=["jest.config.js", "jest.config.ts"],
    ),
    FrameworkRule(
        name="vitest",
        language="javascript",
        package_json=["vitest"],
        files=["vitest.config.ts"],
    ),
    FrameworkRule(
        name="cypress",
        language="javascript",
        package_json=["cypress"],
        files=["cypress.config.js", "cypress.config.ts", "cypress.json"],
    ),
    # Python フレームワーク
    FrameworkRule(
        name="django",
        language="python",
        requirements=["django", "Django"],
        files=["manage.py", "settings.py"],
        file_contents=[{"file": "manage.py", "pattern": "django"}],
    ),
    FrameworkRule(
        name="flask",
        language="python",
        requirements=["flask", "Flask"],
        file_contents=[{"file": "*.py", "pattern": "from flask"}],
    ),
    FrameworkRule(
        name="fastapi",
        language="python",
        requirements=["fastapi", "FastAPI"],
        file_contents=[{"file": "*.py", "pattern": "from fastapi"}],
    ),
    FrameworkRule(
        name="pytest",
        language="python",
        requirements=["pytest"],
        files=["pytest.ini", "pyproject.toml", "conftest.py"],
    ),
    FrameworkRule(
        name="sqlalchemy",
        language="python",
        requirements=["sqlalchemy", "SQLAlchemy"],
    ),
    FrameworkRule(
        name="celery",
        language="python",
        requirements=["celery", "Celery"],
    ),
    FrameworkRule(
        name="pydantic",
        language="python",
        requirements=["pydantic"],
    ),
    # Ruby フレームワーク
    FrameworkRule(
        name="rails",
        language="ruby",
        gemfile=["rails"],
        files=["config/routes.rb", "app/controllers/application_controller.rb"],
    ),
    FrameworkRule(
        name="sinatra",
        language="ruby",
        gemfile=["sinatra"],
    ),
    FrameworkRule(
        name="rspec",
        language="ruby",
        gemfile=["rspec", "rspec-rails"],
        files=[".rspec", "spec/spec_helper.rb"],
    ),
    FrameworkRule(
        name="minitest",
        language="ruby",
        gemfile=["minitest"],
        files=["test/test_helper.rb"],
    ),
    # Go フレームワーク
    FrameworkRule(
        name="gin",
        language="go",
        go_mod=["github.com/gin-gonic/gin"],
    ),
    FrameworkRule(
        name="echo",
        language="go",
        go_mod=["github.com/labstack/echo"],
    ),
    FrameworkRule(
        name="fiber",
        language="go",
        go_mod=["github.com/gofiber/fiber"],
    ),
    FrameworkRule(
        name="cobra",
        language="go",
        go_mod=["github.com/spf13/cobra"],
    ),
    # Rust フレームワーク
    FrameworkRule(
        name="actix",
        language="rust",
        cargo_toml=["actix-web"],
    ),
    FrameworkRule(
        name="axum",
        language="rust",
        cargo_toml=["axum"],
    ),
    FrameworkRule(
        name="tokio",
        language="rust",
        cargo_toml=["tokio"],
    ),
    # Java フレームワーク
    FrameworkRule(
        name="spring",
        language="java",
        pom_xml=["spring-boot", "spring-core"],
        gradle=["org.springframework"],
        files=["src/main/resources/application.properties", "src/main/resources/application.yml"],
    ),
    FrameworkRule(
        name="junit",
        language="java",
        pom_xml=["junit"],
        gradle=["junit"],
    ),
    # PHP フレームワーク
    FrameworkRule(
        name="laravel",
        language="php",
        composer_json=["laravel/framework"],
        files=["artisan", "app/Http/Kernel.php"],
    ),
    FrameworkRule(
        name="symfony",
        language="php",
        composer_json=["symfony/framework-bundle"],
        files=["symfony.lock"],
    ),
    FrameworkRule(
        name="wordpress",
        language="php",
        files=["wp-config.php", "wp-content/themes"],
    ),
    # Dart/Flutter
    FrameworkRule(
        name="flutter",
        language="dart",
        pubspec=["flutter"],
        files=["lib/main.dart", "android/app/build.gradle"],
    ),
    # C#/.NET フレームワーク
    FrameworkRule(
        name="aspnet",
        language="csharp",
        csproj=["Microsoft.AspNetCore"],
        files=["Program.cs", "Startup.cs"],
    ),
    FrameworkRule(
        name="blazor",
        language="csharp",
        csproj=["Microsoft.AspNetCore.Components"],
    ),
    # Elixir フレームワーク
    FrameworkRule(
        name="phoenix",
        language="elixir",
        files=["lib/*_web/router.ex", "config/config.exs"],
        file_contents=[{"file": "mix.exs", "pattern": "phoenix"}],
    ),
]


def detect_languages(project_root: str | Path) -> list[str]:
    """プロジェクトで使われているプログラミング言語を検出する。

    Args:
        project_root: project_root の値

    Returns:
        list[str]: str の一覧を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)
    if not root.exists():
        return []

    detected: set[str] = set()

    # ルートディレクトリのファイルを収集する（速度のため非再帰）
    try:
        root_files = {f.name for f in root.iterdir() if f.is_file()}
    except (PermissionError, OSError):
        root_files = set()

    for rule in LANGUAGE_RULES:
        # マーカーファイルを確認
        for marker_file in rule.files:
            if "*" in marker_file:
                # グロブパターンを処理
                pattern = marker_file
                if any(root.glob(pattern)):
                    detected.add(rule.name)
                    break
            elif marker_file in root_files:
                detected.add(rule.name)
                break

    # 拡張子の高速スキャン（性能のため深さを制限）
    extension_languages: dict[str, str] = {}
    for rule in LANGUAGE_RULES:
        for ext in rule.extensions:
            extension_languages[ext] = rule.name

    for file_path in _limited_file_scan(root, max_depth=3, max_files=1000):
        ext = file_path.suffix
        if ext in extension_languages:
            detected.add(extension_languages[ext])

    return sorted(detected)


def _limited_file_scan(
    root: Path,
    max_depth: int = 3,
    max_files: int = 1000,
) -> list[Path]:
    """深さと件数の上限付きでファイルを走査する。

    Args:
        root: root の値
        max_depth: 探索する最大深さ
        max_files: 返す最大ファイル数

    Returns:
        list[Path]: Path の一覧を返します。

    Raises:
        例外は発生しません。
    """
    files: list[Path] = []

    def scan(directory: Path, depth: int) -> None:
        """ディレクトリを再帰的に探索し、見つかったファイルを外側の files リストに追加する。

        Args:
            directory: 探索対象ディレクトリ。
            depth: 現在の探索深さ。

        Returns:
            None: 結果は外側の files リストに追加されます。

        Raises:
            例外は発生しません。
        """
        if depth > max_depth or len(files) >= max_files:
            return

        try:
            entries = list(directory.iterdir())
        except (PermissionError, OSError):
            return

        for entry in entries:
            if len(files) >= max_files:
                return

            if entry.name.startswith("."):
                continue
            if entry.name == "node_modules":
                continue
            if entry.name == "__pycache__":
                continue
            if entry.name == "venv" or entry.name == ".venv":
                continue

            if entry.is_file():
                files.append(entry)
            elif entry.is_dir():
                scan(entry, depth + 1)

    scan(root, 0)
    return files


def _read_json_file(path: Path) -> dict[str, Any]:
    """JSONファイルを読み込んで解析し、エラー時は空辞書を返す。

    Args:
        path: path の値

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_text_file(path: Path) -> str:
    """テキストファイルを読み込み、エラー時は空文字列を返す。

    Args:
        path: path の値

    Returns:
        str: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _check_package_json_deps(root: Path, deps: list[str]) -> bool:
    """package.json に指定依存関係のいずれかが含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    package_json = root / "package.json"
    if not package_json.exists():
        return False

    data = _read_json_file(package_json)
    all_deps: set[str] = set()

    for key in ["dependencies", "devDependencies", "peerDependencies"]:
        if key in data and isinstance(data[key], dict):
            all_deps.update(data[key].keys())

    return any(dep in all_deps for dep in deps)


def _check_requirements_deps(root: Path, deps: list[str]) -> bool:
    """requirements.txt または pyproject.toml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    # requirements.txt を確認
    requirements = root / "requirements.txt"
    if requirements.exists():
        content = _read_text_file(requirements).lower()
        if any(dep.lower() in content for dep in deps):
            return True

    # pyproject.toml を確認
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        content = _read_text_file(pyproject).lower()
        if any(dep.lower() in content for dep in deps):
            return True

    # Pipfile を確認
    pipfile = root / "Pipfile"
    if pipfile.exists():
        content = _read_text_file(pipfile).lower()
        if any(dep.lower() in content for dep in deps):
            return True

    return False


def _check_cargo_toml_deps(root: Path, deps: list[str]) -> bool:
    """Cargo.toml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    cargo = root / "Cargo.toml"
    if not cargo.exists():
        return False

    content = _read_text_file(cargo)
    return any(dep in content for dep in deps)


def _check_go_mod_deps(root: Path, deps: list[str]) -> bool:
    """go.mod に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    go_mod = root / "go.mod"
    if not go_mod.exists():
        return False

    content = _read_text_file(go_mod)
    return any(dep in content for dep in deps)


def _check_gemfile_deps(root: Path, deps: list[str]) -> bool:
    """Gemfile に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    gemfile = root / "Gemfile"
    if not gemfile.exists():
        return False

    content = _read_text_file(gemfile)
    return any(dep in content for dep in deps)


def _is_rails_app(root: Path) -> bool:
    """Rails アプリかどうかを判定する。

    Rails も Minitest を ``test/test_helper.rb`` で使うため、素の Minitest
    プロジェクトと区別する目的で Rails 固有マーカーの有無を確認する。
    detect_frameworks の rails ルール（Gemfile の rails / config/routes.rb /
    app/controllers/application_controller.rb）と同じシグナルを用いる。

    Args:
        root: プロジェクトルートのパス。

    Returns:
        bool: Rails マーカーが見つかれば True、そうでなければ False。

    Raises:
        例外は発生しません。
    """
    has_rails_gem = _check_gemfile_deps(root, ["rails"])
    has_routes = (root / "config" / "routes.rb").exists()
    has_app_controller = (root / "app" / "controllers" / "application_controller.rb").exists()
    return has_rails_gem or has_routes or has_app_controller


def _check_composer_json_deps(root: Path, deps: list[str]) -> bool:
    """composer.json に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    composer = root / "composer.json"
    if not composer.exists():
        return False

    data = _read_json_file(composer)
    all_deps: set[str] = set()

    for key in ["require", "require-dev"]:
        if key in data and isinstance(data[key], dict):
            all_deps.update(data[key].keys())

    return any(dep in all_deps for dep in deps)


def _check_pubspec_deps(root: Path, deps: list[str]) -> bool:
    """pubspec.yaml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    pubspec = root / "pubspec.yaml"
    if not pubspec.exists():
        return False

    content = _read_text_file(pubspec)
    return any(dep in content for dep in deps)


def _check_pom_xml_deps(root: Path, deps: list[str]) -> bool:
    """pom.xml に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    pom = root / "pom.xml"
    if not pom.exists():
        return False

    content = _read_text_file(pom)
    return any(dep in content for dep in deps)


def _check_gradle_deps(root: Path, deps: list[str]) -> bool:
    """build.gradle または build.gradle.kts に依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    for gradle_file in ["build.gradle", "build.gradle.kts"]:
        gradle = root / gradle_file
        if gradle.exists():
            content = _read_text_file(gradle)
            if any(dep in content for dep in deps):
                return True

    return False


def _check_csproj_deps(root: Path, deps: list[str]) -> bool:
    """いずれかの .csproj ファイルに依存関係が含まれるか確認する。

    Args:
        root: root の値
        deps: deps の値

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    for csproj in root.glob("*.csproj"):
        content = _read_text_file(csproj)
        if any(dep in content for dep in deps):
            return True

    return False


def _check_file_contents(root: Path, patterns: list[dict[str, str]]) -> bool:
    """ファイルに指定パターンが含まれるか確認する。

    Args:
        root: root の値
        patterns: 検索パターンの一覧

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    for pattern_spec in patterns:
        file_pattern = pattern_spec.get("file", "")
        search_pattern = pattern_spec.get("pattern", "")

        if "*" in file_pattern:
            for file_path in root.glob(file_pattern):
                if file_path.is_file():
                    content = _read_text_file(file_path)
                    if search_pattern in content:
                        return True
        else:
            file_path = root / file_pattern
            if file_path.exists():
                content = _read_text_file(file_path)
                if search_pattern in content:
                    return True

    return False


def detect_frameworks(
    project_root: str | Path,
    detected_languages: list[str] | None = None,
) -> list[str]:
    """プロジェクトで使われているフレームワークを検出する。

    Args:
        project_root: project_root の値
        detected_languages: detected_languages の値

    Returns:
        list[str]: str の一覧を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)
    if not root.exists():
        return []

    if detected_languages is None:
        detected_languages = detect_languages(project_root)

    detected: set[str] = set()

    for rule in FRAMEWORK_RULES:
        # フレームワークの言語が未検出ならスキップ
        if rule.language not in detected_languages:
            continue

        # マーカーファイルを確認
        for marker_file in rule.files:
            if "*" in marker_file:
                if any(root.glob(marker_file)):
                    detected.add(rule.name)
                    break
            elif (root / marker_file).exists():
                detected.add(rule.name)
                break

        if rule.name in detected:
            continue

        # 言語に応じた依存ファイルを確認
        if rule.package_json and _check_package_json_deps(root, rule.package_json):
            detected.add(rule.name)
        elif rule.requirements and _check_requirements_deps(root, rule.requirements):
            detected.add(rule.name)
        elif rule.cargo_toml and _check_cargo_toml_deps(root, rule.cargo_toml):
            detected.add(rule.name)
        elif rule.go_mod and _check_go_mod_deps(root, rule.go_mod):
            detected.add(rule.name)
        elif rule.gemfile and _check_gemfile_deps(root, rule.gemfile):
            detected.add(rule.name)
        elif rule.composer_json and _check_composer_json_deps(root, rule.composer_json):
            detected.add(rule.name)
        elif rule.pubspec and _check_pubspec_deps(root, rule.pubspec):
            detected.add(rule.name)
        elif rule.pom_xml and _check_pom_xml_deps(root, rule.pom_xml):
            detected.add(rule.name)
        elif rule.gradle and _check_gradle_deps(root, rule.gradle):
            detected.add(rule.name)
        elif rule.csproj and _check_csproj_deps(root, rule.csproj):
            detected.add(rule.name)

        if rule.name in detected:
            continue

        # ファイル内容を確認
        if rule.file_contents and _check_file_contents(root, rule.file_contents):
            detected.add(rule.name)

    return sorted(detected)


@dataclass
class ProjectInfo:
    """検出されたプロジェクト情報。"""

    root: Path
    languages: list[str]
    frameworks: list[str]
    primary_language: str | None = None


def detect_project(project_root: str | Path) -> ProjectInfo:
    """プロジェクト情報全体を検出する。

    Args:
        project_root: project_root の値

    Returns:
        ProjectInfo: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root).resolve()
    languages = detect_languages(root)
    frameworks = detect_frameworks(root, languages)

    # 主要言語を決定（最も多い言語、または最初に検出された言語）
    primary = languages[0] if languages else None

    return ProjectInfo(
        root=root,
        languages=languages,
        frameworks=frameworks,
        primary_language=primary,
    )


def get_test_command(project_root: str | Path) -> str | None:
    """プロジェクトに適したテストコマンドを取得する。

    Args:
        project_root: project_root の値

    Returns:
        str | None: str を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)

    # package.json の scripts を確認
    package_json = root / "package.json"
    if package_json.exists():
        data = _read_json_file(package_json)
        scripts = data.get("scripts", {})
        if "test" in scripts:
            return "npm test"
        if "tests" in scripts:
            return "npm run tests"

    # Python
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return "pytest"
    if (root / "pyproject.toml").exists():
        content = _read_text_file(root / "pyproject.toml")
        if "pytest" in content:
            return "pytest"

    # Ruby — RSpec を優先し、test/ は Rails と素の Minitest を区別する
    if (root / ".rspec").exists() or (root / "spec").is_dir():
        return "rspec"
    if (root / "test" / "test_helper.rb").exists():
        # Rails も Minitest を test/test_helper.rb で使うが実行は `rails test`。
        # Rails マーカーが無ければ素の Minitest プロジェクトとみなし `rake test`。
        if _is_rails_app(root):
            return "rails test"
        return "rake test"
    if (root / "Rakefile").exists():
        return "rake test"

    # Go
    if (root / "go.mod").exists():
        return "go test ./..."

    # Rust
    if (root / "Cargo.toml").exists():
        return "cargo test"

    # Java
    if (root / "pom.xml").exists():
        return "mvn test"
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "./gradlew test"

    # Elixir
    if (root / "mix.exs").exists():
        return "mix test"

    return None


def get_build_command(project_root: str | Path) -> str | None:
    """プロジェクトに適したビルドコマンドを取得する。

    Args:
        project_root: project_root の値

    Returns:
        str | None: str を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    root = Path(project_root)

    # package.json の scripts を確認
    package_json = root / "package.json"
    if package_json.exists():
        data = _read_json_file(package_json)
        scripts = data.get("scripts", {})
        if "build" in scripts:
            return "npm run build"

    # Go
    if (root / "go.mod").exists():
        return "go build ./..."

    # Rust
    if (root / "Cargo.toml").exists():
        return "cargo build"

    # Java
    if (root / "pom.xml").exists():
        return "mvn compile"
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "./gradlew build"

    # C/C++（CMake）
    if (root / "CMakeLists.txt").exists():
        return "cmake --build build"

    # C/C++（Make）
    if (root / "Makefile").exists():
        return "make"

    return None


__all__ = [
    "FRAMEWORK_RULES",
    "LANGUAGE_RULES",
    "FrameworkRule",
    "LanguageRule",
    "ProjectInfo",
    "detect_frameworks",
    "detect_languages",
    "detect_project",
    "get_build_command",
    "get_test_command",
]
