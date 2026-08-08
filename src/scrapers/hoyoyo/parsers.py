"""Hoyoyo 聚合搜索结果解析器。

产出的字典键名与闲鱼/Mercari 保持一致(中文 key),下游 result_storage /
notification / AI 层零改动。聚合结果混杂三种来源(Yahoo拍卖 / Mercari /
雅虎购物),仅 Yahoo拍卖来源的卡片带竞拍数+发布时间信息,其余来源缺失字段
统一用现有占位符风格(`未知卖家`/`未知时间`),不新增字段语义。
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.scrapers.hoyoyo.constants import (
    BASE_URL,
    IMG_SELECTOR,
    ITEM_BLOCK_SELECTOR,
    MERCARI_IMG_DOMAIN,
    MERCARI_LINK_MARKER,
    OPTION_SELECTOR,
    PRICE_SELECTOR,
    SOURCE_MERCARI,
    SOURCE_YAHOO_AUCTION,
    SOURCE_YAHOO_SHOPPING,
    TITLE_SELECTOR,
    YAHOO_SHOPPING_IMG_DOMAIN,
)

_ITEM_ID_PATTERNS = (
    re.compile(r"itemId~([^~]+?)\.html"),
    re.compile(r"~id~([^~]+?)\.html"),
)


def parse_search_response_json(json_data: dict) -> tuple[list[dict], dict]:
    """解析搜索接口顶层响应,拆出商品列表和分页元信息。"""
    content_html = json_data.get("content") or ""
    items = parse_item_blocks_html(content_html)
    page_info = {
        "max_p": _safe_int(json_data.get("max_p"), default=1),
        "p": _safe_int(json_data.get("p"), default=1),
        "has_next": str(json_data.get("nextPage") or "").strip().upper() == "Y",
    }
    return items, page_info


def parse_item_blocks_html(content_html: str) -> list[dict]:
    """解析商品卡片 HTML 片段,返回统一中文字段字典列表。"""
    if not content_html:
        return []
    soup = BeautifulSoup(content_html, "html.parser")
    items: list[dict] = []
    for block in soup.select(ITEM_BLOCK_SELECTOR):
        parsed = _parse_one_block(block)
        if parsed is not None:
            items.append(parsed)
    return items


def _parse_one_block(block: Any) -> Optional[dict]:
    link_el = block.select_one("a[href]")
    href = link_el.get("href") if link_el else None
    if not href:
        return None
    link = urljoin(BASE_URL, href)

    title_el = block.select_one(TITLE_SELECTOR)
    title = title_el.get_text(strip=True) if title_el else "未知标题"

    img_el = block.select_one(IMG_SELECTOR)
    image_url = (img_el.get("src") or "").strip() if img_el else ""

    source_type = _detect_source_type(link, image_url)

    price_el = block.select_one(PRICE_SELECTOR)
    price_text = price_el.get_text(" ", strip=True) if price_el else ""
    current_price = _format_price(price_text)

    option_el = block.select_one(OPTION_SELECTOR)
    publish_time = "未知时间"
    if source_type == SOURCE_YAHOO_AUCTION and option_el is not None:
        publish_time = _extract_publish_time(option_el.get_text(" ", strip=True))

    item_id = _extract_item_id(href, source_type)

    return {
        "商品标题": title,
        "当前售价": current_price,
        "商品原价": "暂无",
        "商品标签": [source_type],
        "发货地区": "地区未知",
        "卖家昵称": "未知卖家",
        "商品链接": link,
        "发布时间": publish_time,
        "商品ID": item_id,
        "商品图片列表": [image_url] if image_url else [],
        "商品主图链接": image_url or None,
    }


def _detect_source_type(link: str, image_url: str) -> str:
    if MERCARI_LINK_MARKER in link or MERCARI_IMG_DOMAIN in image_url:
        return SOURCE_MERCARI
    if YAHOO_SHOPPING_IMG_DOMAIN in image_url:
        return SOURCE_YAHOO_SHOPPING
    return SOURCE_YAHOO_AUCTION


def _extract_item_id(href: str, source_type: str) -> str:
    for pattern in _ITEM_ID_PATTERNS:
        match = pattern.search(href)
        if match:
            return f"hoyoyo:{source_type}:{match.group(1)}"
    return f"hoyoyo:{source_type}:{href}"


def _format_price(price_text: str) -> str:
    if not price_text:
        return "价格异常"
    match = re.search(r"([\d,]+)\s*日元", price_text)
    if not match:
        return "价格异常"
    try:
        amount = int(match.group(1).replace(",", ""))
    except ValueError:
        return "价格异常"
    return f"¥{amount:,}"


def _extract_publish_time(option_text: str) -> str:
    if not option_text:
        return "未知时间"
    # 例: "34 1日" / "12 3時間" —— 竞拍数 + 距发布/结束的相对时间。
    match = re.search(r"(\d+\s*(?:日|時間|分|秒|d|h|m|s))", option_text)
    return match.group(1).replace(" ", "") if match else "未知时间"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
