"""爬虫多关键词循环相关测试。"""
from __future__ import annotations

from src.scrapers.mercari.scraper import MercariScraper
from src.scrapers.xianyu.scraper import XianyuScraper


class TestExtractSearchKeywords:
    def test_returns_only_primary_when_no_platform_options(self):
        task = {"task_name": "t", "keyword": "iMac M1", "max_pages": 1}
        scraper = MercariScraper(task)
        assert scraper.extract_search_keywords() == ["iMac M1"]

    def test_returns_only_primary_when_search_keywords_empty(self):
        task = {
            "task_name": "t",
            "keyword": "iMac M1",
            "max_pages": 1,
            "platform_options": {"search_keywords": []},
        }
        assert MercariScraper(task).extract_search_keywords() == ["iMac M1"]

    def test_primary_first_then_alternatives(self):
        task = {
            "task_name": "t",
            "keyword": "iMac M1",
            "max_pages": 1,
            "platform_options": {
                "search_keywords": ["iMac M2", "iMac M3", "iMac 24インチ"]
            },
        }
        assert MercariScraper(task).extract_search_keywords() == [
            "iMac M1",
            "iMac M2",
            "iMac M3",
            "iMac 24インチ",
        ]

    def test_dedupes_across_primary_and_alternatives(self):
        # AI 可能把 primary 也放进 alternatives
        task = {
            "task_name": "t",
            "keyword": "iMac M1",
            "max_pages": 1,
            "platform_options": {
                "search_keywords": ["iMac M1", "iMac M1", "iMac M2"]
            },
        }
        assert MercariScraper(task).extract_search_keywords() == [
            "iMac M1",
            "iMac M2",
        ]

    def test_case_insensitive_dedup(self):
        task = {
            "task_name": "t",
            "keyword": "iMac M1",
            "max_pages": 1,
            "platform_options": {"search_keywords": ["IMAC M1", "iMac M2"]},
        }
        assert MercariScraper(task).extract_search_keywords() == [
            "iMac M1",
            "iMac M2",
        ]

    def test_strips_whitespace_and_ignores_empty(self):
        task = {
            "task_name": "t",
            "keyword": "iMac M1",
            "max_pages": 1,
            "platform_options": {
                "search_keywords": ["  iMac M2  ", "", "   ", "iMac M3"]
            },
        }
        assert MercariScraper(task).extract_search_keywords() == [
            "iMac M1",
            "iMac M2",
            "iMac M3",
        ]

    def test_ignores_non_list_search_keywords(self):
        task = {
            "task_name": "t",
            "keyword": "iMac M1",
            "max_pages": 1,
            "platform_options": {"search_keywords": "iMac M2"},  # 字符串,不是列表
        }
        # 只取 primary,平台侧非法格式忽略
        assert MercariScraper(task).extract_search_keywords() == ["iMac M1"]

    def test_works_for_xianyu_too(self):
        task = {
            "task_name": "t",
            "keyword": "MacBook Air M1",
            "max_pages": 1,
            "platform_options": {"search_keywords": ["MacBook Air M2"]},
        }
        assert XianyuScraper(task).extract_search_keywords() == [
            "MacBook Air M1",
            "MacBook Air M2",
        ]

    def test_missing_keyword_still_returns_something(self):
        task = {
            "task_name": "t",
            "keyword": "",
            "max_pages": 1,
            "platform_options": {"search_keywords": ["iMac M1"]},
        }
        # AI 关键词起兜底作用
        assert MercariScraper(task).extract_search_keywords() == ["iMac M1"]
