"""hook_flags モジュールのユニットテスト。

デシジョンテーブル:
  normalize_id:
    - None → ""
    - 空文字 → ""
    - 大文字 → 小文字
    - 前後空白 → トリム

   get_hook_profile:
     - 常に "strict"

  parse_profiles:
    - None → fallback
    - 空リスト → fallback
    - 空文字列 → fallback
    - 有効プロファイルのみ含むリスト → フィルタ済みリスト
    - 無効プロファイルのみ含むリスト → fallback
    - CSV文字列 → パース済みリスト
    - 混在（有効＋無効） → 有効のみ

  is_hook_enabled:
    - 空hook_id → True (常に有効)
    - プロファイル一致 → True
    - プロファイル不一致 → False
"""

from __future__ import annotations

import pytest

from devgear.lib.hook_flags import (
    VALID_PROFILES,
    get_hook_profile,
    is_hook_enabled,
    normalize_id,
    parse_profiles,
)


class TestNormalizeId:
    """normalize_id の境界値テスト"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, ""),
            ("", ""),
            ("MyHook", "myhook"),
            ("  MY_HOOK  ", "my_hook"),
            ("hook-123", "hook-123"),
            ("HOOK", "hook"),
        ],
    )
    def test_normalize_id(self, value: str | None, expected: str) -> None:
        assert normalize_id(value) == expected


class TestGetHookProfile:
    """get_hook_profile の固定値テスト"""

    def test_always_returns_strict(self) -> None:
        assert get_hook_profile() == "strict"


class TestParseProfiles:
    """parse_profiles のデシジョンテーブルテスト"""

    def test_none_returns_default_fallback(self) -> None:
        assert parse_profiles(None) == ["standard", "strict"]

    def test_empty_list_returns_fallback(self) -> None:
        assert parse_profiles([]) == ["standard", "strict"]

    def test_empty_string_returns_fallback(self) -> None:
        assert parse_profiles("") == ["standard", "strict"]

    def test_custom_fallback_used(self) -> None:
        assert parse_profiles(None, fallback=["minimal"]) == ["minimal"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["minimal"], ["minimal"]),
            (["standard", "strict"], ["standard", "strict"]),
            (["strict", "minimal", "standard"], ["strict", "minimal", "standard"]),
        ],
    )
    def test_valid_profile_list_returned(self, raw: list[str], expected: list[str]) -> None:
        assert parse_profiles(raw) == expected

    def test_invalid_profiles_in_list_filtered_out(self) -> None:
        result = parse_profiles(["custom", "standard", "invalid"])
        assert result == ["standard"]

    def test_all_invalid_in_list_returns_fallback(self) -> None:
        assert parse_profiles(["custom", "debug"]) == ["standard", "strict"]

    def test_csv_string_parsed(self) -> None:
        assert parse_profiles("standard,strict") == ["standard", "strict"]

    def test_csv_with_whitespace(self) -> None:
        assert parse_profiles("  standard , strict  ") == ["standard", "strict"]

    def test_csv_with_invalid_returns_valid_only(self) -> None:
        assert parse_profiles("minimal,invalid,strict") == ["minimal", "strict"]

    def test_csv_all_invalid_returns_fallback(self) -> None:
        assert parse_profiles("custom,debug") == ["standard", "strict"]

    def test_valid_profiles_constant_contains_three_items(self) -> None:
        assert VALID_PROFILES == frozenset(["minimal", "standard", "strict"])


class TestIsHookEnabled:
    """is_hook_enabled の統合テスト"""

    def test_empty_hook_id_always_enabled(self) -> None:
        assert is_hook_enabled("") is True

    def test_profile_match_returns_true(self) -> None:
        assert is_hook_enabled("my_hook", profiles=["strict"]) is True

    def test_profile_mismatch_returns_false(self) -> None:
        assert is_hook_enabled("my_hook", profiles=["standard"]) is False

    def test_no_profiles_arg_uses_default_fallback(self) -> None:
        # profiles=None → fallback で strict が含まれる
        assert is_hook_enabled("my_hook", profiles=None) is True
