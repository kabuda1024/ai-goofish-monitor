"""平台专属代理派发测试。

验证:
- 闲鱼 (requires_proxy=False) 硬关代理,忽略全局 PROXY_ROTATION_ENABLED
- Mercari (requires_proxy=True) 读取 MERCARI_PROXY_URL / _POOL / _ENABLED
- 任务级 proxy_rotation 覆盖平台默认
"""
from __future__ import annotations

import pytest

from src.scrapers.base import get_rotation_settings
from src.scrapers.mercari.scraper import MercariScraper
from src.scrapers.xianyu.scraper import XianyuScraper


TASK = {"task_name": "t", "keyword": "k", "max_pages": 1}


class TestXianyuProxyForbidden:
    """闲鱼硬关代理:即使全局设了 PROXY_URL/PROXY_POOL 也不生效。"""

    def test_xianyu_ignores_global_proxy_env(self, monkeypatch):
        monkeypatch.setenv("PROXY_ROTATION_ENABLED", "true")
        monkeypatch.setenv("PROXY_POOL", "http://global:8080")

        scraper = XianyuScraper(TASK)
        settings = get_rotation_settings(
            TASK, platform_proxy_defaults=scraper._resolve_platform_proxy_defaults()
        )
        assert settings["proxy_enabled"] is False
        assert settings["proxy_pool"] == ""

    def test_xianyu_task_level_config_still_wins(self, monkeypatch):
        """即使平台禁用代理,任务级显式配了代理也应生效(高级用户覆盖)。"""
        monkeypatch.setenv("PROXY_ROTATION_ENABLED", "false")

        task = {
            **TASK,
            "proxy_rotation": {
                "enabled": True,
                "proxy_pool": "http://cn-line-1:8080,http://cn-line-2:8080",
            },
        }
        scraper = XianyuScraper(task)
        settings = get_rotation_settings(
            task, platform_proxy_defaults=scraper._resolve_platform_proxy_defaults()
        )
        assert settings["proxy_enabled"] is True
        assert "cn-line-1" in settings["proxy_pool"]


class TestMercariProxyRequired:
    """Mercari 声明 requires_proxy=True,读取 MERCARI_PROXY_* 变量。"""

    def test_mercari_reads_platform_env_pool(self, monkeypatch):
        monkeypatch.delenv("PROXY_ROTATION_ENABLED", raising=False)
        monkeypatch.delenv("PROXY_POOL", raising=False)
        monkeypatch.setenv("MERCARI_PROXY_POOL", "http://jp-1:8080,http://jp-2:8080")

        scraper = MercariScraper(TASK)
        settings = get_rotation_settings(
            TASK, platform_proxy_defaults=scraper._resolve_platform_proxy_defaults()
        )
        assert settings["proxy_enabled"] is True
        assert "jp-1" in settings["proxy_pool"]

    def test_mercari_reads_single_url(self, monkeypatch):
        monkeypatch.delenv("MERCARI_PROXY_POOL", raising=False)
        monkeypatch.setenv("MERCARI_PROXY_URL", "http://home-clash:7890")

        scraper = MercariScraper(TASK)
        settings = get_rotation_settings(
            TASK, platform_proxy_defaults=scraper._resolve_platform_proxy_defaults()
        )
        assert settings["proxy_enabled"] is True
        assert settings["proxy_pool"] == "http://home-clash:7890"

    def test_mercari_can_be_explicitly_disabled(self, monkeypatch):
        monkeypatch.setenv("MERCARI_PROXY_URL", "http://x:1")
        monkeypatch.setenv("MERCARI_PROXY_ENABLED", "false")

        scraper = MercariScraper(TASK)
        settings = get_rotation_settings(
            TASK, platform_proxy_defaults=scraper._resolve_platform_proxy_defaults()
        )
        assert settings["proxy_enabled"] is False

    def test_mercari_task_level_overrides_platform_env(self, monkeypatch):
        monkeypatch.setenv("MERCARI_PROXY_URL", "http://platform-default:7890")

        task = {
            **TASK,
            "proxy_rotation": {
                "enabled": True,
                "proxy_pool": "http://task-specific:9999",
            },
        }
        scraper = MercariScraper(task)
        settings = get_rotation_settings(
            task, platform_proxy_defaults=scraper._resolve_platform_proxy_defaults()
        )
        assert settings["proxy_enabled"] is True
        assert settings["proxy_pool"] == "http://task-specific:9999"

    def test_mercari_without_env_returns_disabled(self, monkeypatch):
        monkeypatch.delenv("MERCARI_PROXY_URL", raising=False)
        monkeypatch.delenv("MERCARI_PROXY_POOL", raising=False)
        monkeypatch.delenv("MERCARI_PROXY_ENABLED", raising=False)
        monkeypatch.delenv("PROXY_ROTATION_ENABLED", raising=False)

        scraper = MercariScraper(TASK)
        settings = get_rotation_settings(
            TASK, platform_proxy_defaults=scraper._resolve_platform_proxy_defaults()
        )
        # 没配任何代理时,即使 requires_proxy=True 也不会强制开启
        assert settings["proxy_enabled"] is False
        assert settings["proxy_pool"] == ""


class TestPlatformDefaultsResolution:
    """_resolve_platform_proxy_defaults 的行为。"""

    def test_xianyu_forbids_proxy(self):
        scraper = XianyuScraper(TASK)
        defaults = scraper._resolve_platform_proxy_defaults()
        assert defaults["forbids_proxy"] is True

    def test_mercari_returns_env_derived_defaults(self, monkeypatch):
        monkeypatch.setenv("MERCARI_PROXY_URL", "http://x:1")
        monkeypatch.delenv("MERCARI_PROXY_ENABLED", raising=False)

        scraper = MercariScraper(TASK)
        defaults = scraper._resolve_platform_proxy_defaults()
        assert defaults["forbids_proxy"] is False
        assert defaults["enabled"] is True
        assert defaults["proxy_pool"] == "http://x:1"

    def test_mercari_prefers_pool_over_single_url(self, monkeypatch):
        monkeypatch.setenv("MERCARI_PROXY_URL", "http://single:1")
        monkeypatch.setenv("MERCARI_PROXY_POOL", "http://pool-1:1,http://pool-2:2")

        scraper = MercariScraper(TASK)
        defaults = scraper._resolve_platform_proxy_defaults()
        assert "pool-1" in defaults["proxy_pool"]
        assert "pool-2" in defaults["proxy_pool"]
