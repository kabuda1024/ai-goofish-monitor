"""Mercari(日本站)相关常量。

Mercari 前端主要使用 REST + GraphQL 混合;搜索走 `api.mercari.jp/v2/entities:search`
(POST + DPoP header,但公开数据无需登录);详情走 `api.mercari.jp/items/{id}`。
本项目通过 Playwright 拦截浏览器发出的这些请求,规避 DPoP 签名。
"""
from __future__ import annotations

HOMEPAGE_URL = "https://jp.mercari.com/"
SEARCH_PAGE_URL = "https://jp.mercari.com/search"
ITEM_PAGE_URL_TEMPLATE = "https://jp.mercari.com/item/{item_id}"
USER_PROFILE_URL_TEMPLATE = "https://jp.mercari.com/user/profile/{user_id}"

# 拦截片段(用 in 判断)
SEARCH_API_URL_FRAGMENT = "api.mercari.jp/v2/entities:search"
DETAIL_API_URL_FRAGMENT = "api.mercari.jp/items/"
USER_PROFILE_API_URL_FRAGMENT = "api.mercari.jp/users/"

# Cloudflare / bot 检测标识
CLOUDFLARE_CHALLENGE_MARKERS = (
    "cf-challenge-running",
    "Attention Required! | Cloudflare",
    "Please verify you are a human",
    "Access denied",
)

# 商品状态 / 送料方标识
STATUS_ON_SALE = "on_sale"       # 販売中
STATUS_SOLD_OUT = "trading"      # 取引中/売り切れ
SHIPPING_PAYER_BUYER = 1         # 着払い(买家付)
SHIPPING_PAYER_SELLER = 2        # 送料込み(卖家包邮)

# 商品状态 code(item_condition_id) → 日文
ITEM_CONDITION_LABELS = {
    1: "新品、未使用",
    2: "未使用に近い",
    3: "目立った傷や汚れなし",
    4: "やや傷や汚れあり",
    5: "傷や汚れあり",
    6: "全体的に状態が悪い",
}
