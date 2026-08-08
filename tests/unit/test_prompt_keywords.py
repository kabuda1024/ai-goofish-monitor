"""AI 关键词生成解析测试。"""
from __future__ import annotations

from src.prompt_utils import (
    TaskMetadata,
    _parse_keywords_response,
)


class TestParseKeywordsResponse:
    def test_parses_clean_json(self):
        raw = '{"primary": "iMac M1", "alternatives": ["iMac M2", "iMac M3"]}'
        result = _parse_keywords_response(raw)
        assert result["primary"] == "iMac M1"
        assert result["alternatives"] == ["iMac M2", "iMac M3"]

    def test_strips_markdown_code_fence(self):
        raw = '```json\n{"primary": "iMac", "alternatives": ["Mac mini"]}\n```'
        result = _parse_keywords_response(raw)
        assert result["primary"] == "iMac"
        assert result["alternatives"] == ["Mac mini"]

    def test_strips_generic_code_fence(self):
        raw = '```\n{"primary": "α7 IV", "alternatives": []}\n```'
        result = _parse_keywords_response(raw)
        assert result["primary"] == "α7 IV"
        assert result["alternatives"] == []

    def test_returns_empty_on_invalid_json(self):
        result = _parse_keywords_response("not a json")
        assert result == {"primary": "", "alternatives": []}

    def test_returns_empty_on_empty_string(self):
        assert _parse_keywords_response("") == {"primary": "", "alternatives": []}

    def test_ignores_non_dict_top_level(self):
        # AI 可能返回一个数组而不是对象
        assert _parse_keywords_response("[]") == {
            "primary": "",
            "alternatives": [],
        }

    def test_filters_empty_alternatives(self):
        raw = '{"primary": "iPhone", "alternatives": ["  ", "iPhone 15", ""]}'
        result = _parse_keywords_response(raw)
        assert result["alternatives"] == ["iPhone 15"]

    def test_handles_missing_alternatives_key(self):
        raw = '{"primary": "iPhone 15"}'
        result = _parse_keywords_response(raw)
        assert result["primary"] == "iPhone 15"
        assert result["alternatives"] == []

    def test_alternatives_wrong_type_falls_back_to_empty(self):
        raw = '{"primary": "iPhone", "alternatives": "not a list"}'
        result = _parse_keywords_response(raw)
        assert result["alternatives"] == []


class TestTaskMetadata:
    def test_all_keywords_dedupes_and_orders(self):
        meta = TaskMetadata(
            criteria="...",
            primary_keyword="iMac M1",
            alternative_keywords=["iMac M2", "iMac M1", "iMac M3"],  # M1 重复
        )
        # 主关键词在最前,后续重复的被去掉
        assert meta.all_keywords == ["iMac M1", "iMac M2", "iMac M3"]

    def test_all_keywords_ignores_case_dedup(self):
        meta = TaskMetadata(
            criteria="...",
            primary_keyword="iMac M1",
            alternative_keywords=["IMAC M1", "iMac M2"],
        )
        assert meta.all_keywords == ["iMac M1", "iMac M2"]

    def test_all_keywords_strips_whitespace(self):
        meta = TaskMetadata(
            criteria="...",
            primary_keyword="  iMac M1  ",
            alternative_keywords=["  iMac M2 "],
        )
        assert meta.all_keywords == ["iMac M1", "iMac M2"]

    def test_empty_primary_still_returns_alternatives(self):
        meta = TaskMetadata(
            criteria="...",
            primary_keyword="",
            alternative_keywords=["iMac M2", "iMac M3"],
        )
        assert meta.all_keywords == ["iMac M2", "iMac M3"]
