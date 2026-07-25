"""闲鱼爬虫子类。

承接原 src.scraper.scrape_xianyu 的全部业务语义,通过钩子覆写平台差异部分。
"""
from __future__ import annotations

import asyncio
import random
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
from src.scrapers.xianyu.constants import (
    BAXIA_DIALOG_SELECTOR,
    DETAIL_API_URL_FRAGMENT,
    FAIL_SYS_USER_VALIDATE,
    HOMEPAGE_URL,
    MIDDLEWARE_FRAME_SELECTOR,
    MINI_LOGIN_KEYWORD,
    PASSPORT_HOST_KEYWORD,
    PERSONAL_PAGE_URL_TEMPLATE,
    SEARCH_PAGE_URL,
    STATE_FILENAME,
    USER_HEAD_API_URL_FRAGMENT,
    USER_ITEM_LIST_API_URL_FRAGMENT,
    USER_RATING_LIST_API_URL_FRAGMENT,
)
from src.scrapers.xianyu.parsers import (
    calculate_reputation_from_ratings,
    parse_ratings_data,
    parse_search_results_json,
    parse_user_head_data,
    parse_user_items_data,
)
from src.services.search_pagination import is_search_results_response
from src.utils import format_registration_days, random_sleep, safe_get


class XianyuScraper(BasePlaywrightScraper):
    """闲鱼(Goofish)站点的 Playwright 爬虫。"""

    platform_name = "xianyu"
    homepage_url = HOMEPAGE_URL
    default_state_filename = STATE_FILENAME
    requires_login_state = True

    # ---------------- 站点钩子 ----------------

    def build_search_url(self, keyword: str, filters: dict) -> str:
        params = {"q": keyword}
        return f"{SEARCH_PAGE_URL}?{urlencode(params)}"

    def is_search_api_response(self, response: Response) -> bool:
        return is_search_results_response(response)

    def is_detail_api_response(self, url: str) -> bool:
        return DETAIL_API_URL_FRAGMENT in url

    async def parse_search_json(
        self, json_data: dict, source_label: str
    ) -> list[dict]:
        return await parse_search_results_json(json_data, source_label)

    async def parse_detail_json(
        self, detail_json: dict, item_data: dict
    ) -> tuple[dict, Optional[str], dict]:
        item_do = await safe_get(detail_json, "data", "itemDO", default={})
        seller_do = await safe_get(detail_json, "data", "sellerDO", default={})

        reg_days_raw = await safe_get(seller_do, "userRegDay", default=0)
        registration_duration_text = self.format_registration_duration(
            reg_days_raw if isinstance(reg_days_raw, int) else 0
        )

        zhima_credit_text = await safe_get(
            seller_do, "zhimaLevelInfo", "levelName"
        )

        image_infos = await safe_get(item_do, "imageInfos", default=[])
        if image_infos:
            all_image_urls = [
                img.get("url") for img in image_infos if img.get("url")
            ]
            if all_image_urls:
                item_data["商品图片列表"] = all_image_urls
                item_data["商品主图链接"] = all_image_urls[0]

        item_data["“想要”人数"] = await safe_get(
            item_do, "wantCnt", default=item_data.get("“想要”人数", "NaN")
        )
        item_data["浏览量"] = await safe_get(item_do, "browseCnt", default="-")

        user_id = await safe_get(seller_do, "sellerId")
        seller_id = str(user_id) if user_id else None

        extras = {
            "zhima_credit_text": zhima_credit_text,
            "registration_duration_text": registration_duration_text,
        }
        return item_data, seller_id, extras

    async def apply_filters(
        self, page: Page, filters: dict
    ) -> Optional[Response]:
        new_publish_option = filters.get("new_publish_option") or ""
        personal_only = filters.get("personal_only", False)
        free_shipping = filters.get("free_shipping", False)
        region_filter = (filters.get("region") or "").strip()
        min_price = filters.get("min_price")
        max_price = filters.get("max_price")

        final_response: Optional[Response] = None

        if new_publish_option:
            try:
                await page.click("text=新发布")
                await random_sleep(1, 2)
                async with page.expect_response(
                    is_search_results_response, timeout=20000
                ) as response_info:
                    await page.click(f"text={new_publish_option}")
                    await random_sleep(2, 4)
                final_response = await response_info.value
            except PlaywrightTimeoutError:
                print(
                    f"新发布筛选 '{new_publish_option}' 请求超时，继续执行。"
                )
            except Exception as e:
                print(f"LOG: 应用新发布筛选失败: {e}")

        if personal_only:
            async with page.expect_response(
                is_search_results_response, timeout=20000
            ) as response_info:
                await page.click("text=个人闲置")
                await random_sleep(2, 4)
            final_response = await response_info.value

        if free_shipping:
            try:
                async with page.expect_response(
                    is_search_results_response, timeout=20000
                ) as response_info:
                    await page.click("text=包邮")
                    await random_sleep(2, 4)
                final_response = await response_info.value
            except PlaywrightTimeoutError:
                print("包邮筛选请求超时，继续执行。")
            except Exception as e:
                print(f"LOG: 应用包邮筛选失败: {e}")

        if region_filter:
            final_response = await self._apply_region_filter(
                page, region_filter, final_response
            )

        if min_price or max_price:
            price_container = page.locator(
                'div[class*="search-price-input-container"]'
            ).first
            if await price_container.is_visible():
                if min_price:
                    await price_container.get_by_placeholder("¥").first.fill(
                        str(min_price)
                    )
                    await random_sleep(1, 2.5)
                if max_price:
                    await (
                        price_container.get_by_placeholder("¥")
                        .nth(1)
                        .fill(str(max_price))
                    )
                    await random_sleep(1, 2.5)

                async with page.expect_response(
                    is_search_results_response, timeout=20000
                ) as response_info:
                    await page.keyboard.press("Tab")
                    await random_sleep(2, 4)
                final_response = await response_info.value
            else:
                print("LOG: 警告 - 未找到价格输入容器。")

        return final_response

    async def _apply_region_filter(
        self, page: Page, region_filter: str, existing_response: Optional[Response]
    ) -> Optional[Response]:
        """闲鱼三级区域选择器交互(省 / 市 / 区)。"""
        final_response = existing_response
        try:
            area_trigger = page.get_by_text("区域", exact=True)
            if not await area_trigger.count():
                print("LOG: 未找到区域筛选触发器。")
                return final_response
            await area_trigger.first.click()
            await random_sleep(1.5, 2)
            popover_candidates = page.locator("div.ant-popover")
            popover = popover_candidates.filter(
                has=page.locator(".areaWrap--FaZHsn8E, [class*='areaWrap']")
            ).last
            if not await popover.count():
                popover = popover_candidates.filter(
                    has=page.get_by_text("重新定位")
                ).last
            if not await popover.count():
                popover = popover_candidates.filter(
                    has=page.get_by_text("查看")
                ).last
            if not await popover.count():
                print("LOG: 未找到区域弹窗，跳过区域筛选。")
                return final_response
            await popover.wait_for(state="visible", timeout=5000)

            area_wrap = popover.locator(
                ".areaWrap--FaZHsn8E, [class*='areaWrap']"
            ).first
            await area_wrap.wait_for(state="visible", timeout=3000)
            columns = area_wrap.locator(":scope > div")
            col_prov = columns.nth(0)
            col_city = columns.nth(1)
            col_dist = columns.nth(2)

            region_parts = [
                p.strip() for p in region_filter.split("/") if p.strip()
            ]

            async def _click_in_column(
                column_locator, text_value: str, desc: str
            ) -> None:
                option = column_locator.locator(
                    ".provItem--QAdOx8nD", has_text=text_value
                ).first
                if await option.count():
                    await option.click()
                    await random_sleep(1.5, 2)
                    try:
                        await option.wait_for(state="attached", timeout=1500)
                        await option.wait_for(state="visible", timeout=1500)
                    except PlaywrightTimeoutError:
                        pass

            if region_parts:
                await _click_in_column(col_prov, region_parts[0], "省")
            if len(region_parts) > 1:
                await _click_in_column(col_city, region_parts[1], "市")
            if len(region_parts) > 2:
                await _click_in_column(col_dist, region_parts[2], "区")

            search_btn = popover.locator("div.searchBtn--Ic6RKcAb").first
            if await search_btn.count():
                try:
                    async with page.expect_response(
                        is_search_results_response, timeout=20000
                    ) as response_info:
                        await search_btn.click()
                        await random_sleep(2, 3)
                    final_response = await response_info.value
                except PlaywrightTimeoutError:
                    print("区域筛选提交超时，继续执行。")
            else:
                print("LOG: 未找到区域弹窗的“查看XX件宝贝”按钮，跳过提交。")
        except PlaywrightTimeoutError:
            print(f"区域筛选 '{region_filter}' 请求超时，继续执行。")
        except Exception as e:
            print(f"LOG: 应用区域筛选 '{region_filter}' 失败: {e}")
        return final_response

    async def handle_search_landing(self, page: Page) -> None:
        # 等待关键筛选元素出现,确认到达搜索结果页
        try:
            await page.wait_for_selector("text=新发布", timeout=15000)
        except PlaywrightTimeoutError:
            if self.is_login_redirect(page.url):
                raise LoginRequiredError(
                    f"Login required: redirected to {page.url}"
                )
            raise

        # 关闭广告弹窗(如果有)
        try:
            await page.click("div[class*='closeIconBg']", timeout=3000)
            print("LOG: 已关闭广告弹窗。")
        except PlaywrightTimeoutError:
            print("LOG: 未检测到广告弹窗。")

    async def detect_search_risk_control(self, page: Page) -> None:
        baxia_dialog = page.locator(BAXIA_DIALOG_SELECTOR)
        middleware_widget = page.locator(MIDDLEWARE_FRAME_SELECTOR)
        try:
            await baxia_dialog.wait_for(state="visible", timeout=2000)
            print(
                "\n==================== CRITICAL BLOCK DETECTED ===================="
            )
            print("检测到闲鱼反爬虫验证弹窗 (baxia-dialog)，无法继续操作。")
            print("这通常是因为操作过于频繁或被识别为机器人。")
            print("建议:")
            print("1. 停止脚本一段时间再试。")
            print(
                "2. (推荐) 在 .env 文件中设置 RUN_HEADLESS=false，"
                "以非无头模式运行，这有助于绕过检测。"
            )
            print(
                "==================================================================="
            )
            raise RiskControlError("baxia-dialog")
        except PlaywrightTimeoutError:
            pass

        try:
            await middleware_widget.wait_for(state="visible", timeout=2000)
            print(
                "\n==================== CRITICAL BLOCK DETECTED ===================="
            )
            print(
                "检测到闲鱼反爬虫验证弹窗 (J_MIDDLEWARE_FRAME_WIDGET)，无法继续操作。"
            )
            print("这通常是因为操作过于频繁或被识别为机器人。")
            print(
                "==================================================================="
            )
            raise RiskControlError("J_MIDDLEWARE_FRAME_WIDGET")
        except PlaywrightTimeoutError:
            pass

    def detect_detail_risk_control(self, detail_json: dict) -> None:
        ret_string = str(detail_json.get("ret", []))
        if FAIL_SYS_USER_VALIDATE in ret_string:
            print(
                "\n==================== CRITICAL BLOCK DETECTED ===================="
            )
            print(
                f"检测到闲鱼反爬虫验证 ({FAIL_SYS_USER_VALIDATE})，程序将终止。"
            )
            long_sleep_duration = random.randint(3, 60)
            print(
                f"为避免账户风险，将执行一次长时间休眠 "
                f"({long_sleep_duration} 秒) 后再退出..."
            )
            # 注意：detect_detail_risk_control 是同步函数,不能 await sleep。
            # 但原 scraper 里在这个点上是 await asyncio.sleep(long_sleep_duration)。
            # 由于 base 在调用后马上会抛出,我们保留同步 raise 即可,
            # 长睡眠交由调用方通过风控异常处理路径完成。
            print(
                "==================================================================="
            )
            raise RiskControlError(FAIL_SYS_USER_VALIDATE)

    def is_login_redirect(self, url: str) -> bool:
        if not url:
            return False
        lowered = url.lower()
        return (
            PASSPORT_HOST_KEYWORD in lowered or MINI_LOGIN_KEYWORD in lowered
        )

    def _exception_is_login_redirect(self, exc: BaseException) -> bool:
        return PASSPORT_HOST_KEYWORD in str(exc)

    async def scrape_seller_profile(
        self, context: BrowserContext, user_id: str
    ) -> dict:
        return await _scrape_user_profile(context, user_id)

    def format_registration_duration(self, days: int) -> str:
        return format_registration_days(days)


async def _scrape_user_profile(context: BrowserContext, user_id: str) -> dict:
    """访问指定用户的个人主页，按顺序采集摘要信息、商品列表和评价列表。"""
    print(f"   -> 开始采集用户ID: {user_id} 的完整信息...")
    profile_data: dict = {}
    page = await context.new_page()

    head_api_future = asyncio.get_event_loop().create_future()
    all_items: list = []
    all_ratings: list = []
    stop_item_scrolling = asyncio.Event()
    stop_rating_scrolling = asyncio.Event()

    async def handle_response(response: Response):
        if (
            USER_HEAD_API_URL_FRAGMENT in response.url
            and not head_api_future.done()
        ):
            try:
                head_api_future.set_result(await response.json())
                print("      [API捕获] 用户头部信息... 成功")
            except Exception as e:
                if not head_api_future.done():
                    head_api_future.set_exception(e)

        elif USER_ITEM_LIST_API_URL_FRAGMENT in response.url:
            try:
                data = await response.json()
                all_items.extend(data.get("data", {}).get("cardList", []))
                print(
                    f"      [API捕获] 商品列表... 当前已捕获 {len(all_items)} 件"
                )
                if not data.get("data", {}).get("nextPage", True):
                    stop_item_scrolling.set()
            except Exception:
                stop_item_scrolling.set()

        elif USER_RATING_LIST_API_URL_FRAGMENT in response.url:
            try:
                data = await response.json()
                all_ratings.extend(data.get("data", {}).get("cardList", []))
                print(
                    f"      [API捕获] 评价列表... 当前已捕获 {len(all_ratings)} 条"
                )
                if not data.get("data", {}).get("nextPage", True):
                    stop_rating_scrolling.set()
            except Exception:
                stop_rating_scrolling.set()

    page.on("response", handle_response)

    try:
        await page.goto(
            PERSONAL_PAGE_URL_TEMPLATE.format(user_id=user_id),
            wait_until="domcontentloaded",
            timeout=20000,
        )
        head_data = await asyncio.wait_for(head_api_future, timeout=15)
        profile_data = await parse_user_head_data(head_data)

        print("      [采集阶段] 开始采集该用户的商品列表...")
        await random_sleep(2, 4)
        while not stop_item_scrolling.is_set():
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
            try:
                await asyncio.wait_for(stop_item_scrolling.wait(), timeout=8)
            except asyncio.TimeoutError:
                print("      [滚动超时] 商品列表可能已加载完毕。")
                break
        profile_data["卖家发布的商品列表"] = await parse_user_items_data(
            all_items
        )

        print("      [采集阶段] 开始采集该用户的评价列表...")
        rating_tab_locator = page.locator(
            "//div[text()='信用及评价']/ancestor::li"
        )
        if await rating_tab_locator.count() > 0:
            await rating_tab_locator.click()
            await random_sleep(3, 5)

            while not stop_rating_scrolling.is_set():
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                try:
                    await asyncio.wait_for(
                        stop_rating_scrolling.wait(), timeout=8
                    )
                except asyncio.TimeoutError:
                    print("      [滚动超时] 评价列表可能已加载完毕。")
                    break

            profile_data["卖家收到的评价列表"] = await parse_ratings_data(
                all_ratings
            )
            reputation_stats = await calculate_reputation_from_ratings(
                all_ratings
            )
            profile_data.update(reputation_stats)
        else:
            print("      [警告] 未找到评价选项卡，跳过评价采集。")
    except (PlaywrightTimeoutError, asyncio.TimeoutError) as e:
        print(f"   [采集失败] 采集用户 {user_id} 资料时超时: {e}")
    except Exception as e:
        print(f"   [采集失败] 采集用户 {user_id} 资料时出错: {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

    print(f"   -> 用户 {user_id} 信息采集完成。")
    return profile_data


# 向后兼容:src.scraper 曾直接 export scrape_user_profile
scrape_user_profile = _scrape_user_profile
