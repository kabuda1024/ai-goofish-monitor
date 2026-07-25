"""Scrapers 注册表(get_scraper_class)行为测试。"""
from __future__ import annotations

import pytest

from src.scrapers import get_scraper_class
from src.scrapers.base import BasePlaywrightScraper
from src.scrapers.mercari.scraper import MercariScraper
from src.scrapers.xianyu.scraper import XianyuScraper


def test_get_scraper_class_returns_xianyu_by_default():
    assert get_scraper_class("") is XianyuScraper
    assert get_scraper_class(None) is XianyuScraper  # type: ignore[arg-type]


def test_get_scraper_class_dispatches_xianyu():
    cls = get_scraper_class("xianyu")
    assert cls is XianyuScraper
    assert issubclass(cls, BasePlaywrightScraper)
    assert cls.platform_name == "xianyu"


def test_get_scraper_class_dispatches_mercari():
    cls = get_scraper_class("mercari")
    assert cls is MercariScraper
    assert issubclass(cls, BasePlaywrightScraper)
    assert cls.platform_name == "mercari"


def test_get_scraper_class_falls_back_on_unknown_platform():
    cls = get_scraper_class("unknown_platform_xyz")
    assert cls is XianyuScraper


def test_platform_is_case_insensitive():
    assert get_scraper_class("MERCARI") is MercariScraper
    assert get_scraper_class("Xianyu") is XianyuScraper


def test_xianyu_scraper_config_defaults():
    """确保 XianyuScraper 保留原有的默认行为。"""
    assert XianyuScraper.requires_login_state is True
    assert XianyuScraper.default_state_filename == "xianyu_state.json"
    assert "goofish.com" in XianyuScraper.homepage_url


def test_mercari_scraper_config_defaults():
    """Mercari 不需要登录态,homepage 指向日区。"""
    assert MercariScraper.requires_login_state is False
    assert MercariScraper.default_state_filename is None
    assert "jp.mercari.com" in MercariScraper.homepage_url


def test_scrapers_can_be_instantiated():
    """两个子类应该都能被实例化(说明所有抽象方法都有实现)。"""
    task_config = {"task_name": "t", "keyword": "k", "max_pages": 1}
    xy = XianyuScraper(task_config)
    mc = MercariScraper(task_config)
    assert xy.platform_name == "xianyu"
    assert mc.platform_name == "mercari"


def test_base_class_is_abstract():
    """BasePlaywrightScraper 本身应该无法实例化(还有未实现的抽象方法)。"""
    with pytest.raises(TypeError):
        BasePlaywrightScraper({"task_name": "t", "keyword": "k", "max_pages": 1})  # type: ignore[abstract]
