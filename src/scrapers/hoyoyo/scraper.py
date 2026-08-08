"""Hoyoyo(日购聚合站)轻量 httpx 爬虫。

`https://cn.hoyoyo-cloud.com/goods~search.html` 是无需登录的聚合搜索接口,
不涉及浏览器自动化,因此不继承 `BasePlaywrightScraper`(那套是为账号/代理
轮换 + 浏览器风控设计的)。本类只需满足 `spider_v2.py` 的 duck-typing 契约:
`__init__(task_config, debug_limit)` + `async def run() -> int`,并复用现有
下游管道(`ItemAnalysisDispatcher`、价格历史、结果落盘、图片下载、AI 分析、通知)。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

import httpx

from src.ai_handler import (
    cleanup_task_images,
    download_all_images,
    get_ai_analysis,
    send_ntfy_notification,
)
from src.config import SKIP_AI_ANALYSIS
from src.scrapers.base import get_ai_analysis_concurrency, should_analyze_images
from src.scrapers.hoyoyo.constants import (
    ALLOWED_SORTS,
    BASE_URL,
    BLOCK_RETRY_BACKOFF_SECONDS,
    CLOUDFLARE_BLOCK_MARKERS,
    DEFAULT_HEADERS,
    DEFAULT_PARAMS,
    PAGE_SLEEP_MAX_SECONDS,
    PAGE_SLEEP_MIN_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SORT_DEFAULT,
)
from src.scrapers.hoyoyo.parsers import parse_search_response_json
from src.services.item_analysis_dispatcher import ItemAnalysisDispatcher, ItemAnalysisJob
from src.services.price_history_service import (
    build_market_reference,
    load_price_snapshots,
    record_market_snapshots,
)
from src.services.result_storage_service import load_processed_link_keys
from src.utils import get_link_unique_key, log_time, random_sleep, save_to_jsonl


class HoyoyoBlockedError(Exception):
    """连续多次判定被 Cloudflare/反爬拦截,放弃本次任务。"""


async def _empty_seller_loader(_user_id: str) -> dict:
    return {}


class HoyoyoScraper:
    """日购聚合站(Yahoo拍卖/Mercari/雅虎购物)搜索列表爬虫,无登录态。"""

    platform_name = "hoyoyo"

    def __init__(self, task_config: dict, debug_limit: int = 0):
        self.task_config = task_config
        self.debug_limit = debug_limit

    def _resolve_sort(self) -> str:
        options = self.task_config.get("platform_options") or {}
        sort = str(options.get("sort") or "").strip()
        return sort if sort in ALLOWED_SORTS else SORT_DEFAULT

    async def _fetch_page(
        self, client: httpx.AsyncClient, *, keyword: str, sort: str, page: int
    ) -> dict:
        params = {
            **DEFAULT_PARAMS,
            "keyword": keyword,
            "keys": keyword.upper(),
            "sort": sort,
            "page": page,
        }
        response = await client.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        body_text = response.text
        if "application/json" not in content_type or any(
            marker in body_text for marker in CLOUDFLARE_BLOCK_MARKERS
        ):
            raise HoyoyoBlockedError(f"疑似被拦截 (content-type={content_type})")

        return response.json()

    async def _fetch_page_with_retry(
        self, client: httpx.AsyncClient, *, keyword: str, sort: str, page: int
    ) -> dict:
        last_error: Optional[Exception] = None
        attempts = 1 + len(BLOCK_RETRY_BACKOFF_SECONDS)
        for attempt in range(attempts):
            try:
                return await self._fetch_page(client, keyword=keyword, sort=sort, page=page)
            except HoyoyoBlockedError as e:
                last_error = e
                if attempt >= len(BLOCK_RETRY_BACKOFF_SECONDS):
                    break
                backoff = BLOCK_RETRY_BACKOFF_SECONDS[attempt]
                log_time(f"第 {page} 页疑似被拦截,{backoff}s 后重试 (第 {attempt + 1} 次重试)...")
                await asyncio.sleep(backoff)
        raise HoyoyoBlockedError(f"第 {page} 页多次重试后仍被拦截: {last_error}")

    async def run(self) -> int:
        task_config = self.task_config
        keyword = task_config["keyword"]
        task_name = task_config.get("task_name", "Untitled Task")
        max_pages = task_config.get("max_pages", 1)
        analyze_images = should_analyze_images(task_config)
        decision_mode = str(task_config.get("decision_mode", "ai")).strip().lower()
        if decision_mode not in {"ai", "keyword"}:
            decision_mode = "ai"
        keyword_rules = task_config.get("keyword_rules") or []
        ai_prompt_text = task_config.get("ai_prompt_text", "")
        sort = self._resolve_sort()

        history_run_id = datetime.now().strftime("%Y%m%d%H%M%S")
        history_seen_item_ids: set[str] = set()
        historical_snapshots = load_price_snapshots(keyword)
        processed_links = load_processed_link_keys(keyword)
        log_time(
            f"任务 '{task_name}' (hoyoyo) 已加载 {len(processed_links)} 个历史商品用于去重。"
        )

        processed_item_count = 0
        analysis_dispatcher = ItemAnalysisDispatcher(
            concurrency=get_ai_analysis_concurrency(task_config),
            skip_ai_analysis=SKIP_AI_ANALYSIS,
            seller_loader=_empty_seller_loader,
            image_downloader=download_all_images,
            ai_analyzer=get_ai_analysis,
            notifier=send_ntfy_notification,
            saver=save_to_jsonl,
        )

        try:
            async with httpx.AsyncClient(
                headers=DEFAULT_HEADERS, follow_redirects=True
            ) as client:
                page = 1
                while page <= max_pages:
                    if self.debug_limit and processed_item_count >= self.debug_limit:
                        log_time(f"已达到调试上限 ({self.debug_limit})，停止获取新商品。")
                        break

                    log_time(f"开始处理关键词 '{keyword}' 第 {page}/{max_pages} 页 (sort={sort}) ...")
                    try:
                        json_data = await self._fetch_page_with_retry(
                            client, keyword=keyword, sort=sort, page=page
                        )
                    except HoyoyoBlockedError as e:
                        log_time(f"任务 '{task_name}' 放弃本次抓取: {e}")
                        break

                    basic_items, page_info = parse_search_response_json(json_data)
                    if not basic_items:
                        log_time(f"第 {page} 页未解析出商品，停止翻页。")
                        break

                    historical_snapshots.extend(
                        record_market_snapshots(
                            keyword=keyword,
                            task_name=task_name,
                            items=basic_items,
                            run_id=history_run_id,
                            snapshot_time=datetime.now().isoformat(),
                            seen_item_ids=history_seen_item_ids,
                        )
                    )

                    for item_data in basic_items:
                        if self.debug_limit and processed_item_count >= self.debug_limit:
                            break

                        unique_key = get_link_unique_key(item_data["商品链接"])
                        if unique_key in processed_links:
                            continue

                        item_data["_platform"] = self.platform_name
                        final_record = {
                            "爬取时间": datetime.now().isoformat(),
                            "搜索关键字": keyword,
                            "任务名称": task_name,
                            "商品信息": item_data,
                            "卖家信息": {},
                            "_platform": self.platform_name,
                        }
                        final_record["价格参考"] = build_market_reference(
                            keyword=keyword,
                            item=item_data,
                            current_market_items=basic_items,
                            historical_snapshots=historical_snapshots,
                        )

                        analysis_dispatcher.submit(
                            ItemAnalysisJob(
                                keyword=keyword,
                                task_name=task_name,
                                decision_mode=decision_mode,
                                analyze_images=analyze_images,
                                prompt_text=ai_prompt_text,
                                keyword_rules=tuple(keyword_rules or []),
                                final_record=final_record,
                                seller_id=None,
                                zhima_credit_text=None,
                                registration_duration_text="未知",
                            )
                        )
                        processed_links.add(unique_key)
                        processed_item_count += 1

                    if not page_info.get("has_next") or page >= page_info.get("max_p", page):
                        break
                    page += 1
                    await random_sleep(PAGE_SLEEP_MIN_SECONDS, PAGE_SLEEP_MAX_SECONDS)

            log_time("等待后台分析任务完成...")
            await analysis_dispatcher.join()
        finally:
            cleanup_task_images(task_name)

        log_time(f"任务 '{task_name}' (hoyoyo) 执行完毕，本次共处理 {processed_item_count} 个新商品。")
        return processed_item_count
