"""コードマップ生成器モジュールのテスト。"""

import tempfile
from pathlib import Path

import devgear.codemaps.generate_codemaps as generate


def test_walk_dir():
    """ディレクトリ走査の動作を検証する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # テスト用の構成を作成する
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("console.log('app');")
        (tmp_path / "src" / "components").mkdir()
        (tmp_path / "src" / "components" / "Button.tsx").write_text("export const Button = () => {};")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg").mkdir()
        (tmp_path / "node_modules" / "pkg" / "index.js").write_text("module.exports = {};")

        files = generate.walk_dir(tmp_path)
        file_names = {f.name for f in files}

        # app.ts と Button.tsx は見つかり、node_modules 配下は除外される
        assert "app.ts" in file_names
        assert "Button.tsx" in file_names
        assert "index.js" not in file_names
        assert len(files) == 2


def test_classify_files(monkeypatch):
    """ファイル分類が領域ごとに行われることを確認する。"""
    files = [
        Path("src/components/Header.tsx"),
        Path("src/api/routes/users.ts"),
        Path("src/models/User.model.ts"),
        Path("src/integrations/stripe.ts"),
        Path("src/workers/email.worker.ts"),
    ]

    monkeypatch.setattr(generate, "ROOT", Path("."))
    monkeypatch.setattr(
        generate,
        "_AREA_PATTERNS",
        {
            "frontend": ("フロントエンド", ["components", "pages", "views", "client"]),
            "backend": ("バックエンド/API", ["api", "routes", "controllers", "server", "handlers"]),
            "database": ("データベース", ["models", "migrations", "schemas", "db", "database"]),
            "integrations": ("インテグレーション", ["integrations", "adapters", "external", "third-party"]),
            "workers": ("ワーカー", ["workers", "jobs", "tasks", "queues", "background"]),
        },
    )

    areas = generate.classify_files(files)

    assert len(areas["frontend"].files) == 1
    assert "Header.tsx" in areas["frontend"].files[0]

    assert len(areas["backend"].files) == 1
    assert "users.ts" in areas["backend"].files[0]

    assert len(areas["database"].files) == 1
    assert "User.model.ts" in areas["database"].files[0]

    assert len(areas["integrations"].files) == 1
    assert "stripe.ts" in areas["integrations"].files[0]

    assert len(areas["workers"].files) == 1
    assert "email.worker.ts" in areas["workers"].files[0]


def test_line_count():
    """行数計測の動作を確認する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        test_file = tmp_path / "test.txt"

        test_file.write_text("line 1\nline 2\nline 3\n")
        assert generate.line_count(test_file) == 3

        test_file.write_text("")
        assert generate.line_count(test_file) == 0

        # 存在しないファイルは 0 を返す
        assert generate.line_count(tmp_path / "nonexistent.txt") == 0


def test_build_tree():
    """ディレクトリツリー生成の動作を確認する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # 単純な構成を作成する
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("test")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "app.test.ts").write_text("test")
        (tmp_path / "README.md").write_text("# README")

        tree = generate.build_tree(tmp_path)

        # 構成が含まれていることを確認する
        assert "src" in tree
        assert "tests" in tree
        assert "README.md" in tree
        assert "├── " in tree or "└── " in tree


def test_generate_area_doc():
    """領域ドキュメントの生成を確認する。"""
    area = generate.AreaInfo("フロントエンド")
    area.files = ["src/components/Button.tsx", "src/pages/index.tsx"]
    area.entry_points = ["src/pages/index.tsx"]
    area.directories = ["src/components", "src/pages"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # テストファイルを作成する
        for f in area.files:
            file_path = tmp_path / f
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("// test\n// line 2\n")

        # 元の ROOT を保存する
        original_root = generate.ROOT
        generate.ROOT = tmp_path

        try:
            doc = generate.generate_area_doc("frontend", area, [])

            # ドキュメントに期待するセクションが含まれていることを確認する
            assert "# フロントエンド コードマップ" in doc
            assert "## エントリポイント" in doc
            assert "## アーキテクチャ" in doc
            assert "## 主要モジュール" in doc
            assert "src/pages/index.tsx" in doc
            assert "src/components/Button.tsx" in doc
        finally:
            generate.ROOT = original_root


def test_generate_index():
    """インデックス生成を確認する。"""
    areas = {
        "frontend": generate.AreaInfo("フロントエンド"),
        "backend": generate.AreaInfo("バックエンド/API"),
    }
    areas["frontend"].files = ["src/app.tsx"]
    areas["frontend"].directories = ["src"]
    areas["backend"].files = ["api/routes.ts"]
    areas["backend"].directories = ["api"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # 元の値を保存する
        original_root = generate.ROOT
        original_src = generate.SRC_DIR
        generate.ROOT = tmp_path
        generate.SRC_DIR = tmp_path

        try:
            index = generate.generate_index(areas, [])

            # インデックスに期待するセクションが含まれていることを確認する
            assert "# コードベース概要 — codemaps インデックス" in index
            assert "## 領域" in index
            assert "## リポジトリ構成" in index
            assert "## 再生成方法" in index
            assert "フロントエンド" in index
            assert "バックエンド/API" in index
        finally:
            generate.ROOT = original_root
            generate.SRC_DIR = original_src


def test_full_generation():
    """コードマップ全体の生成を一通り確認する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # 実際に近いテスト構成を作成する
        (tmp_path / "src" / "components").mkdir(parents=True)
        (tmp_path / "src" / "components" / "Button.tsx").write_text("export const Button = () => {};")
        (tmp_path / "src" / "api" / "routes").mkdir(parents=True)
        (tmp_path / "src" / "api" / "routes" / "users.ts").write_text("export const getUsers = () => {};")
        (tmp_path / "docs" / "codemaps").mkdir(parents=True)

        # 元の値を保存する
        original_root = generate.ROOT
        original_src = generate.SRC_DIR
        original_output = generate.OUTPUT_DIR
        generate.ROOT = tmp_path
        generate.SRC_DIR = tmp_path / "src"
        generate.OUTPUT_DIR = tmp_path / "docs" / "codemaps"

        try:
            # main 関数を実行する
            generate.main()

            # 期待するファイルがすべて作成されたことを確認する
            assert (tmp_path / "docs" / "codemaps" / "index.md").exists()
            assert (tmp_path / "docs" / "codemaps" / "frontend.md").exists()
            assert (tmp_path / "docs" / "codemaps" / "backend.md").exists()
            assert (tmp_path / "docs" / "codemaps" / "database.md").exists()
            assert (tmp_path / "docs" / "codemaps" / "integrations.md").exists()
            assert (tmp_path / "docs" / "codemaps" / "workers.md").exists()

            # index.md の内容を確認する
            index_content = (tmp_path / "docs" / "codemaps" / "index.md").read_text()
            assert "フロントエンド" in index_content
            assert "バックエンド/API" in index_content

            # frontend.md に Button.tsx が含まれていることを確認する
            frontend_content = (tmp_path / "docs" / "codemaps" / "frontend.md").read_text()
            assert "Button.tsx" in frontend_content

        finally:
            generate.ROOT = original_root
            generate.SRC_DIR = original_src
            generate.OUTPUT_DIR = original_output


def test_walk_dir_handles_permission_error():
    """walk_dir は PermissionError を無視して処理を継続する。"""
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / "app.ts").write_text("app")

        # iterdir が PermissionError を送出するようにモック
        original_iterdir = Path.iterdir

        def mock_iterdir(self):
            if self == tmp_path:
                raise PermissionError("permission denied")
            return original_iterdir(self)

        with patch.object(Path, "iterdir", mock_iterdir):
            files = generate.walk_dir(tmp_path)
        # エラーが起きても空リストを返すこと
        assert files == []


def test_build_tree_handles_permission_error():
    """build_tree は PermissionError を無視してルート名のみ返す。"""
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / "file.ts").write_text("content")

        original_iterdir = Path.iterdir

        def mock_iterdir(self):
            if self == tmp_path:
                raise PermissionError("permission denied")
            return original_iterdir(self)

        with patch.object(Path, "iterdir", mock_iterdir):
            tree = generate.build_tree(tmp_path)
        # PermissionError でもルート名だけ返ること
        assert tmp_path.name in tree


if __name__ == "__main__":
    # テストを実行する
    test_walk_dir()
    print("✓ test_walk_dir が成功しました")

    test_classify_files()
    print("✓ test_classify_files が成功しました")

    test_line_count()
    print("✓ test_line_count が成功しました")

    test_build_tree()
    print("✓ test_build_tree が成功しました")

    test_generate_area_doc()
    print("✓ test_generate_area_doc が成功しました")

    test_generate_index()
    print("✓ test_generate_index が成功しました")

    test_full_generation()
    print("✓ test_full_generation が成功しました")

    print("\nすべてのテストが成功しました！")
