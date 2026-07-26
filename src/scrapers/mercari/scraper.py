"""Mercari(日本站)爬虫。

Mercari 前端在页面加载时会向 `api.mercari.jp/v2/entities:search` 发起 POST 请求
(带 DPoP header),我们通过 Playwright response 拦截直接拿到 JSON,规避签名逻辑。

设计要点:
- 无需登录态(公开搜索和商品详情不要求登录)
- Cloudflare 检测:落地后检查关键文案
- 过滤器优先通过 URL 查询参数注入(price_min/price_max/status),UI 交互仅在必要时使用
- 分页通过 URL `page_token` 或页数参数完成
"""
from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
)

from src.scrapers.base import (
    BasePlaywrightScraper,
    LoginRequiredError,
    RiskControlError,
)
from src.scrapers.mercari.constants import (
    CLOUDFLARE_CHALLENGE_MARKERS,
    DETAIL_API_URL_FRAGMENT,
    HOMEPAGE_URL,
    SEARCH_API_URL_FRAGMENT,
    SEARCH_PAGE_URL,
    STATUS_ON_SALE,
    USER_PROFILE_API_URL_FRAGMENT,
    USER_PROFILE_URL_TEMPLATE,
)
from src.scrapers.mercari.parsers import (
    format_registration_days,
    parse_detail_json as parse_mercari_detail_json,
    parse_search_results_json as parse_mercari_search_json,
    parse_seller_profile_json,
)
from src.services.search_pagination import PageAdvanceResult
from src.utils import random_sleep


class MercariScraper(BasePlaywrightScraper):
    """日本 Mercari 站点的 Playwright 爬虫。"""

    platform_name = "mercari"
    homepage_url = HOMEPAGE_URL
    default_state_filename = None
    requires_login_state = False

    # 日本站点,国内访问需走代理。
    # 读取 MERCARI_PROXY_URL / MERCARI_PROXY_POOL / MERCARI_PROXY_ENABLED 环境变量。
    requires_proxy = True
    proxy_env_prefix = "MERCARI"

    # 保存当前页所属 URL,翻页时基于它拼 page_token 参数
    _current_search_url: Optional[str] = None

    # ---------------- context / init script ----------------

    def default_context_options(self) -> dict:
        """使用日本本地化的桌面浏览器上下文。"""
        return {
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1280, "height": 900},
            "device_scale_factor": 2.0,
            "is_mobile": False,
            "has_touch": False,
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
            "permissions": [],
            "color_scheme": "light",
        }

    def init_page_script(self) -> str:
        return """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja', 'ja-JP', 'en-US', 'en']});
            window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}};
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({state: Notification.permission}) :
                    originalQuery(parameters)
            );
        """

    # ---------------- 过滤器/URL ----------------

    def extract_filters_from_task(self) -> dict:
        base = super().extract_filters_from_task()
        options = base.get("platform_options") or {}
        # Mercari 站点特有:成色范围、都道府县
        item_condition_ids = options.get("item_condition_ids") or []
        prefecture_id = options.get("prefecture_id")  # 例: "13" for 東京都
        # Mercari 用 `status` 过滤販売中/売り切れ
        status_list = options.get("status") or [STATUS_ON_SALE]
        # 排序:新着順=created_time,最安=price,标准=score
        sort = options.get("sort", "created_time")
        order = options.get("order", "desc")
        base.update({
            "item_condition_ids": item_condition_ids,
            "prefecture_id": prefecture_id,
            "status_list": status_list,
            "sort": sort,
            "order": order,
        })
        return base

    def build_search_url(self, keyword: str, filters: dict) -> str:
        params: list[tuple[str, str]] = [("keyword", keyword)]
        params.append(("sort", filters.get("sort") or "created_time"))
        params.append(("order", filters.get("order") or "desc"))
        for status in filters.get("status_list") or [STATUS_ON_SALE]:
            params.append(("status", status))
        min_price = filters.get("min_price")
        max_price = filters.get("max_price")
        if min_price:
            params.append(("price_min", str(min_price)))
        if max_price:
            params.append(("price_max", str(max_price)))
        for cid in filters.get("item_condition_ids") or []:
            params.append(("item_condition_id", str(cid)))
        prefecture_id = filters.get("prefecture_id")
        if prefecture_id:
            params.append(("shipping_from_area", str(prefecture_id)))
        if filters.get("free_shipping"):
            # shipping_payer=2 → 送料込み(卖家包邮)
            params.append(("shipping_payer_id", "2"))
        return f"{SEARCH_PAGE_URL}?{urlencode(params)}"

    # ---------------- 拦截判断 ----------------

    def is_search_api_response(self, response: Response) -> bool:
        url = getattr(response, "url", "") or ""
        request = getattr(response, "request", None)
        method = getattr(request, "method", None)
        # Mercari search 是 POST,但 GraphQL 版本也可能不同,放宽方法判断
        return SEARCH_API_URL_FRAGMENT in url and method in ("POST", "GET")

    def is_detail_api_response(self, url: str) -> bool:
        return DETAIL_API_URL_FRAGMENT in (url or "")

    # ---------------- 解析 ----------------

    async def parse_search_json(
        self, json_data: dict, source_label: str
    ) -> list[dict]:
        # parsers 是同步函数,这里加个 await 语义包装以匹配 base 的签名
        return parse_mercari_search_json(json_data, source_label)

    async def parse_detail_json(
        self, detail_json: dict, item_data: dict
    ) -> tuple[dict, Optional[str], dict]:
        return parse_mercari_detail_json(detail_json, item_data)

    # ---------------- 页面交互 ----------------

    async def apply_filters(
        self, page: Page, filters: dict
    ) -> Optional[Response]:
        """Mercari 过滤器优先通过 URL 参数完成,此处只等页面稳定后返回 None。

        UI 交互可作为增强路径,但当前 MVP 依赖 URL 参数已足够。
        """
        self._current_search_url = page.url
        return None

    async def advance_to_next_page(
        self, page: Page, page_num: int
    ) -> PageAdvanceResult:
        """Mercari 翻页:寻找"次へ / Next"链接并点击,同时拦截搜索 API 响应。

        若找不到下一页按钮或超时,返回 advanced=False,由骨架层停止翻页。
        """
        # 常见的下一页按钮:mercari-web-frontend 用 `nav[aria-label="pagination"]`
        # 或 data-testid="pagination-next";我们做多重回退。
        next_selectors = [
            "a[data-testid='pagination-next']",
            "button[data-testid='pagination-next']:not([disabled])",
            "a[aria-label='Next']",
            "a[aria-label='次のページ']",
        ]
        next_button = None
        for selector in next_selectors:
            candidate = page.locator(selector).first
            if await candidate.count():
                next_button = candidate
                break

        if next_button is None:
            print("LOG: 未找到 Mercari 下一页按钮,停止翻页。")
            return PageAdvanceResult(
                advanced=False, stop_reason="no_next_button"
            )

        try:
            async with page.expect_response(
                self.is_search_api_response, timeout=20000
            ) as response_info:
                await next_button.click(timeout=10000)
                await random_sleep(2, 4)
            response = await response_info.value
            return PageAdvanceResult(advanced=True, response=response)
        except PlaywrightTimeoutError:
            print(f"等待第 {page_num} 页 Mercari 搜索响应超时,停止翻页。")
            return PageAdvanceResult(
                advanced=False, stop_reason="response_timeout"
            )

    # ---------------- 反爬 / 登录 ----------------

    async def detect_search_risk_control(self, page: Page) -> None:
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            html_snippet = await page.content()
        except Exception:
            html_snippet = ""
        blob = f"{title}\n{html_snippet[:4000]}"
        for marker in CLOUDFLARE_CHALLENGE_MARKERS:
            if marker.lower() in blob.lower():
                print(
                    "\n==================== CRITICAL BLOCK DETECTED ===================="
                )
                print(f"检测到 Cloudflare / 反爬拦截 (marker: {marker})。")
                print("建议: 切换代理、降低频率,或临时用 RUN_HEADLESS=false 手动过检。")
                print(
                    "==================================================================="
                )
                raise RiskControlError(f"cloudflare:{marker}")

    def detect_detail_risk_control(self, detail_json: dict) -> None:
        # Mercari 详情失败通常返回 4xx,base 层已按 detail_response.ok 处理;
        # 这里再检查 JSON 里的 error code,若发现 "captcha_required" 则抛。
        error = detail_json.get("error") if isinstance(detail_json, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "").lower()
            if "captcha" in code or "forbidden" in code or "denied" in code:
                raise RiskControlError(f"mercari_detail:{code}")

    def is_login_redirect(self, url: str) -> bool:
        # Mercari 需要登录时会跳到 /login,但公开搜索/详情不会。做一个兜底。
        if not url:
            return False
        lowered = url.lower()
        return "jp.mercari.com/login" in lowered

    def _exception_is_login_redirect(self, exc: BaseException) -> bool:
        return "jp.mercari.com/login" in str(exc).lower()

    # ---------------- 卖家资料 ----------------

    async def scrape_seller_profile(
        self, context: BrowserContext, user_id: str
    ) -> dict:
        print(f"   -> 开始采集 Mercari 用户 {user_id} 的资料...")
        page = await context.new_page()
        profile_future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def handle_response(response: Response) -> None:
            url = getattr(response, "url", "") or ""
            if (
                USER_PROFILE_API_URL_FRAGMENT in url
                and user_id in url
                and not profile_future.done()
            ):
                try:
                    profile_future.set_result(await response.json())
                except Exception as e:
                    if not profile_future.done():
                        profile_future.set_exception(e)

        page.on("response", handle_response)

        profile_data: dict = {}
        try:
            await page.goto(
                USER_PROFILE_URL_TEMPLATE.format(user_id=user_id),
                wait_until="domcontentloaded",
                timeout=20000,
            )
            data = await asyncio.wait_for(profile_future, timeout=15)
            profile_data = parse_seller_profile_json(data)
        except (PlaywrightTimeoutError, asyncio.TimeoutError):
            print(f"   [采集失败] Mercari 用户 {user_id} 资料请求超时。")
        except Exception as e:
            print(f"   [采集失败] Mercari 用户 {user_id} 资料出错: {e}")
        finally:
            try:
                await page.close()
            except Exception:
                pass

        print(f"   -> Mercari 用户 {user_id} 采集完成。")
        return profile_data

    def format_registration_duration(self, days: int) -> str:
        return format_registration_days(days)
