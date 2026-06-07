"""言語・フレームワーク検出ルールの定義テーブル。"""

from __future__ import annotations

from bluecore.lib.project_detect.models import FrameworkRule, LanguageRule

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
