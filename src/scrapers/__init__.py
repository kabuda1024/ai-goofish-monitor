"""
多站点爬虫注册表。

从 platform 字符串派发到对应的爬虫类。
"""
from __future__ import annotations

from src.scrapers.base import (
    BasePlaywrightScraper,
    LoginRequiredError,
    RiskControlError,
)


def get_scraper_class(platform: str) -> type:
    """根据 platform 字符串返回对应的爬虫类,未知平台回退到闲鱼。

    返回类型放宽为 `type`(而非 `Type[BasePlaywrightScraper]`):hoyoyo 是无
    登录态的轻量 httpx 爬虫,不继承 `BasePlaywrightScraper`,如实反映架构差异。
    """
    normalized = (platform or "").strip().lower() or "xianyu"
    # 延迟 import,避免循环依赖 + 更快启动
    if normalized == "mercari":
        from src.scrapers.mercari.scraper import MercariScraper
        return MercariScraper
    if normalized == "hoyoyo":
        from src.scrapers.hoyoyo.scraper import HoyoyoScraper
        return HoyoyoScraper
    from src.scrapers.xianyu.scraper import XianyuScraper
    return XianyuScraper


__all__ = [
    "BasePlaywrightScraper",
    "LoginRequiredError",
    "RiskControlError",
    "get_scraper_class",
]
