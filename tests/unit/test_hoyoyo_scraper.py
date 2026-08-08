"""HoyoyoScraper 分页/去重/反爬检测测试。

用 httpx.MockTransport 模拟搜索接口响应,不依赖真实网络或 SQLite —— 所有
下游服务函数(去重加载、价格历史、保存、通知、AI分析)在 scraper 模块级别
直接打桩,只验证 HoyoyoScraper.run() 自身的控制流(分页停止条件、
debug_limit 截断、按链接去重、反爬拦截重试后放弃)。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import src.scrapers.hoyoyo.scraper as hoyoyo_scraper
from src.scrapers.hoyoyo.scraper import HoyoyoScraper
from src.utils import get_link_unique_key


def _block_html(item_id: str, *, title: str = "商品") -> str:
    return f"""
    <div class="item-search-item-info">
      <a href="/auction~detail~itemId~{item_id}.html">
        <img class="lazy" src="https://auctions.c.yimg.jp/img/{item_id}.jpg" />
        <p class="item-search-item-title">{title} {item_id}</p>
        <div class="content-price">10,000 日元 ( 480 RMB )</div>
        <div class="item-search-item-option">5 2日</div>
      </a>
    </div>
    """


def _page_payload(*, max_p: int, p: int, next_page: str, item_ids: list[str]) -> dict:
    return {
        "max_p": max_p,
        "p": str(p),
        "nextPage": next_page,
        "status": 1,
        "content": "".join(_block_html(item_id) for item_id in item_ids),
    }


def _json_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


@pytest.fixture(autouse=True)
def _stub_downstream_services(monkeypatch):
    """把 scraper 模块里引用的下游服务函数换成无 IO 的假实现。"""
    saved: list[dict] = []

    async def fake_load_price_snapshots_noop(*args, **kwargs):
        return []

    def fake_load_price_snapshots(keyword):
        return []

    def fake_record_market_snapshots(**kwargs):
        return []

    def fake_build_market_reference(**kwargs):
        return {}

    async def fake_download_all_images(product_id, image_urls, task_name):
        return []

    async def fake_get_ai_analysis(record, image_paths, prompt_text):
        return {
            "analysis_source": "ai",
            "is_recommended": False,
            "reason": "test",
            "keyword_hit_count": 0,
        }

    async def fake_send_ntfy_notification(item_data, reason):
        return None

    async def fake_save_to_jsonl(record, keyword):
        saved.append(record)
        return True

    def fake_cleanup_task_images(task_name):
        return None

    monkeypatch.setattr(hoyoyo_scraper, "load_price_snapshots", fake_load_price_snapshots)
    monkeypatch.setattr(hoyoyo_scraper, "record_market_snapshots", fake_record_market_snapshots)
    monkeypatch.setattr(hoyoyo_scraper, "build_market_reference", fake_build_market_reference)
    monkeypatch.setattr(hoyoyo_scraper, "download_all_images", fake_download_all_images)
    monkeypatch.setattr(hoyoyo_scraper, "get_ai_analysis", fake_get_ai_analysis)
    monkeypatch.setattr(hoyoyo_scraper, "send_ntfy_notification", fake_send_ntfy_notification)
    monkeypatch.setattr(hoyoyo_scraper, "save_to_jsonl", fake_save_to_jsonl)
    monkeypatch.setattr(hoyoyo_scraper, "cleanup_task_images", fake_cleanup_task_images)

    async def fast_random_sleep(*_args, **_kwargs):
        return None

    async def fast_asyncio_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hoyoyo_scraper, "random_sleep", fast_random_sleep)
    monkeypatch.setattr(hoyoyo_scraper.asyncio, "sleep", fast_asyncio_sleep)

    return saved


def _base_task_config(**overrides) -> dict:
    config = {
        "keyword": "MacBook",
        "task_name": "hoyoyo-test",
        "max_pages": 5,
        "analyze_images": False,
        "decision_mode": "keyword",
        "keyword_rules": [],
    }
    config.update(overrides)
    return config


def _install_transport(monkeypatch, handler) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped)

    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return requests


class TestPagination:
    def test_stops_when_next_page_is_no(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "1"))
            if page == 1:
                return _json_response(
                    _page_payload(max_p=2, p=1, next_page="Y", item_ids=["a1", "a2"])
                )
            return _json_response(
                _page_payload(max_p=2, p=2, next_page="N", item_ids=["a3"])
            )

        requests = _install_transport(monkeypatch, handler)
        scraper = HoyoyoScraper(_base_task_config())
        count = asyncio.run(scraper.run())

        assert count == 3
        assert len(requests) == 2

    def test_stops_when_max_pages_reached(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "1"))
            return _json_response(
                _page_payload(max_p=99, p=page, next_page="Y", item_ids=[f"p{page}"])
            )

        requests = _install_transport(monkeypatch, handler)
        scraper = HoyoyoScraper(_base_task_config(max_pages=3))
        count = asyncio.run(scraper.run())

        assert count == 3
        assert len(requests) == 3

    def test_stops_when_page_returns_no_items(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(_page_payload(max_p=5, p=1, next_page="Y", item_ids=[]))

        requests = _install_transport(monkeypatch, handler)
        scraper = HoyoyoScraper(_base_task_config())
        count = asyncio.run(scraper.run())

        assert count == 0
        assert len(requests) == 1


class TestDebugLimit:
    def test_debug_limit_truncates_processing_and_stops_before_next_page(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", "1"))
            return _json_response(
                _page_payload(max_p=5, p=page, next_page="Y", item_ids=[f"p{page}a", f"p{page}b"])
            )

        requests = _install_transport(monkeypatch, handler)
        scraper = HoyoyoScraper(_base_task_config(), debug_limit=1)
        count = asyncio.run(scraper.run())

        assert count == 1
        assert len(requests) == 1


class TestDedup:
    def test_skips_items_already_in_processed_links(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                _page_payload(max_p=1, p=1, next_page="N", item_ids=["dup1", "new1"])
            )

        _install_transport(monkeypatch, handler)

        dup_link = "https://cn.hoyoyo-cloud.com/auction~detail~itemId~dup1.html"
        monkeypatch.setattr(
            hoyoyo_scraper,
            "load_processed_link_keys",
            lambda keyword: {get_link_unique_key(dup_link)},
        )

        scraper = HoyoyoScraper(_base_task_config())
        count = asyncio.run(scraper.run())

        assert count == 1


class TestCloudflareBlock:
    def test_retries_then_aborts_without_raising(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="Attention Required! | Cloudflare",
            )

        requests = _install_transport(monkeypatch, handler)
        scraper = HoyoyoScraper(_base_task_config())
        count = asyncio.run(scraper.run())

        assert count == 0
        # 1 次首发 + len(BLOCK_RETRY_BACKOFF_SECONDS) 次重试
        assert len(requests) == 1 + len(hoyoyo_scraper.BLOCK_RETRY_BACKOFF_SECONDS)

    def test_recovers_if_a_retry_succeeds(self, monkeypatch):
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="Attention Required! | Cloudflare",
                )
            return _json_response(
                _page_payload(max_p=1, p=1, next_page="N", item_ids=["ok1"])
            )

        requests = _install_transport(monkeypatch, handler)
        scraper = HoyoyoScraper(_base_task_config())
        count = asyncio.run(scraper.run())

        assert count == 1
        assert len(requests) == 2


class TestSortResolution:
    def test_invalid_sort_falls_back_to_default(self):
        scraper = HoyoyoScraper(
            _base_task_config(platform_options={"sort": "not-a-real-sort"})
        )
        assert scraper._resolve_sort() == hoyoyo_scraper.SORT_DEFAULT

    def test_valid_sort_is_kept(self):
        scraper = HoyoyoScraper(_base_task_config(platform_options={"sort": "time"}))
        assert scraper._resolve_sort() == "time"
