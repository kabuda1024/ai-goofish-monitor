"""向后兼容层。

原 scrape_xianyu / scrape_user_profile / RiskControlError / LoginRequiredError
均已迁移到 src.scrapers.xianyu。本模块保留同名 re-export 供旧调用点使用。
"""
from src.scrapers import base as _base
from src.scrapers.base import LoginRequiredError, RiskControlError
from src.scrapers.xianyu import XianyuScraper, scrape_user_profile


# 向后兼容 re-export:老代码/测试可能引用了这些名字
_resolve_browser_channel = _base.resolve_browser_channel


def __getattr__(name: str):
    """向后兼容:透传模块级状态给旧代码。"""
    if name == "EDGE_DOCKER_WARNING_PRINTED":
        return _base.EDGE_DOCKER_WARNING_PRINTED
    raise AttributeError(f"module 'src.scraper' has no attribute {name!r}")


async def scrape_xianyu(task_config: dict, debug_limit: int = 0) -> int:
    """向后兼容入口。新代码请使用 src.scrapers.get_scraper_class。"""
    return await XianyuScraper(task_config, debug_limit=debug_limit).run()


__all__ = [
    "LoginRequiredError",
    "RiskControlError",
    "XianyuScraper",
    "scrape_user_profile",
    "scrape_xianyu",
    "_resolve_browser_channel",
]
