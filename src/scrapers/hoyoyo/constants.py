"""Hoyoyo(日购聚合站)相关常量。

`https://cn.hoyoyo-cloud.com/goods~search.html` 聚合了 Yahoo拍卖 / Mercari /
雅虎购物等日本站点的搜索结果。该接口无需登录,但必须带 `X-Requested-With` 头
+ 浏览器 UA,否则会返回整页 HTML 而不是 AJAX JSON。
"""
from __future__ import annotations

BASE_URL = "https://cn.hoyoyo-cloud.com/goods~search.html"

DEFAULT_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": BASE_URL,
}

# curl 实测验证过的固定追踪值,与登录态无关。
DEFAULT_FYKEYID = "58798"

DEFAULT_PARAMS = {
    "lang": "translated",
    "sites_id": "0",
    "category_id": "",
    "fykeyid": DEFAULT_FYKEYID,
}

# platform_options.sort 允许的原始站点排序 token。
SORT_DEFAULT = "-score"
SORT_NEWEST = "time"
SORT_PRICE_ASC = "price"
SORT_PRICE_DESC = "-price"
ALLOWED_SORTS = {SORT_DEFAULT, SORT_NEWEST, SORT_PRICE_ASC, SORT_PRICE_DESC}

ITEM_BLOCK_SELECTOR = "div.item-search-item-info"
TITLE_SELECTOR = ".item-search-item-title"
PRICE_SELECTOR = ".content-price"
OPTION_SELECTOR = ".item-search-item-option"
IMG_SELECTOR = "img.lazy"
SOURCE_LOGO_SELECTOR = "img.show-logo"

# 来源类型判定标记。
SOURCE_YAHOO_AUCTION = "yahoo_auction"
SOURCE_MERCARI = "mercari"
SOURCE_YAHOO_SHOPPING = "yahoo_shopping"

MERCARI_LINK_MARKER = "/mercari~detail~id~"
MERCARI_IMG_DOMAIN = "image.hoyoyo-cache.com/mercari/"
YAHOO_SHOPPING_IMG_DOMAIN = "item-shopping.c.yimg.jp"

CLOUDFLARE_BLOCK_MARKERS = (
    "Attention Required! | Cloudflare",
    "Access denied",
    "cf-challenge-running",
    "Sorry, you have been blocked",
)

REQUEST_TIMEOUT_SECONDS = 20
PAGE_SLEEP_MIN_SECONDS = 2
PAGE_SLEEP_MAX_SECONDS = 5
BLOCK_RETRY_BACKOFF_SECONDS = (10, 20)
