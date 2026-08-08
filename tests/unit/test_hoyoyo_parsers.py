"""Hoyoyo(日购聚合站)搜索结果解析器测试。"""
from __future__ import annotations

from src.scrapers.hoyoyo.parsers import (
    parse_item_blocks_html,
    parse_search_response_json,
)


def _yahoo_auction_block(href: str = "/auction~detail~itemId~a12345.html") -> str:
    return f"""
    <div class="item-search-item-info">
      <a href="{href}">
        <img class="lazy" src="https://auctions.c.yimg.jp/img/a12345.jpg" />
        <p class="item-search-item-title">Nintendo Switch 本体 中古</p>
        <div class="content-price">12,000 日元 ( 580 RMB )</div>
        <div class="item-search-item-option">34 1日</div>
      </a>
    </div>
    """


def _mercari_block(href: str = "/mercari~detail~id~m987.html") -> str:
    return f"""
    <div class="item-search-item-info">
      <a href="{href}">
        <img class="lazy" src="https://image.hoyoyo-cache.com/mercari/m987.jpg" />
        <p class="item-search-item-title">MacBook Air M2 中古品</p>
        <div class="content-price">145,000 日元 ( 6670 RMB )</div>
      </a>
    </div>
    """


def _yahoo_shopping_block(href: str = "/yshopping~detail~id~y555.html") -> str:
    return f"""
    <div class="item-search-item-info">
      <a href="{href}">
        <img class="lazy" src="https://item-shopping.c.yimg.jp/i/l/y555.jpg" />
        <p class="item-search-item-title">任天堂 新品未開封</p>
        <div class="content-price">38,500 日元 ( 1780 RMB )</div>
      </a>
    </div>
    """


REQUIRED_KEYS = {"商品ID", "商品标题", "当前售价", "商品链接", "商品图片列表"}


class TestParseItemBlocksHtml:
    def test_empty_content_returns_empty_list(self):
        assert parse_item_blocks_html("") == []
        assert parse_item_blocks_html(None) == []  # type: ignore[arg-type]

    def test_yahoo_auction_block_extracts_publish_time(self):
        items = parse_item_blocks_html(_yahoo_auction_block())
        assert len(items) == 1
        item = items[0]
        assert item["商品标签"] == ["yahoo_auction"]
        assert item["发布时间"] == "1日"
        assert item["当前售价"] == "¥12,000"

    def test_mercari_block_missing_option_falls_back_to_unknown_time(self):
        items = parse_item_blocks_html(_mercari_block())
        assert len(items) == 1
        item = items[0]
        assert item["商品标签"] == ["mercari"]
        assert item["发布时间"] == "未知时间"

    def test_yahoo_shopping_block_missing_option_falls_back_to_unknown_time(self):
        items = parse_item_blocks_html(_yahoo_shopping_block())
        assert len(items) == 1
        item = items[0]
        assert item["商品标签"] == ["yahoo_shopping"]
        assert item["发布时间"] == "未知时间"

    def test_required_keys_present_for_every_item(self):
        html = _yahoo_auction_block() + _mercari_block() + _yahoo_shopping_block()
        items = parse_item_blocks_html(html)
        assert len(items) == 3
        for item in items:
            missing = REQUIRED_KEYS - set(item.keys())
            assert not missing, f"missing keys: {missing}"

    def test_price_text_parses_display_price_with_thousand_separator(self):
        items = parse_item_blocks_html(_mercari_block())
        assert items[0]["当前售价"] == "¥145,000"

    def test_price_text_without_yen_marker_falls_back_to_error_placeholder(self):
        html = """
        <div class="item-search-item-info">
          <a href="/auction~detail~itemId~bad1.html">
            <img class="lazy" src="https://auctions.c.yimg.jp/img/bad1.jpg" />
            <p class="item-search-item-title">価格不明の商品</p>
            <div class="content-price">お問い合わせください</div>
          </a>
        </div>
        """
        items = parse_item_blocks_html(html)
        assert items[0]["当前售价"] == "价格异常"

    def test_item_id_prefixes_differ_across_source_types(self):
        html = _yahoo_auction_block() + _mercari_block() + _yahoo_shopping_block()
        items = parse_item_blocks_html(html)
        ids = [item["商品ID"] for item in items]
        assert ids[0] == "hoyoyo:yahoo_auction:a12345"
        assert ids[1] == "hoyoyo:mercari:m987"
        assert ids[2] == "hoyoyo:yahoo_shopping:y555"
        assert len(set(ids)) == len(ids)

    def test_block_without_link_is_skipped(self):
        html = """
        <div class="item-search-item-info">
          <p class="item-search-item-title">无链接商品</p>
        </div>
        """
        assert parse_item_blocks_html(html) == []


class TestParseSearchResponseJson:
    def test_splits_items_and_page_info(self):
        payload = {
            "max_p": 5,
            "p": "2",
            "nextPage": "Y",
            "status": 1,
            "content": _yahoo_auction_block(),
        }
        items, page_info = parse_search_response_json(payload)
        assert len(items) == 1
        assert page_info == {"max_p": 5, "p": 2, "has_next": True}

    def test_next_page_n_means_has_next_false(self):
        payload = {"max_p": 1, "p": "1", "nextPage": "N", "content": ""}
        items, page_info = parse_search_response_json(payload)
        assert items == []
        assert page_info["has_next"] is False

    def test_missing_page_fields_fall_back_to_defaults(self):
        items, page_info = parse_search_response_json({})
        assert items == []
        assert page_info == {"max_p": 1, "p": 1, "has_next": False}
