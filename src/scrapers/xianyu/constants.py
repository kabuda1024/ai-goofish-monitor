"""闲鱼站点相关的硬编码常量。"""
from __future__ import annotations

STATE_FILENAME = "xianyu_state.json"

HOMEPAGE_URL = "https://www.goofish.com/"
SEARCH_PAGE_URL = "https://www.goofish.com/search"
PERSONAL_PAGE_URL_TEMPLATE = "https://www.goofish.com/personal?userId={user_id}"

# API URL 片段(用于 Playwright response 拦截)
SEARCH_API_URL_FRAGMENT = "h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search"
DETAIL_API_URL_FRAGMENT = "h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail"
USER_HEAD_API_URL_FRAGMENT = "mtop.idle.web.user.page.head"
USER_ITEM_LIST_API_URL_FRAGMENT = "mtop.idle.web.xyh.item.list"
USER_RATING_LIST_API_URL_FRAGMENT = "mtop.idle.web.trade.rate.list"

# 登录/风控标识
PASSPORT_HOST_KEYWORD = "passport.goofish.com"
MINI_LOGIN_KEYWORD = "mini_login"
BAXIA_DIALOG_SELECTOR = "div.baxia-dialog-mask"
MIDDLEWARE_FRAME_SELECTOR = "div.J_MIDDLEWARE_FRAME_WIDGET"
FAIL_SYS_USER_VALIDATE = "FAIL_SYS_USER_VALIDATE"

# 移动端跳转链接
FLEAMARKET_SCHEME = "fleamarket://"
