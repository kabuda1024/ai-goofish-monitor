"""
多站点爬虫的通用基类。

BasePlaywrightScraper 封装 Playwright 浏览器启动、账号 + 代理轮换、
`ItemAnalysisDispatcher` 商品分析派发、分页循环等通用骨架。各站点子类
只需实现平台差异钩子(URL 构造、搜索/详情 JSON 解析、UI 交互、反爬检测等)。

设计目标:
1. 保留现有闲鱼爬虫的行为(通过 XianyuScraper 承接原 scrape_xianyu 全部语义)
2. 新增 Mercari 时只写钩子,不重复 Playwright / 轮换 / 派发骨架
3. 未来加 Yahoo Auctions / Rakuma 只需再写一个子类
"""
from __future__ import annotations

import abc
import asyncio
import json
import os
import random
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.ai_handler import (
    cleanup_task_images,
    download_all_images,
    get_ai_analysis,
    send_ntfy_notification,
)
from src.config import (
    AI_DEBUG_MODE,
    LOGIN_IS_EDGE,
    RUN_HEADLESS,
    RUNNING_IN_DOCKER,
    SKIP_AI_ANALYSIS,
    STATE_FILE,
)
from src.failure_guard import FailureGuard
from src.infrastructure.persistence.storage_names import build_result_filename
from src.rotation import RotationItem, RotationPool, load_state_files, parse_proxy_pool
from src.services.account_strategy_service import resolve_account_runtime_plan
from src.services.item_analysis_dispatcher import (
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)
from src.services.price_history_service import (
    build_market_reference,
    load_price_snapshots,
    record_market_snapshots,
)
from src.services.result_storage_service import load_processed_link_keys
from src.services.search_pagination import (
    PageAdvanceResult,
    advance_search_page as advance_search_page_xianyu,
)
from src.services.seller_profile_cache import SellerProfileCache
from src.utils import (
    get_link_unique_key,
    log_time,
    random_sleep,
    safe_get,
    save_to_jsonl,
)


class RiskControlError(Exception):
    """站点风控/验证码被触发。"""


class LoginRequiredError(Exception):
    """站点要求登录(cookies 失效)。"""


# 模块级失败守护(与原 scraper.py 共享同一 FailureGuard 实例语义)
FAILURE_GUARD = FailureGuard()
EDGE_DOCKER_WARNING_PRINTED = False


# ---------------------------------------------------------------------------
# 通用工具函数(平台无关)
# ---------------------------------------------------------------------------

def resolve_browser_channel() -> str:
    """在 Docker 中强制 chromium,否则按 LOGIN_IS_EDGE 选 msedge/chrome。"""
    global EDGE_DOCKER_WARNING_PRINTED
    if RUNNING_IN_DOCKER:
        if LOGIN_IS_EDGE and not EDGE_DOCKER_WARNING_PRINTED:
            print(
                "检测到 LOGIN_IS_EDGE=true，但 Docker 镜像未内置 Edge，"
                "任务运行时将改用 Chromium。"
            )
            EDGE_DOCKER_WARNING_PRINTED = True
        return "chromium"
    return "msedge" if LOGIN_IS_EDGE else "chrome"


def should_analyze_images(task_config: dict) -> bool:
    raw_value = task_config.get("analyze_images", True)
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value).strip().lower() not in {"false", "0", "no", "off"}


def format_failure_reason(reason: str, limit: int = 500) -> str:
    if not reason:
        return "未知错误"
    cleaned = " ".join(str(reason).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def as_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_rotation_settings(
    task_config: dict,
    *,
    platform_proxy_defaults: Optional[dict] = None,
) -> dict:
    """
    读取账号 + 代理轮换配置。

    优先级(高 → 低):
      1. 任务配置(task_config["proxy_rotation"] / ["account_rotation"])
      2. 平台专属默认(platform_proxy_defaults,由子类通过 requires_proxy /
         proxy_env_prefix 声明,base.run() 组装后传入)
      3. 全局环境变量(PROXY_ROTATION_ENABLED / PROXY_POOL 等)

    平台声明"不需要代理"(requires_proxy=False)时会**硬关代理**:
    不管全局环境变量或平台专属变量怎么设,只要任务配置也没显式打开,就直连。
    这防止用户设了全局 PROXY_URL 之后闲鱼误走代理导致风控。
    """
    account_cfg = task_config.get("account_rotation") or {}
    proxy_cfg = task_config.get("proxy_rotation") or {}
    platform_defaults = platform_proxy_defaults or {}

    account_enabled = as_bool(
        account_cfg.get("enabled"),
        as_bool(os.getenv("ACCOUNT_ROTATION_ENABLED"), False),
    )
    account_mode = (
        account_cfg.get("mode") or os.getenv("ACCOUNT_ROTATION_MODE", "per_task")
    ).lower()
    account_state_dir = account_cfg.get("state_dir") or os.getenv(
        "ACCOUNT_STATE_DIR", "state"
    )
    account_retry_limit = as_int(
        account_cfg.get("retry_limit"),
        as_int(os.getenv("ACCOUNT_ROTATION_RETRY_LIMIT"), 2),
    )
    account_blacklist_ttl = as_int(
        account_cfg.get("blacklist_ttl_sec"),
        as_int(os.getenv("ACCOUNT_BLACKLIST_TTL"), 300),
    )

    # ---- 代理:任务级 > 平台级 > 全局 ----
    platform_forbids_proxy = platform_defaults.get("forbids_proxy", False)
    platform_default_enabled = platform_defaults.get("enabled")
    platform_default_pool = platform_defaults.get("proxy_pool", "")

    if proxy_cfg.get("enabled") is not None:
        # 任务级显式开关(True/False) 拥有最高优先级
        proxy_enabled = as_bool(proxy_cfg.get("enabled"), False)
    elif platform_forbids_proxy:
        # 平台声明不用代理 → 硬关
        proxy_enabled = False
    elif platform_default_enabled is not None:
        # 平台有专属默认
        proxy_enabled = as_bool(platform_default_enabled, False)
    else:
        # 回退到全局环境变量
        proxy_enabled = as_bool(os.getenv("PROXY_ROTATION_ENABLED"), False)

    if platform_forbids_proxy and proxy_cfg.get("proxy_pool") is None:
        # 平台禁用代理 + 任务也没显式给代理池 → 强制清空
        proxy_pool = ""
    else:
        proxy_pool = (
            proxy_cfg.get("proxy_pool")
            or platform_default_pool
            or os.getenv("PROXY_POOL", "")
        )

    proxy_mode = (
        proxy_cfg.get("mode") or os.getenv("PROXY_ROTATION_MODE", "per_task")
    ).lower()
    proxy_retry_limit = as_int(
        proxy_cfg.get("retry_limit"),
        as_int(os.getenv("PROXY_ROTATION_RETRY_LIMIT"), 2),
    )
    proxy_blacklist_ttl = as_int(
        proxy_cfg.get("blacklist_ttl_sec"),
        as_int(os.getenv("PROXY_BLACKLIST_TTL"), 300),
    )

    return {
        "account_enabled": account_enabled,
        "account_mode": account_mode,
        "account_state_dir": account_state_dir,
        "account_retry_limit": max(1, account_retry_limit),
        "account_blacklist_ttl": max(0, account_blacklist_ttl),
        "proxy_enabled": proxy_enabled,
        "proxy_mode": proxy_mode,
        "proxy_pool": proxy_pool,
        "proxy_retry_limit": max(1, proxy_retry_limit),
        "proxy_blacklist_ttl": max(0, proxy_blacklist_ttl),
    }


def get_ai_analysis_concurrency(task_config: dict) -> int:
    configured = task_config.get("ai_analysis_concurrency")
    default = as_int(os.getenv("AI_ANALYSIS_CONCURRENCY"), 2)
    return max(1, as_int(configured, default))


def get_seller_profile_cache_ttl(task_config: dict) -> int:
    configured = task_config.get("seller_profile_cache_ttl")
    default = as_int(os.getenv("SELLER_PROFILE_CACHE_TTL"), 1800)
    return max(0, as_int(configured, default))


def clean_kwargs(options: dict) -> dict:
    return {k: v for k, v in options.items() if v is not None}


def _looks_like_mobile(ua: str) -> Optional[bool]:
    if not ua:
        return None
    ua_lower = ua.lower()
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        return True
    if "windows" in ua_lower or "macintosh" in ua_lower:
        return False
    return None


def build_context_overrides(snapshot: dict) -> dict:
    """从增强快照(Chrome 扩展导出格式)提取 Playwright context 覆盖参数。"""
    env = snapshot.get("env") or {}
    headers = snapshot.get("headers") or {}
    navigator = env.get("navigator") or {}
    screen = env.get("screen") or {}
    intl = env.get("intl") or {}

    overrides = {}

    ua = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or navigator.get("userAgent")
    )
    if ua:
        overrides["user_agent"] = ua

    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    locale = None
    if accept_language:
        locale = accept_language.split(",")[0].strip()
    elif navigator.get("language"):
        locale = navigator["language"]
    if locale:
        overrides["locale"] = locale

    tz = intl.get("timeZone")
    if tz:
        overrides["timezone_id"] = tz

    width = screen.get("width")
    height = screen.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        overrides["viewport"] = {"width": int(width), "height": int(height)}

    dpr = screen.get("devicePixelRatio")
    if isinstance(dpr, (int, float)):
        overrides["device_scale_factor"] = float(dpr)

    touch_points = navigator.get("maxTouchPoints")
    if isinstance(touch_points, (int, float)):
        overrides["has_touch"] = touch_points > 0

    mobile_flag = _looks_like_mobile(ua or "")
    if mobile_flag is not None:
        overrides["is_mobile"] = mobile_flag

    return clean_kwargs(overrides)


def build_extra_headers(raw_headers: Optional[dict]) -> dict:
    if not raw_headers:
        return {}
    excluded = {"cookie", "content-length"}
    headers = {}
    for key, value in raw_headers.items():
        if not key or key.lower() in excluded or value is None:
            continue
        headers[key] = value
    return headers


# ---------------------------------------------------------------------------
# BasePlaywrightScraper
# ---------------------------------------------------------------------------

class BasePlaywrightScraper(abc.ABC):
    """基于 Playwright 的多站点爬虫基类。

    子类需覆写下列钩子(至少):
      - platform_name / homepage_url / default_state_filename
      - default_context_options()
      - default_launch_args()
      - build_search_url(keyword, filters)
      - is_search_api_response(response)
      - is_detail_api_response(response)
      - parse_search_json(json_data, source_label)
      - parse_detail_json(detail_json, item_data)
      - apply_filters(page, filters)
      - detect_search_risk_control(page)
      - detect_detail_risk_control(detail_json)
      - is_login_redirect(url)
      - scrape_seller_profile(context, user_id)
      - format_registration_duration(days)

    可选覆写(有默认实现):
      - init_page_script()
      - handle_search_landing(page)   —— 落地页反检测/关广告等钩子
      - requires_login_state           —— 默认 True(闲鱼);Mercari 覆写为 False
    """

    # 站点身份
    platform_name: str = "base"
    homepage_url: str = ""
    default_state_filename: Optional[str] = None
    requires_login_state: bool = True

    # 代理策略
    #   True  → 平台需要走代理(如 Mercari 日本站),会读取 {proxy_env_prefix}_PROXY_URL
    #           / _POOL / _ENABLED 环境变量。任务级 proxy_rotation 可覆盖。
    #   False → 平台不需要代理,即使全局 PROXY_ROTATION_ENABLED=true 也硬关。
    #           防止国内站点(闲鱼)因误配全局代理而走境外 IP,触发风控。
    requires_proxy: bool = False
    #   平台专属代理环境变量前缀,如 "MERCARI" → 读 MERCARI_PROXY_URL 等。
    #   仅当 requires_proxy=True 时生效。
    proxy_env_prefix: str = ""

    def __init__(self, task_config: dict, debug_limit: int = 0):
        self.task_config = task_config
        self.debug_limit = debug_limit

    def _resolve_platform_proxy_defaults(self) -> dict:
        """
        根据 requires_proxy / proxy_env_prefix 组装平台级代理默认值,
        传给 get_rotation_settings。
        """
        if not self.requires_proxy:
            return {"forbids_proxy": True}

        prefix = (self.proxy_env_prefix or self.platform_name or "").upper()
        if not prefix:
            return {}

        url = os.getenv(f"{prefix}_PROXY_URL", "").strip()
        pool = os.getenv(f"{prefix}_PROXY_POOL", "").strip()
        enabled_raw = os.getenv(f"{prefix}_PROXY_ENABLED")

        # 平台代理池优先于单一 URL(RotationPool 需要逗号分隔字符串)
        proxy_pool = pool or url

        # 默认:声明 requires_proxy=True 的平台,如果配置了 URL/池,自动启用
        if enabled_raw is not None:
            enabled = as_bool(enabled_raw, False)
        else:
            enabled = bool(proxy_pool)

        return {
            "forbids_proxy": False,
            "enabled": enabled,
            "proxy_pool": proxy_pool,
        }

    # ---------------- 子类必须实现的钩子 ----------------

    @abc.abstractmethod
    def build_search_url(self, keyword: str, filters: dict) -> str:
        """根据关键词 + 过滤器生成搜索页 URL。"""

    @abc.abstractmethod
    def is_search_api_response(self, response: Response) -> bool:
        """判断 Playwright 拦截到的响应是否是搜索结果 API。"""

    @abc.abstractmethod
    def is_detail_api_response(self, url: str) -> bool:
        """判断是否是商品详情 API URL。"""

    @abc.abstractmethod
    async def parse_search_json(
        self, json_data: dict, source_label: str
    ) -> list[dict]:
        """将搜索 API JSON 解析为 [item_data 字典] 列表(中文 key 约定)。"""

    @abc.abstractmethod
    async def parse_detail_json(
        self, detail_json: dict, item_data: dict
    ) -> tuple[dict, Optional[str], dict]:
        """
        解析商品详情 JSON,更新 item_data 并抽取卖家 ID 和其他额外字段。

        Returns:
            (item_data, seller_id, extras)
            extras 至少可包含: zhima_credit_text / registration_duration_text
        """

    @abc.abstractmethod
    async def apply_filters(
        self, page: Page, filters: dict
    ) -> Optional[Response]:
        """在搜索落地页上应用 UI 过滤器(新发布/包邮/区域/价格),返回最后一次筛选后的响应。"""

    @abc.abstractmethod
    async def detect_search_risk_control(self, page: Page) -> None:
        """在搜索页检查反爬弹窗,触发时抛 RiskControlError。"""

    @abc.abstractmethod
    def detect_detail_risk_control(self, detail_json: dict) -> None:
        """在详情 JSON 中检查风控标识,触发时抛 RiskControlError。"""

    @abc.abstractmethod
    def is_login_redirect(self, url: str) -> bool:
        """判断当前 URL 是否指向登录页面(用于抛 LoginRequiredError)。"""

    @abc.abstractmethod
    async def scrape_seller_profile(
        self, context: BrowserContext, user_id: str
    ) -> dict:
        """采集单个卖家的完整资料(个人信息 + 商品列表 + 评价)。"""

    @abc.abstractmethod
    def format_registration_duration(self, days: int) -> str:
        """把注册天数格式化为人类可读的字符串(平台化措辞)。"""

    # ---------------- 有默认实现的可选钩子 ----------------

    def default_context_options(self) -> dict:
        """Playwright context 的默认参数(移动端伪装)。"""
        return {
            "user_agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
            "viewport": {"width": 412, "height": 915},
            "device_scale_factor": 2.625,
            "is_mobile": True,
            "has_touch": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "permissions": ["geolocation"],
            "geolocation": {"longitude": 121.4737, "latitude": 31.2304},
            "color_scheme": "light",
        }

    def default_launch_args(self) -> list[str]:
        """Chromium 启动参数(反自动化检测)。"""
        return [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

    def init_page_script(self) -> str:
        """context.add_init_script 内容,反自动化检测(浏览器指纹)。"""
        return """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
            window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}};
            Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({state: Notification.permission}) :
                    originalQuery(parameters)
            );
        """

    async def handle_search_landing(self, page: Page) -> None:
        """搜索页落地后的通用处理(等待关键元素、关广告弹窗等)。子类可覆写。"""
        return None

    async def advance_to_next_page(
        self, page: Page, page_num: int
    ) -> PageAdvanceResult:
        """翻到下一页并返回新页面的搜索响应。子类可覆写。

        默认沿用闲鱼的实现(依赖 mtop URL 片段和闲鱼分页按钮 CSS 类)。
        Mercari 之类其他站点必须覆写。
        """
        return await advance_search_page_xianyu(page=page, page_num=page_num)

    def extract_filters_from_task(self) -> dict:
        """从 task_config 抽取本次运行相关的过滤器。子类可覆写以支持 platform_options。"""
        raw_new_publish = self.task_config.get("new_publish_option") or ""
        new_publish_option = raw_new_publish.strip()
        if new_publish_option == "__none__":
            new_publish_option = ""
        return {
            "min_price": self.task_config.get("min_price"),
            "max_price": self.task_config.get("max_price"),
            "free_shipping": self.task_config.get("free_shipping", False),
            "new_publish_option": new_publish_option,
            "region": (self.task_config.get("region") or "").strip(),
            "personal_only": self.task_config.get("personal_only", False),
            "platform_options": self.task_config.get("platform_options") or {},
        }

    # ---------------- 通用入口:run() ----------------

    async def run(self) -> int:
        """任务运行入口,处理账号 + 代理轮换 + 失败守护 + 通知。"""
        task_config = self.task_config
        keyword = task_config["keyword"]
        rotation_settings = get_rotation_settings(
            task_config,
            platform_proxy_defaults=self._resolve_platform_proxy_defaults(),
        )
        account_items = load_state_files(rotation_settings["account_state_dir"])
        root_state_exists = (
            bool(self.default_state_filename)
            and os.path.exists(self.default_state_filename)
        )
        runtime_plan = resolve_account_runtime_plan(
            strategy=task_config.get("account_strategy"),
            account_state_file=task_config.get("account_state_file"),
            has_root_state_file=root_state_exists,
            available_account_files=account_items,
        )
        forced_account = runtime_plan["forced_account"]
        if runtime_plan["prefer_root_state"] and self.default_state_filename:
            account_items = [self.default_state_filename]
            rotation_settings["account_enabled"] = False
        elif runtime_plan["use_account_pool"]:
            rotation_settings["account_enabled"] = True
        else:
            rotation_settings["account_enabled"] = False

        account_pool = RotationPool(
            account_items, rotation_settings["account_blacklist_ttl"], "account"
        )
        proxy_pool = RotationPool(
            parse_proxy_pool(rotation_settings["proxy_pool"]),
            rotation_settings["proxy_blacklist_ttl"],
            "proxy",
        )

        selected_account: Optional[RotationItem] = None
        selected_proxy: Optional[RotationItem] = None

        def _select_account(force_new: bool = False) -> Optional[RotationItem]:
            nonlocal selected_account
            if forced_account:
                return RotationItem(value=forced_account)
            if not rotation_settings["account_enabled"]:
                if self.default_state_filename and os.path.exists(
                    self.default_state_filename
                ):
                    return RotationItem(value=self.default_state_filename)
                return None
            if (
                rotation_settings["account_mode"] == "per_task"
                and selected_account
                and not force_new
            ):
                return selected_account
            picked = account_pool.pick_random()
            return picked or selected_account

        def _select_proxy(force_new: bool = False) -> Optional[RotationItem]:
            nonlocal selected_proxy
            if not rotation_settings["proxy_enabled"]:
                return None
            if (
                rotation_settings["proxy_mode"] == "per_task"
                and selected_proxy
                and not force_new
            ):
                return selected_proxy
            picked = proxy_pool.pick_random()
            return picked or selected_proxy

        processed_item_count = 0
        attempt_limit = max(
            rotation_settings["account_retry_limit"],
            rotation_settings["proxy_retry_limit"],
            1,
        )
        last_error = ""
        last_state_path: Optional[str] = None

        task_name_for_guard = task_config.get("task_name", "未命名任务")
        pause_cookie_path = None
        if (
            isinstance(task_config.get("account_state_file"), str)
            and task_config.get("account_state_file").strip()
        ):
            pause_cookie_path = task_config.get("account_state_file").strip()
        elif root_state_exists:
            pause_cookie_path = self.default_state_filename

        decision = FAILURE_GUARD.should_skip_start(
            task_name_for_guard, cookie_path=pause_cookie_path
        )
        if decision.skip:
            print(
                f"[FailureGuard] 任务 '{task_name_for_guard}' 已暂停重试 (连续失败 "
                f"{decision.consecutive_failures}/{FAILURE_GUARD.threshold})"
            )
            if decision.should_notify:
                try:
                    await send_ntfy_notification(
                        {
                            "商品标题": f"[任务暂停] {task_name_for_guard}",
                            "当前售价": "N/A",
                            "商品链接": "#",
                        },
                        "任务处于暂停状态，将跳过执行。\n"
                        f"原因: {decision.reason}\n"
                        f"连续失败: {decision.consecutive_failures}/{FAILURE_GUARD.threshold}\n"
                        f"暂停到: {decision.paused_until.strftime('%Y-%m-%d %H:%M:%S') if decision.paused_until else 'N/A'}\n"
                        "修复方法: 更新登录态/cookies文件后会自动恢复。",
                    )
                except Exception as e:
                    print(f"发送任务暂停通知失败: {e}")

            cleanup_task_images(task_config.get("task_name", "default"))
            return 0

        for attempt in range(1, attempt_limit + 1):
            if attempt == 1:
                selected_account = _select_account()
                selected_proxy = _select_proxy()
            else:
                if (
                    rotation_settings["account_enabled"]
                    and rotation_settings["account_mode"] == "on_failure"
                ):
                    account_pool.mark_bad(selected_account, last_error)
                    selected_account = _select_account(force_new=True)
                if (
                    rotation_settings["proxy_enabled"]
                    and rotation_settings["proxy_mode"] == "on_failure"
                ):
                    proxy_pool.mark_bad(selected_proxy, last_error)
                    selected_proxy = _select_proxy(force_new=True)

            # 需要登录态的站点(闲鱼)才校验;Mercari 之类可不登录直接跑。
            if self.requires_login_state:
                if rotation_settings["account_enabled"] and not selected_account:
                    last_error = "未找到可用的登录状态文件，无法继续执行任务。"
                    print(last_error)
                    break
                if not rotation_settings["account_enabled"] and not selected_account:
                    last_error = "未找到可用的登录状态文件，无法继续执行任务。"
                    print(last_error)
                    break
            if rotation_settings["proxy_enabled"] and not selected_proxy:
                last_error = "未找到可用的代理地址，无法继续执行任务。"
                print(last_error)
                break

            state_path = selected_account.value if selected_account else self.default_state_filename
            last_state_path = state_path
            proxy_server = selected_proxy.value if selected_proxy else None
            if rotation_settings["account_enabled"]:
                print(f"账号轮换：使用登录状态 {state_path}")
            if rotation_settings["proxy_enabled"] and proxy_server:
                print(f"IP 轮换：使用代理 {proxy_server}")

            try:
                processed_item_count += await self._run_scrape_attempt(
                    state_path, proxy_server
                )
                last_error = ""
                FAILURE_GUARD.record_success(task_name_for_guard)
                break
            except LoginRequiredError as e:
                last_error = str(e)
                print(f"检测到登录失效/重定向: {e}")
                break
            except RiskControlError as e:
                last_error = str(e)
                print(f"检测到风控或验证触发: {e}")
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                print(f"本次尝试失败: {last_error}")
                if attempt < attempt_limit:
                    print("将尝试轮换账号/IP 后重试...")

        if last_error:
            await self._notify_task_failure(
                task_config, last_error, cookie_path=last_state_path
            )

        cleanup_task_images(task_config.get("task_name", "default"))
        return processed_item_count

    async def _notify_task_failure(
        self, task_config: dict, reason: str, *, cookie_path: Optional[str]
    ) -> None:
        task_name = task_config.get("task_name", "未命名任务")
        keyword = task_config.get("keyword", "")
        formatted_reason = format_failure_reason(reason)

        pause_immediately = any(
            marker in formatted_reason
            for marker in (
                "未找到可用的代理地址",
                "未找到可用的登录状态文件",
            )
        )

        guard_result = FAILURE_GUARD.record_failure(
            task_name,
            formatted_reason,
            cookie_path=cookie_path,
            min_failures_to_pause=1 if pause_immediately else None,
        )

        if not guard_result.get("should_notify"):
            print(
                f"[FailureGuard] 任务 '{task_name}' 失败计数 "
                f"{guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold}，暂不通知。"
            )
            return

        paused_until = guard_result.get("paused_until")
        paused_until_str = (
            paused_until.strftime("%Y-%m-%d %H:%M:%S") if paused_until else "N/A"
        )

        product_data = {
            "商品标题": f"[任务异常] {task_name}",
            "当前售价": "N/A",
            "商品链接": "#",
        }
        notify_reason = (
            f"任务运行失败(已连续 {guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold} 次): {formatted_reason}"
            f"\n任务: {task_name}"
            f"\n关键词: {keyword or 'N/A'}"
            f"\n已自动暂停重试，暂停到: {paused_until_str}"
            f"\n修复后(更新登录态/cookies文件)将自动恢复。"
        )

        try:
            await send_ntfy_notification(product_data, notify_reason)
        except Exception as e:
            print(f"发送任务异常通知失败: {e}")

    # ---------------- 核心骨架:单次尝试 ----------------

    async def _run_scrape_attempt(
        self, state_file: Optional[str], proxy_server: Optional[str]
    ) -> int:
        task_config = self.task_config
        keyword = task_config["keyword"]
        max_pages = task_config.get("max_pages", 1)
        analyze_images = should_analyze_images(task_config)
        decision_mode = str(task_config.get("decision_mode", "ai")).strip().lower()
        if decision_mode not in {"ai", "keyword"}:
            decision_mode = "ai"
        keyword_rules = task_config.get("keyword_rules") or []
        ai_prompt_text = task_config.get("ai_prompt_text", "")
        filters = self.extract_filters_from_task()

        # 历史数据加载
        history_run_id = datetime.now().strftime("%Y%m%d%H%M%S")
        history_seen_item_ids: set[str] = set()
        historical_snapshots = load_price_snapshots(keyword)
        result_filename = build_result_filename(keyword)
        processed_links = load_processed_link_keys(keyword)
        if processed_links:
            print(
                f"LOG: 发现已存在结果集 {result_filename}，已加载 "
                f"{len(processed_links)} 个历史商品用于去重。"
            )
        else:
            print(f"LOG: 结果集 {result_filename} 当前为空，将写入新记录。")

        processed_item_count = 0
        stop_scraping = False

        # 登录态读取
        snapshot_data: Any = None
        if state_file:
            if not os.path.exists(state_file):
                if self.requires_login_state:
                    raise FileNotFoundError(f"登录状态文件不存在: {state_file}")
            else:
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        snapshot_data = json.load(f)
                except Exception as e:
                    print(f"警告：读取登录状态文件失败，将直接按路径使用: {e}")

        async with async_playwright() as p:
            launch_kwargs = {
                "headless": RUN_HEADLESS,
                "args": self.default_launch_args(),
            }
            if proxy_server:
                launch_kwargs["proxy"] = {"server": proxy_server}
            launch_kwargs["channel"] = resolve_browser_channel()

            browser = await p.chromium.launch(**launch_kwargs)

            context_kwargs = self.default_context_options()
            storage_state_arg: Any = state_file if state_file else None
            analysis_dispatcher: Optional[ItemAnalysisDispatcher] = None

            if isinstance(snapshot_data, dict):
                if any(
                    key in snapshot_data
                    for key in ("env", "headers", "page", "storage")
                ):
                    print(f"检测到增强浏览器快照，应用环境参数: {state_file}")
                    storage_state_arg = {"cookies": snapshot_data.get("cookies", [])}
                    context_kwargs.update(build_context_overrides(snapshot_data))
                    extra_headers = build_extra_headers(snapshot_data.get("headers"))
                    if extra_headers:
                        context_kwargs["extra_http_headers"] = extra_headers
                else:
                    storage_state_arg = snapshot_data

            context_kwargs = clean_kwargs(context_kwargs)
            # 无登录态且不需要登录态时,不给 context 传 storage_state
            context_new_kwargs = {}
            if storage_state_arg is not None:
                context_new_kwargs["storage_state"] = storage_state_arg
            context = await browser.new_context(
                **context_new_kwargs, **context_kwargs
            )

            seller_profile_cache = SellerProfileCache(
                ttl_seconds=get_seller_profile_cache_ttl(task_config)
            )
            analysis_dispatcher = ItemAnalysisDispatcher(
                concurrency=get_ai_analysis_concurrency(task_config),
                skip_ai_analysis=SKIP_AI_ANALYSIS,
                seller_loader=lambda user_id: seller_profile_cache.get_or_load(
                    str(user_id),
                    lambda seller_key: self.scrape_seller_profile(context, seller_key),
                ),
                image_downloader=download_all_images,
                ai_analyzer=get_ai_analysis,
                notifier=send_ntfy_notification,
                saver=save_to_jsonl,
            )

            init_script = self.init_page_script()
            if init_script:
                await context.add_init_script(init_script)

            page = await context.new_page()

            try:
                # 步骤 0:模拟真实用户访问首页
                if self.homepage_url:
                    log_time("步骤 0 - 模拟真实用户访问首页...")
                    await page.goto(
                        self.homepage_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    log_time("[反爬] 在首页停留，模拟浏览...")
                    await random_sleep(1, 2)
                    await page.evaluate(
                        "window.scrollBy(0, Math.random() * 500 + 200)"
                    )
                    await random_sleep(1, 2)

                # 步骤 1:导航到搜索结果页
                log_time("步骤 1 - 导航到搜索结果页...")
                search_url = self.build_search_url(keyword, filters)
                log_time(f"目标URL: {search_url}")

                async with page.expect_response(
                    self.is_search_api_response, timeout=30000
                ) as initial_response_info:
                    await page.goto(
                        search_url, wait_until="domcontentloaded", timeout=60000
                    )
                if self.is_login_redirect(page.url):
                    raise LoginRequiredError(
                        f"Login required: redirected to {page.url}"
                        " (cookies/state likely expired)"
                    )

                initial_response = await initial_response_info.value

                # 搜索落地页处理(反爬弹窗/关广告等)
                try:
                    await self.handle_search_landing(page)
                except PlaywrightTimeoutError as e:
                    if self.is_login_redirect(page.url):
                        raise LoginRequiredError(
                            f"Login required: redirected to {page.url}"
                        ) from e
                    raise

                await random_sleep(1, 3)
                await self.detect_search_risk_control(page)

                # 步骤 2:应用筛选条件
                log_time("步骤 2 - 应用筛选条件...")
                final_response = await self.apply_filters(page, filters)

                log_time("所有筛选已完成，开始处理商品列表...")

                current_response = (
                    final_response
                    if final_response and final_response.ok
                    else initial_response
                )

                for page_num in range(1, max_pages + 1):
                    if stop_scraping:
                        break
                    log_time(f"开始处理第 {page_num}/{max_pages} 页 ...")

                    if page_num > 1:
                        page_advance_result = await self.advance_to_next_page(
                            page=page,
                            page_num=page_num,
                        )
                        if not page_advance_result.advanced:
                            break
                        current_response = page_advance_result.response

                    if not (current_response and current_response.ok):
                        log_time(f"第 {page_num} 页响应无效，跳过。")
                        continue

                    basic_items = await self.parse_search_json(
                        await current_response.json(), f"第 {page_num} 页"
                    )
                    if not basic_items:
                        break
                    historical_snapshots.extend(
                        record_market_snapshots(
                            keyword=keyword,
                            task_name=task_config.get(
                                "task_name", "Untitled Task"
                            ),
                            items=basic_items,
                            run_id=history_run_id,
                            snapshot_time=datetime.now().isoformat(),
                            seen_item_ids=history_seen_item_ids,
                        )
                    )

                    total_items_on_page = len(basic_items)
                    for i, item_data in enumerate(basic_items, 1):
                        if self.debug_limit > 0 and processed_item_count >= self.debug_limit:
                            log_time(
                                f"已达到调试上限 ({self.debug_limit})，停止获取新商品。"
                            )
                            stop_scraping = True
                            break

                        unique_key = get_link_unique_key(item_data["商品链接"])
                        if unique_key in processed_links:
                            log_time(
                                f"[页内进度 {i}/{total_items_on_page}] 商品 "
                                f"'{item_data['商品标题'][:20]}...' 已存在，跳过。"
                            )
                            continue

                        log_time(
                            f"[页内进度 {i}/{total_items_on_page}] 发现新商品，"
                            f"获取详情: {item_data['商品标题'][:30]}..."
                        )
                        await random_sleep(2, 4)

                        processed = await self._process_item_detail(
                            context=context,
                            item_data=item_data,
                            keyword=keyword,
                            basic_items=basic_items,
                            historical_snapshots=historical_snapshots,
                            decision_mode=decision_mode,
                            analyze_images=analyze_images,
                            ai_prompt_text=ai_prompt_text,
                            keyword_rules=keyword_rules,
                            analysis_dispatcher=analysis_dispatcher,
                        )
                        if processed:
                            processed_links.add(unique_key)
                            processed_item_count += 1
                            log_time(
                                f"商品已提交后台分析。累计处理 "
                                f"{processed_item_count} 个新商品。"
                            )
                            log_time(
                                "[反爬] 执行一次主要的随机延迟以模拟用户浏览间隔..."
                            )
                            await random_sleep(5, 10)

                    if not stop_scraping and page_num < max_pages:
                        print(
                            f"--- 第 {page_num} 页处理完毕，准备翻页。"
                            "执行一次页面间的长时休息... ---"
                        )
                        await random_sleep(10, 15)

            except PlaywrightTimeoutError as e:
                if self.is_login_redirect(page.url):
                    raise LoginRequiredError(
                        f"Login required: redirected to {page.url}"
                        " (cookies/state likely expired)"
                    ) from e
                print(f"\n操作超时错误: 页面元素或网络响应未在规定时间内出现。\n{e}")
                raise
            except asyncio.CancelledError:
                log_time("收到取消信号，正在终止当前爬虫任务...")
                raise
            except Exception as e:
                if type(e).__name__ == "TargetClosedError":
                    log_time("浏览器已关闭，忽略后续异常（可能是任务被停止）。")
                    return processed_item_count
                # 让子类判断是否是登录跳转异常
                if self._exception_is_login_redirect(e):
                    raise LoginRequiredError(
                        f"Login required: redirected to passport flow ({e})"
                    ) from e
                print(f"\n爬取过程中发生未知错误: {e}")
                raise
            finally:
                if analysis_dispatcher is not None:
                    log_time("等待后台分析任务完成...")
                    await analysis_dispatcher.join()
                log_time("任务执行完毕，浏览器将在5秒后自动关闭...")
                await asyncio.sleep(5)
                if self.debug_limit:
                    input("按回车键关闭浏览器...")
                await browser.close()

        return processed_item_count

    def _exception_is_login_redirect(self, exc: BaseException) -> bool:
        """子类可覆写。默认根据字符串匹配当前平台的 passport 域。"""
        return False

    async def _process_item_detail(
        self,
        *,
        context: BrowserContext,
        item_data: dict,
        keyword: str,
        basic_items: list[dict],
        historical_snapshots: list[dict],
        decision_mode: str,
        analyze_images: bool,
        ai_prompt_text: str,
        keyword_rules: list[str],
        analysis_dispatcher: ItemAnalysisDispatcher,
    ) -> bool:
        """访问单商品详情页,提交 AI 分析任务。返回是否成功提交。"""
        detail_page = await context.new_page()
        try:
            async with detail_page.expect_response(
                lambda r: self.is_detail_api_response(r.url), timeout=25000
            ) as detail_info:
                await detail_page.goto(
                    item_data["商品链接"],
                    wait_until="domcontentloaded",
                    timeout=25000,
                )

            detail_response = await detail_info.value
            if not detail_response.ok:
                print(
                    f"   错误: 获取商品详情API响应失败，状态码: {detail_response.status}"
                )
                if AI_DEBUG_MODE:
                    print(
                        f"--- [DETAIL DEBUG] FAILED RESPONSE from "
                        f"{item_data['商品链接']} ---"
                    )
                    try:
                        print(await detail_response.text())
                    except Exception as e:
                        print(f"无法读取响应内容: {e}")
                    print(
                        "----------------------------------------------------"
                    )
                return False

            detail_json = await detail_response.json()

            # 让子类检查风控标识
            self.detect_detail_risk_control(detail_json)

            item_data, seller_id, extras = await self.parse_detail_json(
                detail_json, item_data
            )
            # 让通知层能感知平台(通知模板会传入 item_data 而不是 final_record)
            item_data["_platform"] = self.platform_name
            zhima_credit_text = extras.get("zhima_credit_text")
            registration_duration_text = extras.get(
                "registration_duration_text", "未知"
            )

            final_record = {
                "爬取时间": datetime.now().isoformat(),
                "搜索关键字": keyword,
                "任务名称": self.task_config.get(
                    "task_name", "Untitled Task"
                ),
                "商品信息": item_data,
                "卖家信息": {},
                "_platform": self.platform_name,
            }
            price_reference = build_market_reference(
                keyword=keyword,
                item=item_data,
                current_market_items=basic_items,
                historical_snapshots=historical_snapshots,
            )
            final_record["价格参考"] = price_reference
            final_record["price_insight"] = price_reference.get(
                "本商品价格位置", {}
            )

            analysis_dispatcher.submit(
                ItemAnalysisJob(
                    keyword=keyword,
                    task_name=self.task_config.get(
                        "task_name", "Untitled Task"
                    ),
                    decision_mode=decision_mode,
                    analyze_images=analyze_images,
                    prompt_text=ai_prompt_text,
                    keyword_rules=tuple(keyword_rules or []),
                    final_record=final_record,
                    seller_id=str(seller_id) if seller_id else None,
                    zhima_credit_text=zhima_credit_text,
                    registration_duration_text=registration_duration_text,
                )
            )
            return True

        except RiskControlError:
            raise
        except PlaywrightTimeoutError:
            print("   错误: 访问商品详情页或等待API响应超时。")
            return False
        except Exception as e:
            if type(e).__name__ == "TargetClosedError":
                raise
            print(f"   错误: 处理商品详情时发生未知错误: {e}")
            return False
        finally:
            try:
                await detail_page.close()
            except Exception:
                pass
            await random_sleep(2, 4)
