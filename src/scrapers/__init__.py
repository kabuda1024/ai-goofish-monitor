"""
多站点爬虫注册表。

从 platform 字符串派发到对应的爬虫类。
"""
from __future__ import annotations

from typing import Type

from src.scrapers.base import (
    BasePlaywrightScraper,
    LoginRequiredError,
    RiskControlError,
)


def get_scraper_class(platform: str) -> Type[BasePlaywrightScraper]:
    """根据 platform 字符串返回对应的爬虫类,未知平台回退到闲鱼。"""
    normalized = (platform or "").strip().lower() or "xianyu"
    # 延迟 import,避免循环依赖 + 更快启动
    if normalized == "mercari":
        from src.scrapers.mercari.scraper import MercariScraper
        return MercariScraper
    from src.scrapers.xianyu.scraper import XianyuScraper
    return XianyuScraper


__all__ = [
    "BasePlaywrightScraper",
    "LoginRequiredError",
    "RiskControlError",
    "get_scraper_class",
]
